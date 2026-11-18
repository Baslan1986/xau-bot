"""Solnerv Attend: HTTP API + static PWA."""
import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import attendance, config, geo, leaves, schedule
from .db import Base, engine, get_db
from .models import (Alert, Event, LeaveRequest, LocationPing, Notification, Shift, Station,
                     Team, User)
from .security import hash_password, limiter, verify_password
from .timeutil import iso, local_day_bounds_utc, local_to_utc, now_utc, to_local

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Solnerv Attend", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET, same_site="strict",
                   https_only=config.HTTPS_ONLY, max_age=60 * 60 * 24 * 30)
Base.metadata.create_all(engine)


@app.middleware("http")
async def guard(request: Request, call_next):
    # CSRF defence in depth on top of SameSite=strict: browsers won't send this header cross-site
    # without a CORS preflight, and we don't allow CORS.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
        if request.headers.get("x-requested-with") != "fetch":
            return JSONResponse({"detail": "bad request"}, status_code=400)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def err(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail=msg)


# ------------------------------------------------------------------ auth
def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.active:
        err("لازم تسجل دخول", 401)
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        err("هاي الصفحة للإدارة بس", 403)
    return user


class LoginIn(BaseModel):
    phone: str = Field(max_length=30)
    password: str = Field(max_length=200)


@app.post("/api/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if limiter.blocked(phone):
        err("محاولات كثيرة، جرب بعد ربع ساعة", 429)
    user = db.scalars(select(User).where(User.phone == phone)).first()
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        limiter.fail(phone)
        err("رقم الهاتف أو كلمة السر غلط", 401)
    limiter.reset(phone)
    request.session.clear()
    request.session["uid"] = user.id
    return {"ok": True, "role": user.role}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


class PasswordIn(BaseModel):
    old: str = Field(max_length=200)
    new: str = Field(min_length=6, max_length=200)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(body.old, user.password_hash):
        err("كلمة السر الحالية غلط")
    user.password_hash = hash_password(body.new)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ first-run setup wizard
@app.get("/api/setup")
def setup_status(db: Session = Depends(get_db)):
    return {"needed": db.scalar(select(func.count(User.id))) == 0}


class SetupIn(BaseModel):
    admin_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=30)
    password: str = Field(min_length=6, max_length=200)
    station_name: str = Field(min_length=2, max_length=120)
    polygon: str = Field(max_length=200_000)


@app.post("/api/setup")
def setup(body: SetupIn, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(func.count(User.id))):
        err("النظام متهيّأ من قبل", 409)
    try:
        poly = geo.parse_polygon(body.polygon)
    except geo.PolygonError as e:
        err(str(e))
    st = Station(name=body.station_name.strip(), polygon=json.dumps(poly))
    db.add(st)
    db.flush()
    admin = User(name=body.admin_name.strip(), phone=body.phone.strip(), role="admin",
                 password_hash=hash_password(body.password), station_id=st.id)
    db.add(admin)
    db.commit()
    request.session.clear()
    request.session["uid"] = admin.id
    return {"ok": True}


# ------------------------------------------------------------------ serializers
def ser_shift(s: Shift | None):
    if not s:
        return None
    return {"id": s.id, "kind": s.kind, "start": iso(s.start), "end": iso(s.end),
            "source": s.source, "user_id": s.user_id, "covering_for": s.covering_for}


def ser_event(e: Event):
    return {"id": e.id, "type": e.type, "label": attendance.LABEL[e.type], "ts": iso(e.ts),
            "lat": e.lat, "lng": e.lng, "flagged": e.flagged, "flag_reason": e.flag_reason,
            "offline": e.offline, "auto": e.auto}


def ser_leave(lr: LeaveRequest):
    return {"id": lr.id, "kind": lr.kind, "kind_label": leaves.KINDS[lr.kind],
            "start": iso(lr.start), "end": iso(lr.end), "amount": lr.amount,
            "reason": lr.reason, "status": lr.status, "admin_note": lr.admin_note,
            "user": {"id": lr.user_id, "name": lr.user.name},
            "substitute": ({"id": lr.substitute_id, "name": lr.substitute.name}
                           if lr.substitute_id else None),
            "substitute_status": lr.substitute_status, "created_at": iso(lr.created_at)}


def ser_user(u: User):
    return {"id": u.id, "name": u.name, "phone": u.phone, "role": u.role,
            "job_title": u.job_title, "station_id": u.station_id, "team_id": u.team_id,
            "team": u.team.name if u.team else None, "active": u.active,
            "annual_balance": u.annual_balance, "sick_balance": u.sick_balance,
            "departure_hours_balance": u.departure_hours_balance}


def ser_result(r: attendance.ShiftResult):
    return {"shift": ser_shift(r.shift), "arrive": iso(r.arrive), "depart": iso(r.depart),
            "worked_min": r.worked_min, "site_min": r.site_min, "late_min": r.late_min,
            "early_min": r.early_min, "overtime_min": r.overtime_min, "status": r.status,
            "notes": r.notes}


def parse_local(s: str) -> datetime:
    try:
        return local_to_utc(datetime.fromisoformat(s))
    except ValueError:
        err("صيغة التاريخ غلط")


def month_bounds(month: str | None) -> tuple[datetime, datetime]:
    today = to_local(now_utc()).date()
    if month:
        try:
            y, m = map(int, month.split("-"))
            first = date(y, m, 1)
        except ValueError:
            err("صيغة الشهر لازم تكون YYYY-MM")
    else:
        first = today.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return local_day_bounds_utc(first)[0], local_day_bounds_utc(nxt)[0]


# ------------------------------------------------------------------ employee
@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = now_utc()
    last = attendance.last_event(db, user.id)
    st = last.type if last else None
    cur = db.scalars(select(Shift).where(Shift.user_id == user.id,
                                         Shift.end > now - timedelta(hours=2))
                     .order_by(Shift.start).limit(2)).all()
    lo, hi = month_bounds(None)
    summary = attendance.summarize(attendance.compute(db, user, lo, hi))
    station = db.get(Station, user.station_id) if user.station_id else None
    streak = 0
    for r in reversed(attendance.compute(db, user, now - timedelta(days=60), now)):
        if r.status == "present" and r.late_min == 0:
            streak += 1
        elif r.status in ("upcoming", "in_progress", "leave", "unscheduled"):
            continue
        else:
            break
    return {
        "user": ser_user(user),
        "station": ({"id": station.id, "name": station.name,
                     "polygon": geo.load(station.polygon),
                     "site_polygon": geo.load(station.site_polygon)} if station else None),
        "state": st, "state_since": iso(last.ts) if last else None,
        "allowed": attendance.allowed(st),
        "shifts": [ser_shift(s) for s in cur],
        "month": summary, "streak": streak,
        "unread": db.scalar(select(func.count(Notification.id)).where(
            Notification.user_id == user.id, Notification.read.is_(False))),
        "pending_cover": db.scalar(select(func.count(LeaveRequest.id)).where(
            LeaveRequest.substitute_id == user.id,
            LeaveRequest.status == "pending_substitute")),
    }


class EventIn(BaseModel):
    type: str
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    client_ts: datetime | None = None  # local time when queued offline
    offline: bool = False
    mocked: bool = False


def _record(db: Session, user: User, body: EventIn):
    ts = None
    if body.offline and body.client_ts:
        ct = body.client_ts
        # Phones send UTC with an offset; a naive value is read as station-local time.
        ts = (ct.astimezone(timezone.utc).replace(tzinfo=None) if ct.tzinfo
              else local_to_utc(ct))
        now = now_utc()
        if ts > now + timedelta(minutes=5) or ts < now - timedelta(hours=config.OFFLINE_MAX_HOURS):
            raise attendance.EventError("وقت الحركة المحفوظة غير مقبول")
    return attendance.record(db, user, body.type, body.lat, body.lng, body.accuracy, ts=ts,
                             offline=body.offline, mocked=body.mocked)


@app.post("/api/events")
def post_event(body: EventIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        evs = _record(db, user, body)
    except attendance.EventError as e:
        err(str(e))
    return {"events": [ser_event(e) for e in evs], "state": evs[-1].type,
            "allowed": attendance.allowed(evs[-1].type)}


class SyncIn(BaseModel):
    events: list[EventIn] = Field(max_length=200)


@app.post("/api/events/sync")
def sync_events(body: SyncIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    results = []
    def order(e: EventIn):
        ct = e.client_ts
        if ct is None:
            return datetime.min
        return ct.astimezone(timezone.utc).replace(tzinfo=None) if ct.tzinfo else local_to_utc(ct)

    for ev in sorted(body.events, key=order):
        ev.offline = True
        try:
            _record(db, user, ev)
            results.append({"ok": True})
        except attendance.EventError as e:
            db.rollback()
            results.append({"ok": False, "error": str(e)})
    return {"results": results}


@app.get("/api/me/events")
def my_events(days: int = 7, user: User = Depends(current_user), db: Session = Depends(get_db)):
    since = now_utc() - timedelta(days=min(days, 62))
    evs = db.scalars(select(Event).where(Event.user_id == user.id, Event.ts >= since)
                     .order_by(Event.ts.desc())).all()
    return [ser_event(e) for e in evs]


@app.get("/api/me/attendance")
def my_attendance(month: str | None = None, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    lo, hi = month_bounds(month)
    res = attendance.compute(db, user, lo, hi)
    return {"summary": attendance.summarize(res), "days": [ser_result(r) for r in res]}


@app.get("/api/me/schedule")
def my_schedule(days: int = 30, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    lo = local_day_bounds_utc(to_local(now_utc()).date())[0]
    shifts = db.scalars(select(Shift).where(Shift.user_id == user.id, Shift.start >= lo,
                                            Shift.start < lo + timedelta(days=min(days, 90)))
                        .order_by(Shift.start)).all()
    return [ser_shift(s) for s in shifts]


class PingIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


@app.post("/api/me/ping")
def ping(body: PingIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Live location, sent by the app only while the employee is checked in (privacy)."""
    st = attendance.state(db, user.id)
    if st in (None, "depart"):
        return {"tracking": False}
    db.add(LocationPing(user_id=user.id, lat=body.lat, lng=body.lng, accuracy=body.accuracy))
    station = db.get(Station, user.station_id)
    warn = None
    if station and not geo.point_in_polygon(body.lat, body.lng, geo.load(station.polygon)) \
            and (body.accuracy or 0) < 100:
        warn = "شكلك طلعت من المحطة، لا تنسى تسجل انطلاق"
        recent = db.scalars(select(Alert).where(Alert.user_id == user.id,
                                                Alert.kind == "left_geofence",
                                                Alert.ts > now_utc() - timedelta(hours=1))).first()
        if not recent:
            db.add(Alert(user_id=user.id, kind="left_geofence", lat=body.lat, lng=body.lng,
                         note="طلع من حدود المحطة بدون ما يسجل انطلاق"))
    db.commit()
    return {"tracking": True, "warning": warn}


class SosIn(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=500)


@app.post("/api/me/sos")
def sos(body: SosIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.add(Alert(user_id=user.id, kind="sos", lat=body.lat, lng=body.lng, note=body.note))
    where = f" الموقع: https://maps.google.com/?q={body.lat},{body.lng}" if body.lat else ""
    leaves.notify_admins(db, f"🆘 طوارئ من {user.name}!{where}")
    # Teammates currently at the same station are closest to help.
    for u in db.scalars(select(User).where(User.station_id == user.station_id,
                                           User.id != user.id, User.active.is_(True))):
        if attendance.state(db, u.id) not in (None, "depart") and u.role != "admin":
            leaves.notify(db, u.id, f"🆘 زميلك {user.name} طلب مساعدة طارئة.{where}")
    db.commit()
    return {"ok": True}


@app.get("/api/me/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ns = db.scalars(select(Notification).where(Notification.user_id == user.id)
                    .order_by(Notification.ts.desc()).limit(50)).all()
    out = [{"id": n.id, "text": n.text, "ts": iso(n.ts), "read": n.read} for n in ns]
    for n in ns:
        n.read = True
    db.commit()
    return out


# ------------------------------------------------------------------ leave requests
class LeaveIn(BaseModel):
    kind: str
    start: str
    end: str
    substitute_id: int | None = None
    reason: str | None = Field(default=None, max_length=1000)


@app.get("/api/leaves/candidates")
def leave_candidates(kind: str, start: str, end: str, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    return leaves.candidates(db, user, kind, parse_local(start), parse_local(end))


@app.post("/api/leaves")
def create_leave(body: LeaveIn, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    try:
        lr = leaves.create(db, user, body.kind, parse_local(body.start), parse_local(body.end),
                           body.substitute_id, body.reason)
    except leaves.LeaveError as e:
        err(str(e))
    return ser_leave(lr)


@app.get("/api/leaves/mine")
def my_leaves(user: User = Depends(current_user), db: Session = Depends(get_db)):
    mine = db.scalars(select(LeaveRequest).where(LeaveRequest.user_id == user.id)
                      .order_by(LeaveRequest.created_at.desc()).limit(50)).all()
    cover = db.scalars(select(LeaveRequest).where(LeaveRequest.substitute_id == user.id)
                       .order_by(LeaveRequest.created_at.desc()).limit(50)).all()
    return {"mine": [ser_leave(x) for x in mine], "cover": [ser_leave(x) for x in cover]}


def _leave(db: Session, lid: int) -> LeaveRequest:
    lr = db.get(LeaveRequest, lid)
    if not lr:
        err("الطلب غير موجود", 404)
    return lr


class RespondIn(BaseModel):
    accept: bool


@app.post("/api/leaves/{lid}/respond")
def respond_leave(lid: int, body: RespondIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    try:
        return ser_leave(leaves.substitute_respond(db, user, _leave(db, lid), body.accept))
    except leaves.LeaveError as e:
        err(str(e))


@app.post("/api/leaves/{lid}/cancel")
def cancel_leave(lid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        return ser_leave(leaves.cancel(db, user, _leave(db, lid)))
    except leaves.LeaveError as e:
        err(str(e))


# ------------------------------------------------------------------ admin
class DecideIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=1000)
    substitute_id: int | None = None


@app.get("/api/admin/leaves")
def admin_leaves(status: str | None = None, admin: User = Depends(admin_user),
                 db: Session = Depends(get_db)):
    q = select(LeaveRequest).order_by(LeaveRequest.created_at.desc()).limit(200)
    if status:
        q = q.where(LeaveRequest.status == status)
    return [ser_leave(x) for x in db.scalars(q)]


@app.post("/api/admin/leaves/{lid}/decide")
def decide_leave(lid: int, body: DecideIn, admin: User = Depends(admin_user),
                 db: Session = Depends(get_db)):
    try:
        return ser_leave(leaves.decide(db, admin, _leave(db, lid), body.approve, body.note,
                                       body.substitute_id))
    except leaves.LeaveError as e:
        err(str(e))


class StationIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    polygon: str = Field(max_length=200_000)
    site_polygon: str | None = Field(default=None, max_length=200_000)
    min_staff: int = Field(default=1, ge=0, le=50)
    day_start: str = Field(default="07:00", pattern=r"^\d{2}:\d{2}$")
    night_start: str = Field(default="19:00", pattern=r"^\d{2}:\d{2}$")
    shift_hours: int = Field(default=12, ge=1, le=24)
    grace_minutes: int = Field(default=10, ge=0, le=120)


def ser_station(s: Station):
    return {"id": s.id, "name": s.name, "polygon": geo.load(s.polygon),
            "site_polygon": geo.load(s.site_polygon), "min_staff": s.min_staff,
            "day_start": s.day_start, "night_start": s.night_start,
            "shift_hours": s.shift_hours, "grace_minutes": s.grace_minutes}


def _apply_station(s: Station, body: StationIn):
    try:
        s.polygon = json.dumps(geo.parse_polygon(body.polygon))
        s.site_polygon = (json.dumps(geo.parse_polygon(body.site_polygon))
                          if body.site_polygon and body.site_polygon.strip() else None)
    except geo.PolygonError as e:
        err(str(e))
    s.name, s.min_staff = body.name.strip(), body.min_staff
    s.day_start, s.night_start = body.day_start, body.night_start
    s.shift_hours, s.grace_minutes = body.shift_hours, body.grace_minutes


@app.get("/api/admin/stations")
def list_stations(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [ser_station(s) for s in db.scalars(select(Station))]


@app.post("/api/admin/stations")
def create_station(body: StationIn, admin: User = Depends(admin_user),
                   db: Session = Depends(get_db)):
    s = Station(name="", polygon="[]")
    _apply_station(s, body)
    db.add(s)
    db.commit()
    return ser_station(s)


@app.put("/api/admin/stations/{sid}")
def update_station(sid: int, body: StationIn, admin: User = Depends(admin_user),
                   db: Session = Depends(get_db)):
    s = db.get(Station, sid) or err("المحطة غير موجودة", 404)
    _apply_station(s, body)
    db.commit()
    return ser_station(s)


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    station_id: int
    pattern: str = Field(max_length=200)
    anchor_date: date


def ser_team(t: Team):
    return {"id": t.id, "name": t.name, "station_id": t.station_id, "pattern": t.pattern,
            "anchor_date": t.anchor_date.isoformat()}


@app.get("/api/admin/teams")
def list_teams(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [ser_team(t) for t in db.scalars(select(Team))]


def _apply_team(db: Session, t: Team, body: TeamIn):
    try:
        t.pattern = ",".join(schedule.parse_pattern(body.pattern))
    except ValueError as e:
        err(str(e))
    if not db.get(Station, body.station_id):
        err("المحطة غير موجودة")
    t.name, t.station_id, t.anchor_date = body.name.strip(), body.station_id, body.anchor_date


@app.post("/api/admin/teams")
def create_team(body: TeamIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    t = Team(name="", station_id=body.station_id, anchor_date=body.anchor_date)
    _apply_team(db, t, body)
    db.add(t)
    db.commit()
    return ser_team(t)


@app.put("/api/admin/teams/{tid}")
def update_team(tid: int, body: TeamIn, admin: User = Depends(admin_user),
                db: Session = Depends(get_db)):
    t = db.get(Team, tid) or err("الفريق غير موجود", 404)
    _apply_team(db, t, body)
    db.commit()
    return ser_team(t)


class GenerateIn(BaseModel):
    team_id: int
    start: date
    days: int = Field(default=30, ge=1, le=120)


@app.post("/api/admin/schedule/generate")
def generate_schedule(body: GenerateIn, admin: User = Depends(admin_user),
                      db: Session = Depends(get_db)):
    t = db.get(Team, body.team_id) or err("الفريق غير موجود", 404)
    n = schedule.generate(db, t, body.start, body.start + timedelta(days=body.days - 1))
    return {"created": n}


@app.get("/api/admin/schedule")
def get_schedule(station_id: int, start: date, days: int = 14,
                 admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    station = db.get(Station, station_id) or err("المحطة غير موجودة", 404)
    lo = local_day_bounds_utc(start)[0]
    hi = lo + timedelta(days=min(days, 62))
    shifts = db.scalars(select(Shift).where(Shift.station_id == station_id, Shift.start >= lo,
                                            Shift.start < hi).order_by(Shift.start)).all()
    names = {u.id: u.name for u in db.scalars(select(User))}
    return {
        "shifts": [{**ser_shift(s), "name": names.get(s.user_id),
                    "covering_for_name": names.get(s.covering_for)} for s in shifts],
        "gaps": [{"start": iso(g["start"]), "kind": g["kind"], "count": g["count"],
                  "needed": g["needed"]} for g in schedule.coverage_gaps(db, station, lo, hi)],
    }


class ShiftIn(BaseModel):
    user_id: int
    kind: str = Field(pattern="^[DN]$")
    day: date


@app.post("/api/admin/shifts")
def add_shift(body: ShiftIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    u = db.get(User, body.user_id) or err("الموظف غير موجود", 404)
    if not u.station_id:
        err("الموظف ما إله محطة")
    s, e = schedule.shift_window(db.get(Station, u.station_id), body.day, body.kind)
    if db.scalars(select(Shift).where(Shift.user_id == u.id, Shift.start < e,
                                      Shift.end > s)).first():
        err("عنده شفت بنفس الوقت")
    sh = Shift(user_id=u.id, station_id=u.station_id, kind=body.kind, start=s, end=e,
               source="manual")
    db.add(sh)
    leaves.notify(db, u.id, f"انضاف إلك شفت {'نهاري' if body.kind == 'D' else 'ليلي'} {iso(s)}")
    db.commit()
    return ser_shift(sh)


@app.delete("/api/admin/shifts/{sid}")
def delete_shift(sid: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    s = db.get(Shift, sid) or err("الشفت غير موجود", 404)
    db.delete(s)
    db.commit()
    return {"ok": True}


class EmployeeIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=30)
    password: str | None = Field(default=None, min_length=6, max_length=200)
    role: str = Field(default="employee", pattern="^(employee|admin)$")
    job_title: str | None = Field(default=None, max_length=80)
    station_id: int | None = None
    team_id: int | None = None
    active: bool = True
    annual_balance: float = Field(default=14, ge=0, le=365)
    sick_balance: float = Field(default=14, ge=0, le=365)
    departure_hours_balance: float = Field(default=8, ge=0, le=500)


def _apply_employee(db: Session, u: User, body: EmployeeIn):
    phone = body.phone.strip()
    other = db.scalars(select(User).where(User.phone == phone, User.id != (u.id or 0))).first()
    if other:
        err(f"الرقم {phone} مستخدم لـ {other.name}")
    if body.team_id:
        t = db.get(Team, body.team_id) or err("الفريق غير موجود")
        body.station_id = t.station_id
    for k in ("name", "role", "job_title", "station_id", "team_id", "active",
              "annual_balance", "sick_balance", "departure_hours_balance"):
        setattr(u, k, getattr(body, k))
    u.name, u.phone = body.name.strip(), phone
    if body.password:
        u.password_hash = hash_password(body.password)


@app.get("/api/admin/employees")
def list_employees(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [ser_user(u) for u in db.scalars(select(User).order_by(User.name))]


@app.post("/api/admin/employees")
def create_employee(body: EmployeeIn, admin: User = Depends(admin_user),
                    db: Session = Depends(get_db)):
    if not body.password:
        err("لازم كلمة سر أولية (6 خانات على الأقل)")
    u = User(name="", phone="", password_hash="")
    _apply_employee(db, u, body)
    db.add(u)
    db.commit()
    return ser_user(u)


@app.put("/api/admin/employees/{uid}")
def update_employee(uid: int, body: EmployeeIn, admin: User = Depends(admin_user),
                    db: Session = Depends(get_db)):
    u = db.get(User, uid) or err("الموظف غير موجود", 404)
    if u.id == admin.id and (body.role != "admin" or not body.active):
        err("ما بتقدر تشيل صلاحيتك أو توقف حسابك")
    _apply_employee(db, u, body)
    db.commit()
    return ser_user(u)


class BulkIn(BaseModel):
    text: str = Field(max_length=100_000)
    default_password: str = Field(min_length=6, max_length=200)
    station_id: int | None = None


@app.post("/api/admin/employees/bulk")
def bulk_employees(body: BulkIn, admin: User = Depends(admin_user),
                   db: Session = Depends(get_db)):
    """One employee per line: name, phone, team name (optional), job title (optional)."""
    teams = {t.name.strip().lower(): t for t in db.scalars(select(Team))}
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
            errors.append(f"سطر {n}: الرقم {parts[1]} موجود")
            continue
        team = teams.get(parts[2].lower()) if len(parts) > 2 and parts[2] else None
        if len(parts) > 2 and parts[2] and not team:
            errors.append(f"سطر {n}: الفريق '{parts[2]}' مش موجود، انضاف بدون فريق")
        u = User(name=parts[0], phone=parts[1], password_hash=pw,
                 team_id=team.id if team else None,
                 station_id=team.station_id if team else body.station_id,
                 job_title=parts[3] if len(parts) > 3 and parts[3] else None)
        db.add(u)
        db.flush()
        created.append(u.name)
    db.commit()
    return {"created": created, "errors": errors}


@app.get("/api/admin/live")
def live(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Who is where right now, plus open alerts and today's coverage."""
    now = now_utc()
    people = []
    for u in db.scalars(select(User).where(User.active.is_(True)).order_by(User.name)):
        last = attendance.last_event(db, u.id)
        p = db.scalars(select(LocationPing).where(LocationPing.user_id == u.id)
                       .order_by(LocationPing.ts.desc()).limit(1)).first()
        shift = db.scalars(select(Shift).where(Shift.user_id == u.id, Shift.start <= now,
                                               Shift.end > now)).first()
        lat, lng, at = (p.lat, p.lng, p.ts) if p else (None, None, None)
        if last and last.lat is not None and (not at or last.ts > at):
            lat, lng, at = last.lat, last.lng, last.ts
        st = last.type if last else None
        people.append({"id": u.id, "name": u.name, "station_id": u.station_id,
                       "team": u.team.name if u.team else None, "state": st,
                       "since": iso(last.ts) if last else None,
                       "on_shift": bool(shift), "shift": ser_shift(shift),
                       "missing": bool(shift) and st in (None, "depart")
                       and now > shift.start + timedelta(minutes=15),
                       "lat": lat if st not in (None, "depart") else None,
                       "lng": lng if st not in (None, "depart") else None,
                       "location_at": iso(at) if st not in (None, "depart") else None})
    alerts = db.scalars(select(Alert).where(Alert.resolved.is_(False))
                        .order_by(Alert.ts.desc())).all()
    pending = db.scalar(select(func.count(LeaveRequest.id)).where(
        LeaveRequest.status == "pending_admin"))
    flagged = db.scalars(select(Event).where(Event.flagged.is_(True),
                                             Event.ts > now - timedelta(days=2))
                         .order_by(Event.ts.desc()).limit(20)).all()
    names = {u["id"]: u["name"] for u in people}
    return {
        "people": people,
        "stations": [ser_station(s) for s in db.scalars(select(Station))],
        "alerts": [{"id": a.id, "kind": a.kind, "name": a.user.name, "ts": iso(a.ts),
                    "lat": a.lat, "lng": a.lng, "note": a.note} for a in alerts],
        "pending_leaves": pending,
        "flagged": [{**ser_event(e), "name": names.get(e.user_id)} for e in flagged],
    }


@app.post("/api/admin/alerts/{aid}/resolve")
def resolve_alert(aid: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    a = db.get(Alert, aid) or err("غير موجود", 404)
    a.resolved, a.resolved_by = True, admin.id
    db.commit()
    return {"ok": True}


def _report_rows(db: Session, lo: datetime, hi: datetime, station_id: int | None):
    q = select(User).where(User.active.is_(True)).order_by(User.name)
    if station_id:
        q = q.where(User.station_id == station_id)
    for u in db.scalars(q):
        yield u, attendance.summarize(attendance.compute(db, u, lo, hi))


@app.get("/api/admin/report")
def report(month: str | None = None, station_id: int | None = None,
           admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    lo, hi = month_bounds(month)
    return [{"user": ser_user(u), **s} for u, s in _report_rows(db, lo, hi, station_id)]


@app.get("/api/admin/report/{uid}")
def report_user(uid: int, month: str | None = None, admin: User = Depends(admin_user),
                db: Session = Depends(get_db)):
    u = db.get(User, uid) or err("الموظف غير موجود", 404)
    lo, hi = month_bounds(month)
    res = attendance.compute(db, u, lo, hi)
    evs = db.scalars(select(Event).where(Event.user_id == uid, Event.ts >= lo, Event.ts < hi)
                     .order_by(Event.ts)).all()
    return {"user": ser_user(u), "summary": attendance.summarize(res),
            "days": [ser_result(r) for r in res], "events": [ser_event(e) for e in evs]}


def _csv_cell(v):
    # Neutralise spreadsheet formula injection from user-controlled names.
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@app.get("/api/admin/report.csv")
def report_csv(month: str | None = None, station_id: int | None = None,
               admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    lo, hi = month_bounds(month)
    buf = io.StringIO()
    buf.write("﻿")  # so Excel opens Arabic correctly
    w = csv.writer(buf)
    w.writerow(["الاسم", "الهاتف", "الفريق", "ساعات مجدولة", "ساعات فعلية", "ساعات بالموقع",
                "إضافي (ساعة)", "تأخير (دقيقة)", "خروج بدري (دقيقة)", "غياب", "شفتات إجازة",
                "نسي انطلاق", "رصيد سنوي", "رصيد مرضي", "رصيد مغادرات (ساعة)"])
    for u, s in _report_rows(db, lo, hi, station_id):
        w.writerow([_csv_cell(x) for x in [
            u.name, u.phone, u.team.name if u.team else "", s["scheduled_hours"],
            s["worked_hours"], s["site_hours"], s["overtime_hours"], s["late_minutes"],
            s["early_minutes"], s["absences"], s["leave_shifts"], s["missing_departs"],
            u.annual_balance, u.sick_balance, u.departure_hours_balance]])
    name = f"attendance-{month or to_local(now_utc()).strftime('%Y-%m')}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ static PWA
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
