"""Machine-to-machine API for SolNerve and Shams. Contract: docs/ATTEND_API.md.

SolNerve is the single source of people and leave. Attend owns plant profiles, shifts,
clock events and presence, and answers interlock checks. People are referenced by personRef
(SolNerve's id), never by name. Attendance figures carry no money and no names.
"""
import hmac
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import attendance, config, leaves, names, profiles
from .db import get_db
from .deps import err, month_bounds
from .models import NameQuestion, Plant, Team, User
from .security import hash_password
from .timeutil import aware_to_utc, iso

router = APIRouter(prefix="/api/integration")
ADMIN_ROLES = {"attend_admin", "admin", "supervisor"}


def require_token(authorization: str = Header(default="")):
    if not config.INTEGRATION_TOKEN:
        err("integration disabled", 503)
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), config.INTEGRATION_TOKEN.encode()):
        err("unauthorized", 401)


guard = [Depends(require_token)]


def _plant(db: Session, tenant: str, plant_ref: str) -> Plant:
    p = db.scalars(select(Plant).where(Plant.tenant_id == tenant,
                                       Plant.plant_ref == plant_ref)).first()
    if not p:
        err(f"unknown plant {plant_ref}", 404)
    return p


def _person(db: Session, tenant: str, ref: str) -> User:
    u = db.scalars(select(User).where(User.tenant_id == tenant, User.person_ref == ref)).first()
    if not u:
        err(f"unknown personRef {ref}", 404)
    return u


# ------------------------------------------------------------------ plant profiles
@router.put("/plants", dependencies=guard)
def put_plant(profile: dict, db: Session = Depends(get_db)):
    try:
        return profiles.to_dict(profiles.upsert(db, profile))
    except (profiles.ProfileError, ValueError, TypeError) as e:
        err(str(e))


# ------------------------------------------------------------------ people (from SolNerve)
class PersonIn(BaseModel):
    personRef: str = Field(min_length=1, max_length=64)
    displayName: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    roles: list[str] = Field(default_factory=list, max_length=20)
    plantId: str | None = Field(default=None, max_length=64)
    team: str | None = Field(default=None, max_length=60)
    jobTitle: str | None = Field(default=None, max_length=80)
    active: bool = True
    loginPhone: str | None = Field(default=None, min_length=6, max_length=30)
    balances: dict[str, float] | None = None  # annual, sick, departureHours (mirrored)


class PeopleIn(BaseModel):
    tenantId: str = Field(max_length=64)
    people: list[PersonIn] = Field(max_length=2000)


@router.put("/people", dependencies=guard)
def put_people(body: PeopleIn, db: Session = Depends(get_db)):
    done, errors, unknown_teams = 0, [], set()
    for p in body.people:
        try:
            u = db.scalars(select(User).where(User.tenant_id == body.tenantId,
                                              User.person_ref == p.personRef)).first()
            if u is None:
                u = User(tenant_id=body.tenantId, person_ref=p.personRef,
                         phone=f"unset:{secrets.token_hex(8)}",
                         password_hash=hash_password(secrets.token_hex(16)))
                db.add(u)
            plant = _plant(db, body.tenantId, p.plantId) if p.plantId else None
            u.name, u.active, u.job_title = p.displayName.strip(), p.active, p.jobTitle
            u.role = "admin" if ADMIN_ROLES & {r.lower() for r in p.roles} else "employee"
            u.plant_id = plant.id if plant else None
            u.team_id = None
            if p.team and plant:
                t = db.scalars(select(Team).where(Team.plant_id == plant.id,
                                                  Team.name == p.team)).first()
                if t:
                    u.team_id = t.id
                else:
                    unknown_teams.add(p.team)
            if p.loginPhone:
                clash = db.scalars(select(User).where(User.phone == p.loginPhone,
                                                      User.id != (u.id or 0))).first()
                if clash:
                    raise ValueError("loginPhone already used")
                u.phone = p.loginPhone
            if p.balances:
                u.annual_balance = p.balances.get("annual", u.annual_balance)
                u.sick_balance = p.balances.get("sick", u.sick_balance)
                u.departure_hours_balance = p.balances.get("departureHours",
                                                           u.departure_hours_balance)
            db.flush()
            names.set_aliases(db, u, p.aliases)
            db.commit()
            done += 1
        except Exception as e:  # report per person, keep going; no personal data in the message
            db.rollback()
            msg = e.detail if hasattr(e, "detail") else str(e)
            errors.append({"personRef": p.personRef, "error": msg})
    return {"upserted": done, "errors": errors, "unknownTeams": sorted(unknown_teams)}


# ------------------------------------------------------------------ leave (from SolNerve)
class LeaveSyncItem(BaseModel):
    leaveRef: str = Field(min_length=1, max_length=64)
    personRef: str = Field(max_length=64)
    kind: str
    start: datetime
    end: datetime
    status: str = Field(pattern="^(pending|approved|rejected|cancelled)$")
    substituteRef: str | None = Field(default=None, max_length=64)


class LeaveSyncIn(BaseModel):
    tenantId: str = Field(max_length=64)
    leaves: list[LeaveSyncItem] = Field(max_length=1000)


@router.put("/leaves", dependencies=guard)
def put_leaves(body: LeaveSyncIn, db: Session = Depends(get_db)):
    out = []
    for item in body.leaves:
        try:
            u = _person(db, body.tenantId, item.personRef)
            sub = _person(db, body.tenantId, item.substituteRef) if item.substituteRef else None
            z = profiles.plant_tz(db.get(Plant, u.plant_id) if u.plant_id else None)
            lr = leaves.apply_external(db, u, sub, item.kind, aware_to_utc(item.start, z),
                                       aware_to_utc(item.end, z), item.status, item.leaveRef)
            db.commit()
            out.append({"leaveRef": item.leaveRef, "ok": True, "applied": lr.applied,
                        "shiftsMoved": int(lr.amount) if lr.kind != "departure" else 0})
        except Exception as e:
            db.rollback()
            out.append({"leaveRef": item.leaveRef, "ok": False,
                        "error": e.detail if hasattr(e, "detail") else str(e)})
    return {"results": out}


class InterlockIn(BaseModel):
    tenantId: str = Field(max_length=64)
    personRef: str = Field(max_length=64)
    kind: str
    start: datetime
    end: datetime
    substituteRef: str | None = Field(default=None, max_length=64)


@router.post("/interlock/check", dependencies=guard)
def interlock(body: InterlockIn, db: Session = Depends(get_db)):
    u = _person(db, body.tenantId, body.personRef)
    z = profiles.plant_tz(db.get(Plant, u.plant_id) if u.plant_id else None)
    start, end = aware_to_utc(body.start, z), aware_to_utc(body.end, z)
    try:
        need = leaves.amount(db, u, body.kind, start, end) if body.kind in leaves.KINDS else None
    except leaves.LeaveError as e:
        return {"ok": False, "reason": str(e)}
    if body.substituteRef:
        sub = _person(db, body.tenantId, body.substituteRef)
        why = leaves.substitute_problem(db, u, sub, body.kind, start, end)
        return {"ok": why is None, "reason": why, "amount": need}
    refs = {x.id: x.person_ref for x in db.scalars(select(User).where(
        User.tenant_id == body.tenantId, User.plant_id == u.plant_id))}
    return {"ok": None, "amount": need, "candidates": [
        {"personRef": refs.get(c["id"]), "ok": c["ok"], "reason": c["reason"]}
        for c in leaves.candidates(db, u, body.kind, start, end) if refs.get(c["id"])]}


# ------------------------------------------------------------------ attendance out (no names, no money)
@router.get("/attendance", dependencies=guard)
def attendance_out(tenantId: str, month: str | None = None, plantId: str | None = None,
                   db: Session = Depends(get_db)):
    q = select(User).where(User.tenant_id == tenantId, User.person_ref.is_not(None))
    if plantId:
        q = q.where(User.plant_id == _plant(db, tenantId, plantId).id)
    out = []
    for u in db.scalars(q):
        z = profiles.plant_tz(db.get(Plant, u.plant_id) if u.plant_id else None)
        lo, hi = month_bounds(month, z)
        out.append({"personRef": u.person_ref,
                    **attendance.summarize(attendance.compute(db, u, lo, hi))})
    return {"tenantId": tenantId, "month": month, "people": out}


@router.get("/presence", dependencies=guard)
def presence(tenantId: str, plantId: str, db: Session = Depends(get_db)):
    plant = _plant(db, tenantId, plantId)
    z = profiles.plant_tz(plant)
    out = []
    for u in db.scalars(select(User).where(User.plant_id == plant.id, User.active.is_(True),
                                           User.person_ref.is_not(None))):
        last = attendance.last_event(db, u.id)
        out.append({"personRef": u.person_ref, "state": last.type if last else None,
                    "since": iso(last.ts, z) if last else None})
    return {"plantId": plantId, "people": out}


# ------------------------------------------------------------------ names
class ResolveIn(BaseModel):
    tenantId: str = Field(max_length=64)
    form: str = Field(min_length=1, max_length=120)
    context: str | None = Field(default=None, max_length=200)


@router.post("/names/resolve", dependencies=guard)
def resolve_name(body: ResolveIn, db: Session = Depends(get_db)):
    r = names.resolve(db, body.tenantId, body.form, body.context)
    db.commit()
    return r


@router.get("/names/questions", dependencies=guard)
def list_questions(tenantId: str, db: Session = Depends(get_db)):
    qs = db.scalars(select(NameQuestion).where(NameQuestion.tenant_id == tenantId,
                                               NameQuestion.status == "open")).all()
    return [{"questionId": q.id, "form": q.form, "context": q.context} for q in qs]


class AnswerIn(BaseModel):
    tenantId: str = Field(max_length=64)
    personRef: str = Field(max_length=64)


@router.post("/names/questions/{qid}/answer", dependencies=guard)
def answer(qid: int, body: AnswerIn, db: Session = Depends(get_db)):
    q = db.get(NameQuestion, qid)
    if not q or q.tenant_id != body.tenantId or q.status != "open":
        err("unknown question", 404)
    try:
        names.answer(db, q, _person(db, body.tenantId, body.personRef))
    except names.AliasConflict as e:
        err(str(e))
    db.commit()
    return {"ok": True}
