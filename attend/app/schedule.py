"""Shift rotation for two (or more) teams."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Shift, Station, Team, User
from .timeutil import local_to_utc, parse_hhmm, to_local

VALID = {"D", "N", "O"}


def parse_pattern(p: str) -> list[str]:
    items = [x.strip().upper() for x in p.replace(" ", ",").split(",") if x.strip()]
    if not items or any(x not in VALID for x in items):
        raise ValueError("النمط لازم يكون من D (نهار) و N (ليل) و O (راحة) مفصولة بفواصل")
    return items


def shift_window(station: Station, day: date, kind: str) -> tuple[datetime, datetime]:
    t = parse_hhmm(station.day_start if kind == "D" else station.night_start)
    start = local_to_utc(datetime.combine(day, t))
    return start, start + timedelta(hours=station.shift_hours)


def generate(db: Session, team: Team, start_day: date, end_day: date) -> int:
    """(Re)build automatic shifts for every active member of a team. Manual and substitute
    shifts are left alone, so regenerating never undoes an approved leave."""
    station = db.get(Station, team.station_id)
    pattern = parse_pattern(team.pattern)
    members = db.scalars(select(User).where(User.team_id == team.id, User.active.is_(True))).all()
    lo = local_to_utc(datetime.combine(start_day, datetime.min.time()))
    hi = local_to_utc(datetime.combine(end_day + timedelta(days=1), datetime.min.time()))
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
            kind = pattern[(d - team.anchor_date).days % len(pattern)]
            if kind != "O":
                s, e = shift_window(station, d, kind)
                if not any(p.start < e and s < p.end for p in protected):
                    db.add(Shift(user_id=u.id, station_id=station.id, kind=kind, start=s, end=e))
                    created += 1
            d += timedelta(days=1)
    db.commit()
    return created


def coverage_gaps(db: Session, station: Station, lo: datetime, hi: datetime) -> list[dict]:
    """Shift slots where fewer than station.min_staff people are scheduled."""
    shifts = db.scalars(select(Shift).where(Shift.station_id == station.id,
                                            Shift.start >= lo, Shift.start < hi)).all()
    slots: dict[tuple, int] = {}
    for s in shifts:
        slots[(s.start, s.kind)] = slots.get((s.start, s.kind), 0) + 1
    # also catch slots where nobody is scheduled at all
    d = lo
    while d < hi:
        day = to_local(d).date()
        for k in ("D", "N"):
            st, _ = shift_window(station, day, k)
            if lo <= st < hi:
                slots.setdefault((st, k), 0)
        d += timedelta(days=1)
    return [{"start": k[0], "kind": k[1], "count": c, "needed": station.min_staff}
            for k, c in sorted(slots.items()) if c < station.min_staff]
