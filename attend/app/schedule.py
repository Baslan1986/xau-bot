"""Shift rotation. Shift codes, start times and lengths come from the plant profile."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import profiles
from .models import Plant, Shift, Team, User
from .timeutil import local_to_utc, parse_hhmm, to_local

REST = "O"


def parse_pattern(p: str, plant: Plant) -> list[str]:
    codes = {s["code"] for s in profiles.shifts(plant)} | {REST}
    items = [x.strip().upper() for x in p.replace(" ", ",").split(",") if x.strip()]
    if not items or any(x not in codes for x in items):
        raise ValueError(f"النمط لازم يكون من رموز الشفتات ({', '.join(sorted(codes - {REST}))}) "
                         "و O للراحة، مفصولة بفواصل")
    return items


def shift_window(plant: Plant, day: date, code: str) -> tuple[datetime, datetime]:
    d = profiles.shift_def(plant, code)
    start = local_to_utc(datetime.combine(day, parse_hhmm(d["start"])), profiles.plant_tz(plant))
    return start, start + timedelta(hours=d["hours"])


def generate(db: Session, team: Team, start_day: date, end_day: date) -> int:
    """(Re)build automatic shifts for every active member of a team. Manual and substitute
    shifts are left alone, so regenerating never undoes an approved leave."""
    plant = db.get(Plant, team.plant_id)
    z = profiles.plant_tz(plant)
    pattern = parse_pattern(team.pattern, plant)
    members = db.scalars(select(User).where(User.team_id == team.id, User.active.is_(True))).all()
    lo = local_to_utc(datetime.combine(start_day, datetime.min.time()), z)
    hi = local_to_utc(datetime.combine(end_day + timedelta(days=1), datetime.min.time()), z)
    created = 0
    for u in members:
        for s in db.scalars(select(Shift).where(Shift.user_id == u.id, Shift.source == "auto",
                                                Shift.start >= lo, Shift.start < hi)):
            db.delete(s)
        db.flush()
        protected = db.scalars(select(Shift).where(Shift.user_id == u.id, Shift.source != "auto",
                                                   Shift.start >= lo - timedelta(days=1),
                                                   Shift.start < hi)).all()
        d = start_day
        while d <= end_day:
            code = pattern[(d - team.anchor_date).days % len(pattern)]
            if code != REST:
                s, e = shift_window(plant, d, code)
                if not any(p.start < e and s < p.end for p in protected):
                    db.add(Shift(user_id=u.id, plant_id=plant.id, code=code, start=s, end=e))
                    created += 1
            d += timedelta(days=1)
    db.commit()
    return created


def coverage_gaps(db: Session, plant: Plant, lo: datetime, hi: datetime) -> list[dict]:
    """Shift slots (from the profile) in [lo, hi) with fewer than min_staff people."""
    z = profiles.plant_tz(plant)
    counts: dict[tuple, int] = {}
    for s in db.scalars(select(Shift).where(Shift.plant_id == plant.id, Shift.start >= lo,
                                            Shift.start < hi)):
        counts[(s.start, s.code)] = counts.get((s.start, s.code), 0) + 1
    day = to_local(lo, z).date() - timedelta(days=1)
    last = to_local(hi, z).date()
    while day <= last:
        for d in profiles.shifts(plant):
            st, _ = shift_window(plant, day, d["code"])
            if lo <= st < hi:
                counts.setdefault((st, d["code"]), 0)
        day += timedelta(days=1)
    return [{"start": k[0], "code": k[1], "count": c, "needed": plant.min_staff}
            for k, c in sorted(counts.items()) if c < plant.min_staff]
