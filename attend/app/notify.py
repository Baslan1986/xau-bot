"""Notifications: an in-app inbox plus an outbox for one outbound hook.

Attend does not talk to any messaging app. When ATTEND_NOTIFY_URL is set, each notification is
POSTed (signed) to that hook. Shams delivers it over WhatsApp, using personRef to find the person.
Delivery is at-least-once with a stable id ("id") so the receiver can de-duplicate. A delivery
that failed is retried on the next tick, as soon as possible, until it succeeds or hits the limit.

Privacy: payloads go only to the configured hook over HTTPS; nothing is ever logged.
"""
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import config
from .models import Notification, Plant, User
from .timeutil import now_utc

log = logging.getLogger("attend.notify")


def notify(db: Session, user_id: int, text: str, kind: str = "info", params: dict | None = None,
           dedupe_key: str | None = None) -> Notification | None:
    """Queue a notification. With dedupe_key, the same notification is never created twice."""
    if dedupe_key and db.scalars(select(Notification.id).where(
            Notification.dedupe_key == dedupe_key)).first():
        return None
    n = Notification(user_id=user_id, text=text, kind=kind,
                     params=json.dumps(params, ensure_ascii=False) if params else None,
                     dedupe_key=dedupe_key,
                     out_status="pending" if config.NOTIFY_URL else "disabled")
    db.add(n)
    try:
        db.flush()
    except IntegrityError:  # raced on dedupe_key
        db.rollback()
        return None
    return n


def notify_admins(db: Session, tenant_id: str, text: str, kind: str = "info",
                  params: dict | None = None, dedupe_key: str | None = None) -> None:
    for a in db.scalars(select(User).where(User.tenant_id == tenant_id, User.role == "admin",
                                           User.active.is_(True))):
        notify(db, a.id, text, kind, params, f"{dedupe_key}:{a.id}" if dedupe_key else None)


def payload(db: Session, n: Notification) -> dict:
    u = db.get(User, n.user_id)
    plant = db.get(Plant, u.plant_id) if u and u.plant_id else None
    return {
        "id": f"attend-notification-{n.id}",
        "tenantId": u.tenant_id if u else None,
        "plantId": plant.plant_ref if plant else None,
        "personRef": u.person_ref if u else None,
        "attendUserId": n.user_id,
        "kind": n.kind,
        "locale": plant.locale if plant else None,
        "text": n.text,
        "params": json.loads(n.params) if n.params else {},
        "createdAt": n.ts.isoformat() + "Z",
    }


def sign(body: bytes) -> str:
    return hmac.new(config.NOTIFY_SECRET.encode(), body, hashlib.sha256).hexdigest()


def http_send(body: bytes, headers: dict) -> int:
    req = urllib.request.Request(config.NOTIFY_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (URL is operator config)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# Replaceable in tests.
sender = http_send


def dispatch(db: Session, limit: int = 100) -> dict:
    if not config.NOTIFY_URL:
        return {"sent": 0, "failed": 0}
    now = now_utc()
    rows = db.scalars(select(Notification).where(
        Notification.out_status == "pending",
        or_(Notification.next_try.is_(None), Notification.next_try <= now))
        .order_by(Notification.id).limit(limit)).all()
    sent = failed = 0
    for n in rows:
        body = json.dumps(payload(db, n), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "X-Attend-Event": n.kind,
                   "Idempotency-Key": f"attend-notification-{n.id}"}
        if config.NOTIFY_SECRET:
            headers["X-Attend-Signature"] = "sha256=" + sign(body)
        n.attempts += 1
        try:
            status = sender(body, headers)
        except Exception as e:  # network errors: keep the type only, never the payload
            status, n.last_error = 0, type(e).__name__[:80]
        if 200 <= status < 300:
            n.out_status, n.last_error, n.next_try = "delivered", None, None
            sent += 1
        else:
            if status:
                n.last_error = f"http_{status}"
            failed += 1
            if n.attempts >= config.NOTIFY_MAX_ATTEMPTS:
                n.out_status = "failed"
            else:  # as soon as possible, then back off gently (max 10 min)
                n.next_try = now + timedelta(seconds=min(600, 15 * 2 ** (n.attempts - 1)))
    db.commit()
    if failed:
        log.warning("notification hook: %d deliveries pending retry", failed)  # counts only
    return {"sent": sent, "failed": failed}
