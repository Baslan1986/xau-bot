"""All datetimes are stored as naive UTC. Conversion to local time happens at the edges."""
from datetime import date, datetime, time, timedelta, timezone

from . import config


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(config.TZ).replace(tzinfo=None)


def local_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=config.TZ).astimezone(timezone.utc).replace(tzinfo=None)


def local_day_bounds_utc(d: date) -> tuple[datetime, datetime]:
    start = local_to_utc(datetime.combine(d, time()))
    return start, local_to_utc(datetime.combine(d + timedelta(days=1), time()))


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def iso(dt: datetime | None) -> str | None:
    """Serialize a stored UTC datetime as local ISO time (what the UI shows)."""
    return to_local(dt).isoformat(timespec="minutes") if dt else None
