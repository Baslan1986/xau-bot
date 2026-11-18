"""Clock events (state machine + geofence) and worked-time calculation."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import geo
from .models import Event, LeaveRequest, Plant, Shift, User
from .timeutil import now_utc

# What each event is allowed to follow. None = no prior event (or last was depart).
NEXT = {
    None: {"arrive"},
    "depart": {"arrive"},
    "arrive": {"enter", "depart"},
    "exit": {"enter", "depart"},
    "enter": {"exit", "depart"},  # depart from inside the site auto-closes it with an exit
}
LABEL = {"arrive": "وصول", "enter": "دخول", "exit": "خروج", "depart": "انطلاق"}


class EventError(ValueError):
    pass


def last_event(db: Session, user_id: int, before: datetime | None = None) -> Event | None:
    q = select(Event).where(Event.user_id == user_id)
    if before is not None:
        q = q.where(Event.ts <= before)
    return db.scalars(q.order_by(Event.ts.desc(), Event.id.desc()).limit(1)).first()


def state(db: Session, user_id: int) -> str | None:
    e = last_event(db, user_id)
    return e.type if e else None


def allowed(st: str | None) -> list[str]:
    return sorted(NEXT[st], key=list(LABEL).index)


def record(db: Session, user: User, etype: str, lat: float | None, lng: float | None,
           accuracy: float | None = None, ts: datetime | None = None, offline: bool = False,
           mocked: bool = False) -> list[Event]:
    if etype not in LABEL:
        raise EventError("حركة غير معروفة")
    plant = db.get(Plant, user.plant_id) if user.plant_id else None
    if plant is None:
        raise EventError("ما في محطة معيّنة إلك، راجع الأدمن")
    ts = ts or now_utc()
    prev = last_event(db, user.id)
    if prev and ts < prev.ts:
        raise EventError("وقت الحركة أقدم من آخر حركة مسجلة")
    st = prev.type if prev else None
    if etype not in NEXT[st]:
        need = " أو ".join(LABEL[x] for x in allowed(st))
        raise EventError(f"ما بتقدر تسجل {LABEL[etype]} هلأ. المتاح: {need}")

    flags: list[str] = []
    in_plant = in_site = None
    if lat is None or lng is None:
        if etype in ("arrive", "enter") and not offline:
            raise EventError("لازم نعرف موقعك. فعّل الـ GPS وجرب مرة ثانية")
        flags.append("بدون موقع")
    else:
        in_plant = geo.point_in_polygon(lat, lng, geo.load(plant.polygon))
        site = geo.load(plant.site_polygon)
        in_site = geo.point_in_polygon(lat, lng, site) if site else in_plant
        if etype == "arrive" and not in_plant:
            if not offline:
                raise EventError("إنت مش داخل حدود المحطة. قرّب واستنى الـ GPS يثبت")
            flags.append("وصول خارج الحدود (بدون إنترنت)")
        if etype == "enter" and not in_site:
            if not offline:
                raise EventError("إنت مش داخل حدود موقع العمل")
            flags.append("دخول خارج الحدود (بدون إنترنت)")
        if accuracy and accuracy > 100:
            flags.append(f"دقة GPS ضعيفة ({int(accuracy)}م)")
    if mocked:
        flags.append("موقع مزوّر")
    if offline:
        flags.append("انسجلت بدون إنترنت")

    out: list[Event] = []
    if etype == "depart" and st == "enter":
        out.append(Event(user_id=user.id, plant_id=plant.id, type="exit", ts=ts, lat=lat,
                         lng=lng, accuracy=accuracy, inside_plant=in_plant,
                         inside_site=in_site, offline=offline, auto=True))
    out.append(Event(user_id=user.id, plant_id=plant.id, type=etype, ts=ts, lat=lat, lng=lng,
                     accuracy=accuracy, inside_plant=in_plant, inside_site=in_site,
                     offline=offline, flagged=bool(flags) and flags != ["انسجلت بدون إنترنت"],
                     flag_reason="، ".join(flags) or None))
    db.add_all(out)
    db.commit()
    return out


# ---------------------------------------------------------------- calculation
@dataclass
class ShiftResult:
    shift: Shift | None
    arrive: datetime | None = None
    depart: datetime | None = None
    worked_min: int = 0
    site_min: int = 0
    late_min: int = 0
    early_min: int = 0
    overtime_min: int = 0
    status: str = "absent"  # present | in_progress | missing_depart | absent | leave | upcoming | unscheduled
    notes: list[str] = field(default_factory=list)


def _sessions(events: list[Event], now: datetime):
    """Turn a flat event list into (arrive, depart|None, site_minutes) tuples."""
    out, cur, site, enter_at = [], None, 0, None
    for e in events:
        if e.type == "arrive":
            cur, site, enter_at = e.ts, 0, None
        elif e.type == "enter" and cur:
            enter_at = e.ts
        elif e.type == "exit" and cur and enter_at:
            site += int((e.ts - enter_at).total_seconds() // 60)
            enter_at = None
        elif e.type == "depart" and cur:
            out.append((cur, e.ts, site))
            cur = None
    if cur:
        if enter_at:
            site += int((now - enter_at).total_seconds() // 60)
        out.append((cur, None, site))
    return out


def compute(db: Session, user: User, lo: datetime, hi: datetime) -> list[ShiftResult]:
    """Match station sessions to scheduled shifts in [lo, hi)."""
    now = now_utc()
    shifts = db.scalars(select(Shift).where(Shift.user_id == user.id, Shift.start >= lo,
                                            Shift.start < hi).order_by(Shift.start)).all()
    events = db.scalars(select(Event).where(Event.user_id == user.id,
                                            Event.ts >= lo - timedelta(hours=6),
                                            Event.ts < hi + timedelta(hours=18))
                        .order_by(Event.ts, Event.id)).all()
    leaves = db.scalars(select(LeaveRequest).where(LeaveRequest.user_id == user.id,
                                                   LeaveRequest.status == "approved",
                                                   LeaveRequest.start < hi,
                                                   LeaveRequest.end > lo)).all()
    plant = db.get(Plant, user.plant_id) if user.plant_id else None
    grace = plant.grace_minutes if plant else 0
    sessions = _sessions(events, now)
    used: set[int] = set()
    results: list[ShiftResult] = []
    for s in shifts:
        r = ShiftResult(shift=s)
        idx = next((i for i, (a, _, _) in enumerate(sessions) if i not in used
                    and s.start - timedelta(hours=3) <= a <= s.end), None)
        on_leave = [lv for lv in leaves if lv.start < s.end and s.start < lv.end]
        if idx is None:
            if any(lv.kind != "departure" for lv in on_leave):
                r.status = "leave"
            elif s.start > now:
                r.status = "upcoming"
            elif s.start <= now < s.end:
                r.status = "absent"
                r.notes.append("ما سجّل وصول لسا")
            results.append(r)
            continue
        used.add(idx)
        a, d, site = sessions[idx]
        r.arrive, r.depart, r.site_min = a, d, site
        if d is None:
            if now < s.end + timedelta(hours=2):
                r.status, end = "in_progress", now
            else:
                r.status, end = "missing_depart", s.end
                r.notes.append("نسي يسجل انطلاق، انحسب لحد نهاية الشفت")
        else:
            r.status, end = "present", d
        # Humane times: inside the grace window, an arrival counts as the shift start and a
        # departure as the shift end (10:10 with 10 min grace counts as 10:00). Outside it, the
        # real minutes count in full.
        g = timedelta(minutes=grace)
        a_eff = s.start if s.start < a <= s.start + g else a
        end_eff = end
        if r.status == "present" and s.end - g <= d <= s.end + g:
            end_eff = s.end  # a few minutes either side of the end is just the end
        r.worked_min = max(0, int((end_eff - a_eff).total_seconds() // 60))
        late = int((a - s.start).total_seconds() // 60)
        r.late_min = late if late > grace else 0
        if r.status == "present":
            r.early_min = max(0, int((s.end - end_eff).total_seconds() // 60))
            r.overtime_min = max(0, int((end_eff - s.end).total_seconds() // 60))
        dep = [lv for lv in on_leave if lv.kind == "departure"]
        if dep:
            r.notes.append("مغادرة معتمدة")
            r.early_min = 0 if r.early_min and any(lv.end >= s.end for lv in dep) else r.early_min
        results.append(r)
    for i, (a, d, site) in enumerate(sessions):
        if i in used or not (lo <= a < hi):
            continue
        end = d or now
        m = max(0, int((end - a).total_seconds() // 60))
        results.append(ShiftResult(shift=None, arrive=a, depart=d, worked_min=m, site_min=site,
                                   overtime_min=m, status="unscheduled",
                                   notes=["دوام خارج الجدول"]))
    results.sort(key=lambda r: r.shift.start if r.shift else r.arrive)
    return results


def summarize(results: list[ShiftResult]) -> dict:
    sched = sum(int((r.shift.end - r.shift.start).total_seconds() // 60)
                for r in results if r.shift and r.status != "upcoming")
    return {
        "scheduled_hours": round(sched / 60, 2),
        "worked_hours": round(sum(r.worked_min for r in results) / 60, 2),
        "site_hours": round(sum(r.site_min for r in results) / 60, 2),
        "overtime_hours": round(sum(r.overtime_min for r in results) / 60, 2),
        "late_minutes": sum(r.late_min for r in results),
        "early_minutes": sum(r.early_min for r in results),
        "absences": sum(1 for r in results if r.status == "absent" and r.shift
                        and r.shift.end < now_utc()),
        "leave_shifts": sum(1 for r in results if r.status == "leave"),
        "missing_departs": sum(1 for r in results if r.status == "missing_depart"),
        "shifts_done": sum(1 for r in results if r.status in ("present", "missing_depart")),
    }
