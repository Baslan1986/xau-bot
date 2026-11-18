"""Shared request dependencies and serializers."""
from datetime import date, datetime, timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import attendance, config, leaves, profiles
from .db import get_db
from .models import Event, LeaveRequest, Plant, Shift, User
from .timeutil import iso, local_day_bounds_utc, now_utc, to_local


def err(msg: str, code: int = 400):
    raise HTTPException(status_code=code, detail=msg)


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


def local_people_only():
    if config.PEOPLE_SOURCE == "solnerve":
        err("الموظفين والإجازات بتنداروا من SolNerve", 409)


def tenant_plant(db: Session, admin: User, plant_id: int) -> Plant:
    p = db.get(Plant, plant_id)
    if not p or p.tenant_id != admin.tenant_id:
        err("المحطة غير موجودة", 404)
    return p


def user_tz(db: Session, u: User):
    return profiles.plant_tz(db.get(Plant, u.plant_id) if u.plant_id else None)


def shift_label(plant: Plant | None, code: str) -> str:
    if plant:
        for s in profiles.shifts(plant):
            if s["code"] == code:
                return s["label"]
    return code


def ser_shift(s: Shift | None, z, plant: Plant | None = None):
    if not s:
        return None
    return {"id": s.id, "code": s.code, "label": shift_label(plant, s.code),
            "start": iso(s.start, z), "end": iso(s.end, z), "source": s.source,
            "user_id": s.user_id, "covering_for": s.covering_for}


def ser_event(e: Event, z):
    return {"id": e.id, "type": e.type, "label": attendance.LABEL[e.type], "ts": iso(e.ts, z),
            "lat": e.lat, "lng": e.lng, "flagged": e.flagged, "flag_reason": e.flag_reason,
            "offline": e.offline, "auto": e.auto}


def ser_leave(lr: LeaveRequest, z):
    return {"id": lr.id, "kind": lr.kind, "kind_label": leaves.KINDS[lr.kind],
            "start": iso(lr.start, z), "end": iso(lr.end, z), "amount": lr.amount,
            "reason": lr.reason, "status": lr.status, "admin_note": lr.admin_note,
            "source": lr.source,
            "user": {"id": lr.user_id, "name": lr.user.name},
            "substitute": ({"id": lr.substitute_id, "name": lr.substitute.name}
                           if lr.substitute_id else None),
            "substitute_status": lr.substitute_status, "created_at": iso(lr.created_at, z)}


def ser_user(u: User):
    return {"id": u.id, "name": u.name, "phone": u.phone, "role": u.role,
            "person_ref": u.person_ref, "job_title": u.job_title, "plant_id": u.plant_id,
            "team_id": u.team_id, "team": u.team.name if u.team else None, "active": u.active,
            "annual_balance": u.annual_balance, "sick_balance": u.sick_balance,
            "departure_hours_balance": u.departure_hours_balance}


def ser_result(r: attendance.ShiftResult, z, plant: Plant | None = None):
    return {"shift": ser_shift(r.shift, z, plant), "arrive": iso(r.arrive, z),
            "depart": iso(r.depart, z), "worked_min": r.worked_min, "site_min": r.site_min,
            "late_min": r.late_min, "early_min": r.early_min, "overtime_min": r.overtime_min,
            "status": r.status, "notes": r.notes}


def parse_local(s: str, z) -> datetime:
    from .timeutil import aware_to_utc
    try:
        return aware_to_utc(datetime.fromisoformat(s), z)
    except ValueError:
        err("صيغة التاريخ غلط")


def month_bounds(month: str | None, z) -> tuple[datetime, datetime]:
    if month:
        try:
            y, m = map(int, month.split("-"))
            first = date(y, m, 1)
        except ValueError:
            err("صيغة الشهر لازم تكون YYYY-MM")
    else:
        first = to_local(now_utc(), z).date().replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return local_day_bounds_utc(first, z)[0], local_day_bounds_utc(nxt, z)[0]
