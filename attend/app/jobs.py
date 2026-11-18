"""Background tick: deliver notifications and catch up on missed steps.

Rule: a missed step runs once, as soon as possible. Every check is keyed (dedupe_key or JobRun
period), so a server that was down for three days runs each check once on restart, not three
times, and nobody receives the same reminder twice.
"""
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import attendance, notify, profiles, schedule
from .db import SessionLocal
from .models import Event, JobRun, LeaveRequest, Plant, Shift, User
from .timeutil import iso, now_utc, to_local

log = logging.getLogger("attend.jobs")


def _missed_arrivals(db: Session, now: datetime) -> int:
    n = 0
    shifts = db.scalars(select(Shift).where(Shift.start <= now, Shift.end > now,
                                            Shift.start > now - timedelta(hours=24))).all()
    for s in shifts:
        plant = db.get(Plant, s.plant_id)
        if now < s.start + timedelta(minutes=plant.grace_minutes + 15):
            continue
        st = attendance.state(db, s.user_id)
        if st not in (None, "depart"):
            continue
        on_leave = db.scalars(select(LeaveRequest).where(
            LeaveRequest.user_id == s.user_id, LeaveRequest.status == "approved",
            LeaveRequest.start < s.end, LeaveRequest.end > s.start)).first()
        if on_leave:
            continue
        z = profiles.plant_tz(plant)
        u = db.get(User, s.user_id)
        if notify.notify(db, u.id, f"شفتك بلّش الساعة {iso(s.start, z)[11:16]}، لا تنسى تسجل وصول",
                         "missed_arrival", {"shiftStart": iso(s.start, z)},
                         dedupe_key=f"missed-arrival:{s.id}"):
            notify.notify_admins(db, u.tenant_id, f"{u.name} مناوب وما سجّل وصول لسا",
                                 "missed_arrival_admin", {"attendUserId": u.id},
                                 dedupe_key=f"missed-arrival-admin:{s.id}")
            n += 1
    return n


def _missed_departs(db: Session, now: datetime) -> int:
    n = 0
    for u in db.scalars(select(User).where(User.active.is_(True))):
        last = attendance.last_event(db, u.id)
        if not last or last.type == "depart":
            continue
        arrive = db.scalars(select(Event).where(Event.user_id == u.id, Event.type == "arrive")
                            .order_by(Event.ts.desc()).limit(1)).first()
        if not arrive:
            continue
        shift = db.scalars(select(Shift).where(
            Shift.user_id == u.id, Shift.start - timedelta(hours=3) <= arrive.ts,
            Shift.end >= arrive.ts)).first()
        plant = db.get(Plant, u.plant_id) if u.plant_id else None
        grace = plant.grace_minutes if plant else 0
        due = (shift.end + timedelta(minutes=grace + 60)) if shift else arrive.ts + timedelta(hours=14)
        if now < due:
            continue
        if notify.notify(db, u.id, "شكلك نسيت تسجل انطلاق. سجّله هلأ، أو بلّغ مشرفك بوقت طلوعك",
                         "missed_depart", {}, dedupe_key=f"missed-depart:{arrive.id}"):
            n += 1
    return n


def _daily_coverage(db: Session, now: datetime) -> int:
    """Once per plant-local day: warn admins about uncovered shifts in the next 48h."""
    n = 0
    for plant in db.scalars(select(Plant)):
        z = profiles.plant_tz(plant)
        period = to_local(now, z).date().isoformat()
        key = f"daily-coverage:{plant.id}"
        jr = db.get(JobRun, key)
        if jr and jr.period == period:
            continue
        gaps = schedule.coverage_gaps(db, plant, now, now + timedelta(hours=48))
        if gaps:
            first = gaps[0]
            notify.notify_admins(db, plant.tenant_id,
                                 f"⚠️ {plant.name}: {len(gaps)} شفت بدون تغطية كافية بالـ 48 ساعة "
                                 f"الجاية، أولها {iso(first['start'], z)}",
                                 "coverage_gap", {"plantId": plant.plant_ref, "count": len(gaps)},
                                 dedupe_key=f"{key}:{period}")
        if jr:
            jr.period, jr.ran_at = period, now
        else:
            db.add(JobRun(key=key, period=period, ran_at=now))
        n += 1
    return n


def tick(db: Session, now: datetime | None = None) -> dict:
    now = now or now_utc()
    out = {"missed_arrivals": _missed_arrivals(db, now), "missed_departs": _missed_departs(db, now),
           "coverage_runs": _daily_coverage(db, now)}
    db.commit()
    out.update(notify.dispatch(db))
    return out


def start_background(interval: int) -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            db = SessionLocal()
            try:
                tick(db)
            except Exception:  # never log row data; the type is enough to investigate
                log.exception("tick failed")
            finally:
                db.close()

    threading.Thread(target=loop, name="attend-tick", daemon=True).start()
    return stop
