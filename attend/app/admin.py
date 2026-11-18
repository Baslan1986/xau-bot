"""Admin API. Every query is scoped to the admin's tenant."""
import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import attendance, leaves, names, profiles, schedule
from .db import get_db
from .deps import (admin_user, err, local_people_only, month_bounds, ser_event, ser_leave,
                   ser_result, ser_shift, ser_user, tenant_plant, user_tz)
from .models import Alert, Event, LeaveRequest, LocationPing, NameQuestion, Plant, Shift, Team, User
from .notify import notify
from .security import hash_password
from .timeutil import iso, local_day_bounds_utc, now_utc, to_local

router = APIRouter(prefix="/api/admin")


def _user(db: Session, admin: User, uid: int) -> User:
    u = db.get(User, uid)
    if not u or u.tenant_id != admin.tenant_id:
        err("الموظف غير موجود", 404)
    return u


def _team(db: Session, admin: User, tid: int) -> Team:
    t = db.get(Team, tid)
    if not t:
        err("الفريق غير موجود", 404)
    tenant_plant(db, admin, t.plant_id)
    return t


# ------------------------------------------------------------------ leave approvals
class DecideIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)
    substitute_id: int | None = None


@router.get("/leaves")
def admin_leaves(status: str | None = None, admin: User = Depends(admin_user),
                 db: Session = Depends(get_db)):
    q = (select(LeaveRequest).join(User, User.id == LeaveRequest.user_id)
         .where(User.tenant_id == admin.tenant_id)
         .order_by(LeaveRequest.created_at.desc()).limit(200))
    if status:
        q = q.where(LeaveRequest.status == status)
    return [ser_leave(x, user_tz(db, x.user)) for x in db.scalars(q)]


@router.post("/leaves/{lid}/decide", dependencies=[Depends(local_people_only)])
def decide_leave(lid: int, body: DecideIn, admin: User = Depends(admin_user),
                 db: Session = Depends(get_db)):
    lr = db.get(LeaveRequest, lid)
    if not lr or lr.user.tenant_id != admin.tenant_id:
        err("الطلب غير موجود", 404)
    if body.substitute_id:
        _user(db, admin, body.substitute_id)
    try:
        lr = leaves.decide(db, admin, lr, body.approve, body.note, body.substitute_id)
    except leaves.LeaveError as e:
        err(str(e))
    return ser_leave(lr, user_tz(db, lr.user))


# ------------------------------------------------------------------ plant profiles
class PlantIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    timezone: str = Field(max_length=64)
    locale: str = Field(default="ar", max_length=16)
    polygon: str = Field(max_length=200_000)
    site_polygon: str | None = Field(default=None, max_length=200_000)
    shifts: str = Field(max_length=2000)  # "code, start, hours, label" per line
    grace_minutes: int = Field(default=10, ge=0, le=120)
    min_staff: int = Field(default=1, ge=0, le=50)
    plant_ref: str | None = Field(default=None, max_length=64)


def _profile(admin: User, body: PlantIn, plant_ref: str) -> dict:
    return {"tenantId": admin.tenant_id, "plantId": plant_ref, "name": body.name,
            "timezone": body.timezone, "locale": body.locale,
            "polygons": {"plant": body.polygon, "site": body.site_polygon or None},
            "shifts": body.shifts, "graceMinutes": body.grace_minutes,
            "minStaff": body.min_staff}


@router.get("/plants")
def list_plants(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [profiles.to_dict(p) for p in
            db.scalars(select(Plant).where(Plant.tenant_id == admin.tenant_id))]


@router.post("/plants")
def create_plant(body: PlantIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    import secrets
    ref = body.plant_ref or f"p-{secrets.token_hex(4)}"
    if db.scalars(select(Plant).where(Plant.tenant_id == admin.tenant_id,
                                      Plant.plant_ref == ref)).first():
        err("plantId مستخدم")
    try:
        return profiles.to_dict(profiles.upsert(db, _profile(admin, body, ref)))
    except profiles.ProfileError as e:
        err(str(e))


@router.put("/plants/{pid}")
def update_plant(pid: int, body: PlantIn, admin: User = Depends(admin_user),
                 db: Session = Depends(get_db)):
    p = tenant_plant(db, admin, pid)
    try:
        return profiles.to_dict(profiles.upsert(db, _profile(admin, body, p.plant_ref)))
    except profiles.ProfileError as e:
        err(str(e))


# ------------------------------------------------------------------ teams & schedule
class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    plant_id: int
    pattern: str = Field(max_length=400)
    anchor_date: date


def ser_team(t: Team):
    return {"id": t.id, "name": t.name, "plant_id": t.plant_id, "pattern": t.pattern,
            "anchor_date": t.anchor_date.isoformat()}


@router.get("/teams")
def list_teams(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    q = select(Team).join(Plant).where(Plant.tenant_id == admin.tenant_id)
    return [ser_team(t) for t in db.scalars(q)]


def _apply_team(db: Session, admin: User, t: Team, body: TeamIn):
    plant = tenant_plant(db, admin, body.plant_id)
    try:
        t.pattern = ",".join(schedule.parse_pattern(body.pattern, plant))
    except ValueError as e:
        err(str(e))
    t.name, t.plant_id, t.anchor_date = body.name.strip(), plant.id, body.anchor_date


@router.post("/teams")
def create_team(body: TeamIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    t = Team(name="", plant_id=body.plant_id, pattern="", anchor_date=body.anchor_date)
    _apply_team(db, admin, t, body)
    db.add(t)
    db.commit()
    return ser_team(t)


@router.put("/teams/{tid}")
def update_team(tid: int, body: TeamIn, admin: User = Depends(admin_user),
                db: Session = Depends(get_db)):
    t = _team(db, admin, tid)
    _apply_team(db, admin, t, body)
    db.commit()
    return ser_team(t)


class GenerateIn(BaseModel):
    team_id: int
    start: date
    days: int = Field(default=30, ge=1, le=120)


@router.post("/schedule/generate")
def generate_schedule(body: GenerateIn, admin: User = Depends(admin_user),
                      db: Session = Depends(get_db)):
    t = _team(db, admin, body.team_id)
    try:
        n = schedule.generate(db, t, body.start, body.start + timedelta(days=body.days - 1))
    except (ValueError, profiles.ProfileError) as e:
        err(str(e))
    return {"created": n}


@router.get("/schedule")
def get_schedule(plant_id: int, start: date, days: int = 14,
                 admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    plant = tenant_plant(db, admin, plant_id)
    z = profiles.plant_tz(plant)
    lo = local_day_bounds_utc(start, z)[0]
    hi = lo + timedelta(days=min(days, 62))
    shifts = db.scalars(select(Shift).where(Shift.plant_id == plant.id, Shift.start >= lo,
                                            Shift.start < hi).order_by(Shift.start)).all()
    nm = {u.id: u.name for u in db.scalars(select(User).where(User.tenant_id == admin.tenant_id))}
    return {
        "shifts": [{**ser_shift(s, z, plant), "name": nm.get(s.user_id),
                    "covering_for_name": nm.get(s.covering_for)} for s in shifts],
        "gaps": [{"start": iso(g["start"], z), "code": g["code"], "count": g["count"],
                  "needed": g["needed"]} for g in schedule.coverage_gaps(db, plant, lo, hi)],
    }


class ShiftIn(BaseModel):
    user_id: int
    code: str = Field(max_length=8)
    day: date


@router.post("/shifts")
def add_shift(body: ShiftIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = _user(db, admin, body.user_id)
    if not u.plant_id:
        err("الموظف ما إله محطة")
    plant = db.get(Plant, u.plant_id)
    try:
        s, e = schedule.shift_window(plant, body.day, body.code.upper())
    except profiles.ProfileError as ex:
        err(str(ex))
    if db.scalars(select(Shift).where(Shift.user_id == u.id, Shift.start < e,
                                      Shift.end > s)).first():
        err("عنده شفت بنفس الوقت")
    sh = Shift(user_id=u.id, plant_id=plant.id, code=body.code.upper(), start=s, end=e,
               source="manual")
    db.add(sh)
    z = profiles.plant_tz(plant)
    notify(db, u.id, f"انضاف إلك شفت ({sh.code}) {iso(s, z)}", "shift_added", {"start": iso(s, z)})
    db.commit()
    return ser_shift(sh, z, plant)


@router.delete("/shifts/{sid}")
def delete_shift(sid: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = db.get(Shift, sid)
    if not s:
        err("الشفت غير موجود", 404)
    tenant_plant(db, admin, s.plant_id)
    db.delete(s)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ people
class EmployeeIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=30)
    password: str | None = Field(default=None, min_length=6, max_length=200)
    role: str = Field(default="employee", pattern="^(employee|admin)$")
    job_title: str | None = Field(default=None, max_length=80)
    plant_id: int | None = None
    team_id: int | None = None
    active: bool = True
    aliases: list[str] = Field(default_factory=list, max_length=20)
    annual_balance: float = Field(default=14, ge=0, le=365)
    sick_balance: float = Field(default=14, ge=0, le=365)
    departure_hours_balance: float = Field(default=8, ge=0, le=500)


def _apply_employee(db: Session, admin: User, u: User, body: EmployeeIn):
    phone = body.phone.strip()
    other = db.scalars(select(User).where(User.phone == phone, User.id != (u.id or 0))).first()
    if other:
        err(f"الرقم {phone} مستخدم")
    if body.team_id:
        body.plant_id = _team(db, admin, body.team_id).plant_id
    if body.plant_id:
        tenant_plant(db, admin, body.plant_id)
    for k in ("role", "job_title", "plant_id", "team_id", "active",
              "annual_balance", "sick_balance", "departure_hours_balance"):
        setattr(u, k, getattr(body, k))
    u.tenant_id, u.name, u.phone = admin.tenant_id, body.name.strip(), phone
    if body.password:
        u.password_hash = hash_password(body.password)
    db.flush()
    try:
        names.set_aliases(db, u, body.aliases)
    except names.AliasConflict as e:
        db.rollback()
        err(str(e))


@router.get("/employees")
def list_employees(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    from .models import PersonAlias
    al: dict[int, list[str]] = {}
    for a in db.scalars(select(PersonAlias).where(PersonAlias.tenant_id == admin.tenant_id)):
        al.setdefault(a.user_id, []).append(a.form)
    return [{**ser_user(u), "aliases": sorted(al.get(u.id, []))} for u in db.scalars(
        select(User).where(User.tenant_id == admin.tenant_id).order_by(User.name))]


@router.post("/employees", dependencies=[Depends(local_people_only)])
def create_employee(body: EmployeeIn, admin: User = Depends(admin_user),
                    db: Session = Depends(get_db)):
    if not body.password:
        err("لازم كلمة سر أولية (6 خانات على الأقل)")
    u = User(tenant_id=admin.tenant_id, name="", phone="", password_hash="")
    db.add(u)
    _apply_employee(db, admin, u, body)
    db.commit()
    return ser_user(u)


@router.put("/employees/{uid}", dependencies=[Depends(local_people_only)])
def update_employee(uid: int, body: EmployeeIn, admin: User = Depends(admin_user),
                    db: Session = Depends(get_db)):
    u = _user(db, admin, uid)
    if u.id == admin.id and (body.role != "admin" or not body.active):
        err("ما بتقدر تشيل صلاحيتك أو توقف حسابك")
    _apply_employee(db, admin, u, body)
    db.commit()
    return ser_user(u)


class LoginSetIn(BaseModel):
    phone: str = Field(min_length=6, max_length=30)
    password: str = Field(min_length=6, max_length=200)


@router.put("/employees/{uid}/login")
def set_login(uid: int, body: LoginSetIn, admin: User = Depends(admin_user),
              db: Session = Depends(get_db)):
    """Works in both modes: SolNerve owns the person, Attend owns the app login."""
    u = _user(db, admin, uid)
    if db.scalars(select(User).where(User.phone == body.phone.strip(), User.id != u.id)).first():
        err("الرقم مستخدم")
    u.phone, u.password_hash = body.phone.strip(), hash_password(body.password)
    db.commit()
    return {"ok": True}


class BulkIn(BaseModel):
    text: str = Field(max_length=100_000)
    default_password: str = Field(min_length=6, max_length=200)
    plant_id: int | None = None


@router.post("/employees/bulk", dependencies=[Depends(local_people_only)])
def bulk_employees(body: BulkIn, admin: User = Depends(admin_user),
                   db: Session = Depends(get_db)):
    """One per line: name, phone, team (optional), job title (optional).
    A name that matches an existing person's known form is reported, not duplicated."""
    if body.plant_id:
        tenant_plant(db, admin, body.plant_id)
    teams = {t.name.strip().lower(): t for t in db.scalars(
        select(Team).join(Plant).where(Plant.tenant_id == admin.tenant_id))}
    created, errors = [], []
    pw = hash_password(body.default_password)
    for n, line in enumerate(body.text.splitlines(), 1):
        parts = [p.strip() for p in line.replace("،", ",").replace("\t", ",").split(",")]
        if not parts or not parts[0]:
            continue
        if len(parts) < 2 or len(parts[1]) < 6:
            errors.append(f"سطر {n}: لازم اسم ورقم هاتف")
            continue
        if db.scalars(select(User).where(User.phone == parts[1])).first():
            errors.append(f"سطر {n}: الرقم موجود")
            continue
        if names.lookup(db, admin.tenant_id, parts[0]):
            errors.append(f"سطر {n}: '{parts[0]}' موجود من قبل، ما انضاف مرة ثانية")
            continue
        team = teams.get(parts[2].lower()) if len(parts) > 2 and parts[2] else None
        if len(parts) > 2 and parts[2] and not team:
            errors.append(f"سطر {n}: الفريق '{parts[2]}' مش موجود، انضاف بدون فريق")
        u = User(tenant_id=admin.tenant_id, name=parts[0], phone=parts[1], password_hash=pw,
                 team_id=team.id if team else None,
                 plant_id=team.plant_id if team else body.plant_id,
                 job_title=parts[3] if len(parts) > 3 and parts[3] else None)
        db.add(u)
        db.flush()
        names.set_aliases(db, u, [])
        created.append(u.name)
    db.commit()
    return {"created": created, "errors": errors}


# ------------------------------------------------------------------ name questions
@router.get("/name-questions")
def name_questions(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    qs = db.scalars(select(NameQuestion).where(NameQuestion.tenant_id == admin.tenant_id,
                                               NameQuestion.status == "open")
                    .order_by(NameQuestion.created_at)).all()
    return [{"id": q.id, "form": q.form, "context": q.context} for q in qs]


class AnswerIn(BaseModel):
    user_id: int


@router.post("/name-questions/{qid}/answer")
def answer_question(qid: int, body: AnswerIn, admin: User = Depends(admin_user),
                    db: Session = Depends(get_db)):
    q = db.get(NameQuestion, qid)
    if not q or q.tenant_id != admin.tenant_id or q.status != "open":
        err("السؤال غير موجود", 404)
    try:
        names.answer(db, q, _user(db, admin, body.user_id))
    except names.AliasConflict as e:
        err(str(e))
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ live board
@router.get("/live")
def live(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    now = now_utc()
    plants = {p.id: p for p in db.scalars(select(Plant).where(Plant.tenant_id == admin.tenant_id))}
    people = []
    for u in db.scalars(select(User).where(User.tenant_id == admin.tenant_id,
                                           User.active.is_(True)).order_by(User.name)):
        plant = plants.get(u.plant_id)
        z = profiles.plant_tz(plant)
        last = attendance.last_event(db, u.id)
        p = db.scalars(select(LocationPing).where(LocationPing.user_id == u.id)
                       .order_by(LocationPing.ts.desc()).limit(1)).first()
        shift = db.scalars(select(Shift).where(Shift.user_id == u.id, Shift.start <= now,
                                               Shift.end > now)).first()
        lat, lng, at = (p.lat, p.lng, p.ts) if p else (None, None, None)
        if last and last.lat is not None and (not at or last.ts > at):
            lat, lng, at = last.lat, last.lng, last.ts
        st = last.type if last else None
        here = st not in (None, "depart")
        grace = plant.grace_minutes if plant else 0
        people.append({"id": u.id, "name": u.name, "plant_id": u.plant_id,
                       "team": u.team.name if u.team else None, "state": st,
                       "since": iso(last.ts, z) if last else None,
                       "on_shift": bool(shift), "shift": ser_shift(shift, z, plant),
                       "missing": bool(shift) and not here
                       and now > shift.start + timedelta(minutes=grace + 5),
                       "lat": lat if here else None, "lng": lng if here else None,
                       "location_at": iso(at, z) if here else None})
    uids = [p["id"] for p in people]
    zt = profiles.plant_tz(plants.get(admin.plant_id))
    alerts = db.scalars(select(Alert).where(Alert.resolved.is_(False), Alert.user_id.in_(uids))
                        .order_by(Alert.ts.desc())).all()
    pending = db.scalar(select(func.count(LeaveRequest.id)).where(
        LeaveRequest.status == "pending_admin", LeaveRequest.user_id.in_(uids)))
    flagged = db.scalars(select(Event).where(Event.flagged.is_(True), Event.user_id.in_(uids),
                                             Event.ts > now - timedelta(days=2))
                         .order_by(Event.ts.desc()).limit(20)).all()
    nm = {p["id"]: p["name"] for p in people}
    questions = db.scalar(select(func.count(NameQuestion.id)).where(
        NameQuestion.tenant_id == admin.tenant_id, NameQuestion.status == "open"))
    return {
        "people": people,
        "plants": [profiles.to_dict(p) for p in plants.values()],
        "alerts": [{"id": a.id, "kind": a.kind, "name": a.user.name, "ts": iso(a.ts, zt),
                    "lat": a.lat, "lng": a.lng, "note": a.note} for a in alerts],
        "pending_leaves": pending, "name_questions": questions,
        "flagged": [{**ser_event(e, zt), "name": nm.get(e.user_id)} for e in flagged],
    }


@router.post("/alerts/{aid}/resolve")
def resolve_alert(aid: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    a = db.get(Alert, aid)
    if not a or a.user.tenant_id != admin.tenant_id:
        err("غير موجود", 404)
    a.resolved, a.resolved_by = True, admin.id
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ reports (time only, no money)
def _report_rows(db: Session, admin: User, month: str | None, plant_id: int | None):
    q = select(User).where(User.tenant_id == admin.tenant_id, User.active.is_(True))
    if plant_id:
        tenant_plant(db, admin, plant_id)
        q = q.where(User.plant_id == plant_id)
    for u in db.scalars(q.order_by(User.name)):
        lo, hi = month_bounds(month, user_tz(db, u))
        yield u, attendance.summarize(attendance.compute(db, u, lo, hi))


@router.get("/report")
def report(month: str | None = None, plant_id: int | None = None,
           admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [{"user": ser_user(u), **s} for u, s in _report_rows(db, admin, month, plant_id)]


@router.get("/report/{uid}")
def report_user(uid: int, month: str | None = None, admin: User = Depends(admin_user),
                db: Session = Depends(get_db)):
    u = _user(db, admin, uid)
    plant = db.get(Plant, u.plant_id) if u.plant_id else None
    z = profiles.plant_tz(plant)
    lo, hi = month_bounds(month, z)
    res = attendance.compute(db, u, lo, hi)
    evs = db.scalars(select(Event).where(Event.user_id == uid, Event.ts >= lo, Event.ts < hi)
                     .order_by(Event.ts)).all()
    return {"user": ser_user(u), "summary": attendance.summarize(res),
            "days": [ser_result(r, z, plant) for r in res], "events": [ser_event(e, z) for e in evs]}


def _csv_cell(v):
    # Neutralise spreadsheet formula injection from user-controlled names.
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/report.csv")
def report_csv(month: str | None = None, plant_id: int | None = None,
               admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Attendance hours only. Personnel data never sits next to money figures."""
    buf = io.StringIO()
    buf.write("﻿")  # so Excel opens Arabic correctly
    w = csv.writer(buf)
    w.writerow(["الاسم", "personRef", "الفريق", "ساعات مجدولة", "ساعات فعلية", "ساعات بالموقع",
                "إضافي (ساعة)", "تأخير (دقيقة)", "خروج بدري (دقيقة)", "غياب", "شفتات إجازة",
                "نسي انطلاق"])
    for u, s in _report_rows(db, admin, month, plant_id):
        w.writerow([_csv_cell(x) for x in [
            u.name, u.person_ref or "", u.team.name if u.team else "", s["scheduled_hours"],
            s["worked_hours"], s["site_hours"], s["overtime_hours"], s["late_minutes"],
            s["early_minutes"], s["absences"], s["leave_shifts"], s["missing_departs"]]])
    label = month or to_local(now_utc(), user_tz(db, admin)).strftime("%Y-%m")
    if not (len(label) == 7 and label[4] == "-" and label.replace("-", "").isdigit()):
        err("صيغة الشهر لازم تكون YYYY-MM")
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition":
                                      f'attachment; filename="attendance-{label}.csv"'})
