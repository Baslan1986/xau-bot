"""Solnerv Attend: HTTP API + static PWA."""
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import admin, attendance, config, geo, integration, jobs, leaves, profiles
from .db import Base, SessionLocal, engine, get_db
from .deps import (current_user, err, local_people_only, month_bounds, parse_local, ser_event,
                   ser_leave, ser_result, ser_shift, ser_user, user_tz)
from .models import Alert, LeaveRequest, LocationPing, Notification, Plant, Shift, User
from .notify import notify, notify_admins
from .security import hash_password, limiter, verify_password
from .timeutil import aware_to_utc, iso, local_day_bounds_utc, now_utc, to_local

STATIC = Path(__file__).parent / "static"
Base.metadata.create_all(engine)


@asynccontextmanager
async def lifespan(_app):
    if config.PROFILES_DIR:
        db = SessionLocal()
        try:
            profiles.load_dir(db, config.PROFILES_DIR)
        finally:
            db.close()
    stop = jobs.start_background(config.TICK_SECONDS) if config.TICK_SECONDS > 0 else None
    yield
    if stop:
        stop.set()


app = FastAPI(title="Solnerv Attend", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET, same_site="strict",
                   https_only=config.HTTPS_ONLY, max_age=60 * 60 * 24 * 30)
app.include_router(admin.router)
app.include_router(integration.router)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # CSRF defence in depth on top of SameSite=strict for cookie-authenticated calls.
    # Integration calls use a bearer token, not cookies, so they are exempt.
    if (request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/")
            and not path.startswith("/api/integration/")
            and request.headers.get("x-requested-with") != "fetch"):
        return JSONResponse({"detail": "bad request"}, status_code=400)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(self), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    phone: str = Field(max_length=30)
    password: str = Field(max_length=200)


@app.post("/api/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if limiter.blocked(phone):
        err("محاولات كثيرة، جرب بعد ربع ساعة", 429)
    user = db.scalars(select(User).where(User.phone == phone)).first()
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        limiter.fail(phone)
        err("رقم الهاتف أو كلمة السر غلط", 401)
    limiter.reset(phone)
    request.session.clear()
    request.session["uid"] = user.id
    return {"ok": True, "role": user.role}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


class PasswordIn(BaseModel):
    old: str = Field(max_length=200)
    new: str = Field(min_length=6, max_length=200)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not verify_password(body.old, user.password_hash):
        err("كلمة السر الحالية غلط")
    user.password_hash = hash_password(body.new)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ first-run setup wizard
@app.get("/api/setup")
def setup_status(db: Session = Depends(get_db)):
    return {"needed": db.scalar(select(func.count(User.id))) == 0}


class SetupIn(BaseModel):
    admin_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=6, max_length=30)
    password: str = Field(min_length=6, max_length=200)
    plant_name: str = Field(min_length=2, max_length=120)
    polygon: str = Field(max_length=200_000)
    timezone: str = Field(max_length=64)


DEFAULT_SHIFTS = [{"code": "D", "start": "07:00", "hours": 12, "label": "نهاري"},
                  {"code": "N", "start": "19:00", "hours": 12, "label": "ليلي"}]


@app.post("/api/setup")
def setup(body: SetupIn, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(func.count(User.id))):
        err("النظام متهيّأ من قبل", 409)
    tenant = f"t-{secrets.token_hex(4)}"
    try:
        plant = profiles.upsert(db, {
            "tenantId": tenant, "plantId": f"p-{secrets.token_hex(4)}", "name": body.plant_name,
            "timezone": body.timezone, "locale": "ar", "polygons": {"plant": body.polygon},
            "shifts": DEFAULT_SHIFTS}, commit=False)
    except profiles.ProfileError as e:
        err(str(e))
    a = User(tenant_id=tenant, name=body.admin_name.strip(), phone=body.phone.strip(),
             role="admin", password_hash=hash_password(body.password), plant_id=plant.id)
    db.add(a)
    db.commit()
    request.session.clear()
    request.session["uid"] = a.id
    return {"ok": True}


# ------------------------------------------------------------------ employee
@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    now = now_utc()
    plant = db.get(Plant, user.plant_id) if user.plant_id else None
    z = profiles.plant_tz(plant)
    last = attendance.last_event(db, user.id)
    st = last.type if last else None
    cur = db.scalars(select(Shift).where(Shift.user_id == user.id,
                                         Shift.end > now - timedelta(hours=2))
                     .order_by(Shift.start).limit(2)).all()
    lo, hi = month_bounds(None, z)
    summary = attendance.summarize(attendance.compute(db, user, lo, hi))
    streak = 0
    for r in reversed(attendance.compute(db, user, now - timedelta(days=60), now)):
        if r.status == "present" and r.late_min == 0:
            streak += 1
        elif r.status in ("upcoming", "in_progress", "leave", "unscheduled"):
            continue
        else:
            break
    return {
        "user": ser_user(user),
        "plant": ({"id": plant.id, "name": plant.name, "timezone": plant.timezone,
                   "locale": plant.locale, "shifts": profiles.shifts(plant),
                   "grace_minutes": plant.grace_minutes} if plant else None),
        "people_source": config.PEOPLE_SOURCE, "solnerve_url": config.SOLNERVE_URL or None,
        "state": st, "state_since": iso(last.ts, z) if last else None,
        "allowed": attendance.allowed(st),
        "shifts": [ser_shift(s, z, plant) for s in cur],
        "month": summary, "streak": streak,
        "unread": db.scalar(select(func.count(Notification.id)).where(
            Notification.user_id == user.id, Notification.read.is_(False))),
        "pending_cover": db.scalar(select(func.count(LeaveRequest.id)).where(
            LeaveRequest.substitute_id == user.id,
            LeaveRequest.status == "pending_substitute")),
    }


class EventIn(BaseModel):
    type: str
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    client_ts: datetime | None = None  # when queued offline; UTC with offset preferred
    offline: bool = False
    mocked: bool = False


def _record(db: Session, user: User, body: EventIn):
    ts = None
    if body.offline and body.client_ts:
        ts = aware_to_utc(body.client_ts, user_tz(db, user))
        now = now_utc()
        if ts > now + timedelta(minutes=5) or ts < now - timedelta(hours=config.OFFLINE_MAX_HOURS):
            raise attendance.EventError("وقت الحركة المحفوظة غير مقبول")
    return attendance.record(db, user, body.type, body.lat, body.lng, body.accuracy, ts=ts,
                             offline=body.offline, mocked=body.mocked)


@app.post("/api/events")
def post_event(body: EventIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        evs = _record(db, user, body)
    except attendance.EventError as e:
        err(str(e))
    z = user_tz(db, user)
    return {"events": [ser_event(e, z) for e in evs], "state": evs[-1].type,
            "allowed": attendance.allowed(evs[-1].type)}


class SyncIn(BaseModel):
    events: list[EventIn] = Field(max_length=200)


@app.post("/api/events/sync")
def sync_events(body: SyncIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    z = user_tz(db, user)

    def order(e: EventIn):
        return aware_to_utc(e.client_ts, z) if e.client_ts else datetime.min

    results = []
    for ev in sorted(body.events, key=order):
        ev.offline = True
        try:
            _record(db, user, ev)
            results.append({"ok": True})
        except attendance.EventError as e:
            db.rollback()
            results.append({"ok": False, "error": str(e)})
    return {"results": results}


@app.get("/api/me/events")
def my_events(days: int = 7, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from .models import Event
    since = now_utc() - timedelta(days=min(days, 62))
    evs = db.scalars(select(Event).where(Event.user_id == user.id, Event.ts >= since)
                     .order_by(Event.ts.desc())).all()
    z = user_tz(db, user)
    return [ser_event(e, z) for e in evs]


@app.get("/api/me/attendance")
def my_attendance(month: str | None = None, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    plant = db.get(Plant, user.plant_id) if user.plant_id else None
    z = profiles.plant_tz(plant)
    lo, hi = month_bounds(month, z)
    res = attendance.compute(db, user, lo, hi)
    return {"summary": attendance.summarize(res), "days": [ser_result(r, z, plant) for r in res]}


@app.get("/api/me/schedule")
def my_schedule(days: int = 30, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    plant = db.get(Plant, user.plant_id) if user.plant_id else None
    z = profiles.plant_tz(plant)
    lo = local_day_bounds_utc(to_local(now_utc(), z).date(), z)[0]
    shifts = db.scalars(select(Shift).where(Shift.user_id == user.id, Shift.start >= lo,
                                            Shift.start < lo + timedelta(days=min(days, 90)))
                        .order_by(Shift.start)).all()
    return [ser_shift(s, z, plant) for s in shifts]


class PingIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)


@app.post("/api/me/ping")
def ping(body: PingIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Live location, accepted only while the employee is checked in (privacy)."""
    if attendance.state(db, user.id) in (None, "depart"):
        return {"tracking": False}
    db.add(LocationPing(user_id=user.id, lat=body.lat, lng=body.lng, accuracy=body.accuracy))
    plant = db.get(Plant, user.plant_id)
    warn = None
    if plant and not geo.point_in_polygon(body.lat, body.lng, geo.load(plant.polygon)) \
            and (body.accuracy or 0) < 100:
        warn = "شكلك طلعت من المحطة، لا تنسى تسجل انطلاق"
        recent = db.scalars(select(Alert).where(Alert.user_id == user.id,
                                                Alert.kind == "left_geofence",
                                                Alert.ts > now_utc() - timedelta(hours=1))).first()
        if not recent:
            db.add(Alert(user_id=user.id, kind="left_geofence", lat=body.lat, lng=body.lng,
                         note="طلع من حدود المحطة بدون ما يسجل انطلاق"))
    db.commit()
    return {"tracking": True, "warning": warn}


class SosIn(BaseModel):
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    note: str | None = Field(default=None, max_length=500)


@app.post("/api/me/sos")
def sos(body: SosIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.add(Alert(user_id=user.id, kind="sos", lat=body.lat, lng=body.lng, note=body.note))
    where = f" الموقع: https://maps.google.com/?q={body.lat},{body.lng}" if body.lat else ""
    params = {"attendUserId": user.id, "lat": body.lat, "lng": body.lng}
    notify_admins(db, user.tenant_id, f"🆘 طوارئ من {user.name}!{where}", "sos", params)
    # Teammates checked in at the same plant are closest to help.
    for u in db.scalars(select(User).where(User.tenant_id == user.tenant_id,
                                           User.plant_id == user.plant_id,
                                           User.id != user.id, User.active.is_(True),
                                           User.role != "admin")):
        if attendance.state(db, u.id) not in (None, "depart"):
            notify(db, u.id, f"🆘 زميلك {user.name} طلب مساعدة طارئة.{where}", "sos_peer", params)
    db.commit()
    jobs.notify.dispatch(db)  # SOS must not wait for the next tick
    return {"ok": True}


@app.get("/api/me/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)):
    ns = db.scalars(select(Notification).where(Notification.user_id == user.id)
                    .order_by(Notification.ts.desc()).limit(50)).all()
    z = user_tz(db, user)
    out = [{"id": n.id, "text": n.text, "kind": n.kind, "ts": iso(n.ts, z), "read": n.read}
           for n in ns]
    for n in ns:
        n.read = True
    db.commit()
    return out


# ------------------------------------------------------------------ leave requests (local mode)
class LeaveIn(BaseModel):
    kind: str
    start: str
    end: str
    substitute_id: int | None = None
    reason: str | None = Field(default=None, max_length=1000)


@app.get("/api/leaves/candidates")
def leave_candidates(kind: str, start: str, end: str, user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    z = user_tz(db, user)
    return leaves.candidates(db, user, kind, parse_local(start, z), parse_local(end, z))


@app.post("/api/leaves", dependencies=[Depends(local_people_only)])
def create_leave(body: LeaveIn, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    z = user_tz(db, user)
    try:
        lr = leaves.create(db, user, body.kind, parse_local(body.start, z),
                           parse_local(body.end, z), body.substitute_id, body.reason)
    except leaves.LeaveError as e:
        err(str(e))
    return ser_leave(lr, z)


@app.get("/api/leaves/mine")
def my_leaves(user: User = Depends(current_user), db: Session = Depends(get_db)):
    z = user_tz(db, user)
    mine = db.scalars(select(LeaveRequest).where(LeaveRequest.user_id == user.id)
                      .order_by(LeaveRequest.created_at.desc()).limit(50)).all()
    cover = db.scalars(select(LeaveRequest).where(LeaveRequest.substitute_id == user.id)
                       .order_by(LeaveRequest.created_at.desc()).limit(50)).all()
    return {"mine": [ser_leave(x, z) for x in mine], "cover": [ser_leave(x, z) for x in cover]}


def _leave(db: Session, lid: int) -> LeaveRequest:
    lr = db.get(LeaveRequest, lid)
    if not lr:
        err("الطلب غير موجود", 404)
    return lr


class RespondIn(BaseModel):
    accept: bool


@app.post("/api/leaves/{lid}/respond", dependencies=[Depends(local_people_only)])
def respond_leave(lid: int, body: RespondIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    try:
        lr = leaves.substitute_respond(db, user, _leave(db, lid), body.accept)
    except leaves.LeaveError as e:
        err(str(e))
    return ser_leave(lr, user_tz(db, user))


@app.post("/api/leaves/{lid}/cancel", dependencies=[Depends(local_people_only)])
def cancel_leave(lid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        lr = leaves.cancel(db, user, _leave(db, lid))
    except leaves.LeaveError as e:
        err(str(e))
    return ser_leave(lr, user_tz(db, user))


# ------------------------------------------------------------------ static PWA
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
