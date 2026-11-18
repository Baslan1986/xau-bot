"""Leave / departure requests with the substitute interlock.

Flow: employee requests -> substitute accepts -> admin approves.
- annual / sick: substitute must be free during every one of the requester's shifts in the range;
  on approval those shifts are moved to the substitute, so station coverage never drops.
- departure (hours): substitute must be at the station right now (checked in), because
  they cover the remaining hours of the same shift.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import attendance
from .models import LeaveRequest, Notification, Shift, User
from .timeutil import iso, now_utc

KINDS = {"annual": "إجازة سنوية", "sick": "إجازة مرضية", "departure": "مغادرة"}
OPEN = ("pending_substitute", "pending_admin")


class LeaveError(ValueError):
    pass


def notify(db: Session, user_id: int, text: str) -> None:
    db.add(Notification(user_id=user_id, text=text))


def notify_admins(db: Session, text: str) -> None:
    for a in db.scalars(select(User).where(User.role == "admin", User.active.is_(True))):
        notify(db, a.id, text)


def requester_shifts(db: Session, user: User, start: datetime, end: datetime) -> list[Shift]:
    return db.scalars(select(Shift).where(Shift.user_id == user.id, Shift.start < end,
                                          Shift.end > start).order_by(Shift.start)).all()


def _busy_on_leave(db: Session, uid: int, start: datetime, end: datetime,
                   exclude: int | None = None) -> bool:
    q = select(LeaveRequest).where(LeaveRequest.user_id == uid,
                                   LeaveRequest.status.in_(OPEN + ("approved",)),
                                   LeaveRequest.start < end, LeaveRequest.end > start)
    if exclude:
        q = q.where(LeaveRequest.id != exclude)
    return db.scalars(q).first() is not None


def substitute_problem(db: Session, user: User, sub: User, kind: str, start: datetime,
                       end: datetime, exclude: int | None = None) -> str | None:
    """Why `sub` can't cover `user`, or None if they can."""
    if sub.id == user.id:
        return "ما بتقدر تختار حالك"
    if not sub.active:
        return "الموظف غير فعّال"
    if sub.station_id != user.station_id:
        return f"{sub.name} مش بنفس المحطة"
    if _busy_on_leave(db, sub.id, start, end, exclude):
        return f"{sub.name} عنده إجازة أو طلب بنفس الوقت"
    if kind == "departure":
        if attendance.state(db, sub.id) in (None, "depart"):
            return f"{sub.name} مش موجود بالمحطة هلأ"
    else:
        for s in requester_shifts(db, user, start, end):
            clash = db.scalars(select(Shift).where(Shift.user_id == sub.id, Shift.start < s.end,
                                                   Shift.end > s.start)).first()
            if clash:
                return f"{sub.name} عنده دوام بنفس وقت شفتك ({iso(s.start)})"
    return None


def candidates(db: Session, user: User, kind: str, start: datetime, end: datetime) -> list[dict]:
    people = db.scalars(select(User).where(User.station_id == user.station_id,
                                           User.active.is_(True), User.id != user.id)).all()
    out = []
    for p in people:
        why = substitute_problem(db, user, p, kind, start, end)
        out.append({"id": p.id, "name": p.name, "ok": why is None, "reason": why})
    out.sort(key=lambda x: (not x["ok"], x["name"]))
    return out


def _amount(db: Session, user: User, kind: str, start: datetime, end: datetime) -> float:
    if kind == "departure":
        hours = (end - start).total_seconds() / 3600
        if hours <= 0 or hours > 12:
            raise LeaveError("مدة المغادرة لازم تكون بين دقيقة و 12 ساعة")
        if not requester_shifts(db, user, start, end):
            raise LeaveError("المغادرة لازم تكون خلال شفت إلك")
        return round(hours, 2)
    n = len(requester_shifts(db, user, start, end))
    if n == 0:
        raise LeaveError("ما عندك أي شفت بهاي الفترة، ما في داعي لإجازة")
    return float(n)


def _balance_field(kind: str) -> str:
    return {"annual": "annual_balance", "sick": "sick_balance",
            "departure": "departure_hours_balance"}[kind]


def create(db: Session, user: User, kind: str, start: datetime, end: datetime,
           substitute_id: int | None, reason: str | None) -> LeaveRequest:
    if kind not in KINDS:
        raise LeaveError("نوع الطلب غير معروف")
    if end <= start:
        raise LeaveError("وقت النهاية لازم يكون بعد البداية")
    if kind != "sick" and end < now_utc():
        raise LeaveError("ما بتقدر تطلب لفترة صارت")
    amount = _amount(db, user, kind, start, end)
    if getattr(user, _balance_field(kind)) < amount:
        raise LeaveError(f"رصيدك ما بكفي: بدك {amount:g} ومعك "
                         f"{getattr(user, _balance_field(kind)):g}")
    if _busy_on_leave(db, user.id, start, end):
        raise LeaveError("عندك طلب ثاني بنفس الفترة")
    if substitute_id is None and kind != "sick":
        raise LeaveError("لازم تختار زميل يغطيك (إنترلوك)")
    sub = None
    if substitute_id is not None:
        sub = db.get(User, substitute_id)
        if sub is None:
            raise LeaveError("البديل غير موجود")
        why = substitute_problem(db, user, sub, kind, start, end)
        if why:
            raise LeaveError(why)
    lr = LeaveRequest(user_id=user.id, kind=kind, start=start, end=end, amount=amount,
                      reason=reason, substitute_id=sub.id if sub else None,
                      substitute_status="pending" if sub else None,
                      status="pending_substitute" if sub else "pending_admin")
    db.add(lr)
    db.flush()
    if sub:
        notify(db, sub.id, f"{user.name} طالب منك تغطيه ب{KINDS[kind]} من {iso(start)} "
                           f"لـ {iso(end)}. وافق أو ارفض من صفحة الطلبات")
    else:
        notify_admins(db, f"طلب {KINDS[kind]} من {user.name} بحاجة موافقة (بدون بديل)")
    db.commit()
    return lr


def substitute_respond(db: Session, sub: User, lr: LeaveRequest, accept: bool) -> LeaveRequest:
    if lr.substitute_id != sub.id or lr.status != "pending_substitute":
        raise LeaveError("هذا الطلب مش بانتظار ردّك")
    requester = db.get(User, lr.user_id)
    if accept:
        why = substitute_problem(db, requester, sub, lr.kind, lr.start, lr.end, exclude=lr.id)
        if why:
            raise LeaveError(why)
        lr.substitute_status, lr.status = "accepted", "pending_admin"
        notify(db, requester.id, f"{sub.name} وافق يغطيك. الطلب هلأ عند الإدارة")
        notify_admins(db, f"طلب {KINDS[lr.kind]} من {requester.name} (البديل {sub.name} "
                          f"وافق) بانتظار موافقتك")
    else:
        lr.substitute_status, lr.status = "declined", "rejected"
        lr.admin_note = "البديل رفض"
        notify(db, requester.id, f"{sub.name} اعتذر عن التغطية. اختار زميل ثاني")
    db.commit()
    return lr


def decide(db: Session, admin: User, lr: LeaveRequest, approve: bool,
           note: str | None = None, substitute_id: int | None = None) -> LeaveRequest:
    if lr.status != "pending_admin":
        raise LeaveError("الطلب مش بانتظار موافقة الإدارة")
    user = db.get(User, lr.user_id)
    lr.admin_note, lr.decided_by, lr.decided_at = note, admin.id, now_utc()
    if not approve:
        lr.status = "rejected"
        notify(db, user.id, f"انرفض طلب {KINDS[lr.kind]}" + (f": {note}" if note else ""))
        if lr.substitute_id:
            notify(db, lr.substitute_id, f"طلب {user.name} انرفض، ما في داعي للتغطية")
        db.commit()
        return lr
    if substitute_id and not lr.substitute_id:  # admin assigns one (e.g. sick leave)
        lr.substitute_id, lr.substitute_status = substitute_id, "accepted"
    sub = db.get(User, lr.substitute_id) if lr.substitute_id else None
    if sub:
        why = substitute_problem(db, user, sub, lr.kind, lr.start, lr.end, exclude=lr.id)
        if why:
            raise LeaveError(why)
    field = _balance_field(lr.kind)
    if getattr(user, field) < lr.amount:
        raise LeaveError("رصيد الموظف ما بكفي")
    setattr(user, field, getattr(user, field) - lr.amount)
    if lr.kind != "departure":
        for s in requester_shifts(db, user, lr.start, lr.end):
            if sub:
                s.user_id, s.source, s.covering_for = sub.id, "substitute", user.id
            else:
                db.delete(s)
    lr.status = "approved"
    notify(db, user.id, f"✅ انقبل طلب {KINDS[lr.kind]}. الرصيد المتبقي: {getattr(user, field):g}")
    if sub:
        notify(db, sub.id, f"إنت مغطّي {user.name} من {iso(lr.start)} لـ {iso(lr.end)}. "
                           "الجدول تبعك اتحدّث")
    db.commit()
    return lr


def cancel(db: Session, user: User, lr: LeaveRequest) -> LeaveRequest:
    if lr.user_id != user.id or lr.status not in OPEN:
        raise LeaveError("ما بتقدر تلغي هذا الطلب")
    lr.status = "cancelled"
    if lr.substitute_id:
        notify(db, lr.substitute_id, f"{user.name} لغى طلب التغطية")
    db.commit()
    return lr
