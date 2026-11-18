"""All datetimes are stored as naive UTC. Each plant's timezone comes from its profile."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"منطقة زمنية غير معروفة: {name}") from e


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt: datetime, z: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=timezone.utc).astimezone(z).replace(tzinfo=None)


def local_to_utc(dt: datetime, z: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=z).astimezone(timezone.utc).replace(tzinfo=None)


def aware_to_utc(dt: datetime, z: ZoneInfo) -> datetime:
    """Aware -> UTC; naive is read as plant-local."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else local_to_utc(dt, z)


def local_day_bounds_utc(d: date, z: ZoneInfo) -> tuple[datetime, datetime]:
    return (local_to_utc(datetime.combine(d, time()), z),
            local_to_utc(datetime.combine(d + timedelta(days=1), time()), z))


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def iso(dt: datetime | None, z: ZoneInfo) -> str | None:
    """Plant-local wall time with its UTC offset, e.g. 2026-11-18T07:00+02:00."""
    if not dt:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(z).isoformat(timespec="minutes")
