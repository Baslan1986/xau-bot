from datetime import date, timedelta

import pytest

from app import geo
from app.db import SessionLocal
from app.models import Event, Shift, User
from app.timeutil import tz
from app.timeutil import now_utc, to_local

from .conftest import INSIDE_SITE, INSIDE_PLANT, OUTSIDE, client, make_employee


def ev(c, t, pt, **kw):
    return c.post("/api/events", json={"type": t, "lat": pt[0] if pt else None,
                                       "lng": pt[1] if pt else None, **kw})


# ------------------------------------------------------------ geo
def test_parse_polygon_formats():
    plain = geo.parse_polygon("31.1, 35.1\n31.1, 35.2\n31.2, 35.2")
    kml = geo.parse_polygon("<Polygon><coordinates>35.1,31.1,0 35.2,31.1,0 35.2,31.2,0 "
                            "35.1,31.1,0</coordinates></Polygon>")
    js = geo.parse_polygon("[[31.1,35.1],[31.1,35.2],[31.2,35.2]]")
    assert plain == kml == js == [[31.1, 35.1], [31.1, 35.2], [31.2, 35.2]]
    with pytest.raises(geo.PolygonError):
        geo.parse_polygon("31.1, 35.1\n31.2, 35.2")
    with pytest.raises(geo.PolygonError):
        geo.parse_polygon("99, 35\n31, 35\n31, 36")


def test_point_in_polygon():
    sq = [[0, 0], [0, 1], [1, 1], [1, 0]]
    assert geo.point_in_polygon(0.5, 0.5, sq)
    assert not geo.point_in_polygon(1.5, 0.5, sq)


# ------------------------------------------------------------ auth / security
def test_setup_only_once_and_login(admin):
    r = client().post("/api/setup", json={"admin_name": "x y", "phone": "0791111111",
                                          "password": "secret1", "plant_name": "s s", "timezone": "UTC",
                                          "polygon": "1,1\n1,2\n2,2"})
    assert r.status_code == 409
    c = client()
    assert c.post("/api/login", json={"phone": "0790000000", "password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"phone": "0790000000", "password": "secret1"}).json()["role"] == "admin"


def test_login_rate_limit(admin):
    c = client()
    for _ in range(5):
        c.post("/api/login", json={"phone": "0790000000", "password": "bad"})
    r = c.post("/api/login", json={"phone": "0790000000", "password": "secret1"})
    assert r.status_code == 429


def test_csrf_header_required(admin):
    c = client()
    del c.headers["X-Requested-With"]
    assert c.post("/api/login", json={"phone": "0790000000", "password": "secret1"}).status_code == 400


def test_employee_cannot_use_admin_api(admin):
    emp = make_employee(admin, "سامي", "0791000001")
    assert emp.get("/api/admin/employees").status_code == 403
    assert client().get("/api/me").status_code == 401


# ------------------------------------------------------------ events
def test_event_flow_and_geofence(admin):
    emp = make_employee(admin, "سامي", "0791000001")
    r = ev(emp, "arrive", OUTSIDE)
    assert r.status_code == 400 and "حدود المحطة" in r.json()["detail"]
    assert ev(emp, "enter", INSIDE_SITE).status_code == 400  # must arrive first
    assert ev(emp, "arrive", INSIDE_PLANT).json()["allowed"] == ["enter", "depart"]
    assert ev(emp, "enter", INSIDE_PLANT).status_code == 400  # in plant but not in site
    assert ev(emp, "enter", INSIDE_SITE).status_code == 200
    r = ev(emp, "depart", OUTSIDE)  # depart straight from the site auto-closes with exit
    assert r.status_code == 200
    assert [e["type"] for e in r.json()["events"]] == ["exit", "depart"]
    assert emp.get("/api/me").json()["allowed"] == ["arrive"]


def test_arrive_requires_location(admin):
    emp = make_employee(admin, "سامي", "0791000001")
    assert ev(emp, "arrive", None).status_code == 400


def test_offline_sync(admin):
    emp = make_employee(admin, "سامي", "0791000001")
    t0 = to_local(now_utc(), tz('Asia/Amman')) - timedelta(hours=3)
    r = emp.post("/api/events/sync", json={"events": [
        {"type": "depart", "lat": OUTSIDE[0], "lng": OUTSIDE[1],
         "client_ts": (t0 + timedelta(hours=2)).isoformat()},
        {"type": "arrive", "lat": INSIDE_PLANT[0], "lng": INSIDE_PLANT[1],
         "client_ts": t0.isoformat()},
    ]})
    assert [x["ok"] for x in r.json()["results"]] == [True, True]
    evs = emp.get("/api/me/events").json()
    assert [e["type"] for e in evs] == ["depart", "arrive"]
    assert all(e["offline"] for e in evs)
    # phones send UTC with offset; must land after the existing events regardless of phone TZ
    r = emp.post("/api/events/sync", json={"events": [
        {"type": "arrive", "lat": INSIDE_PLANT[0], "lng": INSIDE_PLANT[1],
         "client_ts": now_utc().isoformat() + "Z"}]})
    assert r.json()["results"][0]["ok"] is True, r.json()
    # too old is refused
    r = emp.post("/api/events/sync", json={"events": [
        {"type": "arrive", "lat": INSIDE_PLANT[0], "lng": INSIDE_PLANT[1],
         "client_ts": (t0 - timedelta(days=10)).isoformat()}]})
    assert r.json()["results"][0]["ok"] is False


# ------------------------------------------------------------ schedule
def _team(admin, name, pattern, anchor):
    r = admin.post("/api/admin/teams", json={"name": name, "plant_id": admin.plant_id,
                                             "pattern": pattern, "anchor_date": anchor})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_schedule_generation_and_gaps(admin):
    start = date.today()
    ta = _team(admin, "A", "D,D,N,N,O,O,O,O", start.isoformat())
    tb = _team(admin, "B", "O,O,O,O,D,D,N,N", start.isoformat())
    make_employee(admin, "أحمد", "0791000001", ta)
    make_employee(admin, "بلال", "0791000002", tb)
    for t in (ta, tb):
        r = admin.post("/api/admin/schedule/generate", json={"team_id": t, "start": start.isoformat(), "days": 8})
        assert r.json()["created"] == 4
    sch = admin.get(f"/api/admin/schedule?plant_id={admin.plant_id}&start={start}&days=8").json()
    assert len(sch["shifts"]) == 8
    # Two teams on a 4/4 rotation: every slot has exactly one D or N... but days 0-1 have no
    # night shift and days 2-3 no day shift, etc. -> 8 empty slots of 16.
    assert len(sch["gaps"]) == 8
    # regenerating is idempotent
    admin.post("/api/admin/schedule/generate", json={"team_id": ta, "start": start.isoformat(), "days": 8})
    assert len(admin.get(f"/api/admin/schedule?plant_id={admin.plant_id}&start={start}&days=8").json()["shifts"]) == 8


def test_bad_pattern(admin):
    r = admin.post("/api/admin/teams", json={"name": "A", "plant_id": admin.plant_id,
                                             "pattern": "D,X", "anchor_date": "2026-01-01"})
    assert r.status_code == 400


def test_bulk_employees(admin):
    _team(admin, "A", "D,N,O,O", "2026-01-01")
    r = admin.post("/api/admin/employees/bulk", json={
        "text": "أحمد علي, 0791000001, A, فني\nسارة، 0791000002\nخطأ\n=evil, 0791000003, Z",
        "default_password": "start123", "plant_id": admin.plant_id})
    body = r.json()
    assert body["created"] == ["أحمد علي", "سارة", "=evil"]
    assert len(body["errors"]) == 2
    csv = admin.get("/api/admin/report.csv").text
    assert "'=evil" in csv  # formula injection neutralised


# ------------------------------------------------------------ attendance calc
def _shift(uid, sid, start, hours=12):
    db = SessionLocal()
    s = Shift(user_id=uid, plant_id=sid, code="D", start=start, end=start + timedelta(hours=hours))
    db.add(s)
    db.commit()
    db.close()


def _event(uid, sid, t, ts):
    db = SessionLocal()
    db.add(Event(user_id=uid, plant_id=sid, type=t, ts=ts))
    db.commit()
    db.close()


def test_late_and_overtime(admin):
    emp = make_employee(admin, "سامي", "0791000001")
    # a shift that ended yesterday, in this month if possible
    start = now_utc().replace(minute=0, second=0, microsecond=0) - timedelta(hours=30)
    _shift(emp.uid, admin.plant_id, start)
    _event(emp.uid, admin.plant_id, "arrive", start + timedelta(minutes=25))
    _event(emp.uid, admin.plant_id, "enter", start + timedelta(hours=1))
    _event(emp.uid, admin.plant_id, "exit", start + timedelta(hours=4))
    _event(emp.uid, admin.plant_id, "depart", start + timedelta(hours=13))
    month = to_local(start, tz('Asia/Amman')).strftime("%Y-%m")
    day = emp.get(f"/api/me/attendance?month={month}").json()["days"][0]
    assert day["status"] == "present"
    assert day["late_min"] == 25
    assert day["overtime_min"] == 60
    assert day["site_min"] == 180
    assert day["worked_min"] == 12 * 60 + 35


def test_missing_depart_and_absent(admin):
    emp = make_employee(admin, "سامي", "0791000001")
    s1 = now_utc().replace(minute=0, second=0, microsecond=0) - timedelta(hours=40)
    _shift(emp.uid, admin.plant_id, s1)
    _event(emp.uid, admin.plant_id, "arrive", s1)
    s2 = s1 + timedelta(hours=24)
    _shift(emp.uid, admin.plant_id, s2, hours=4)
    rep = admin.get(f"/api/admin/report/{emp.uid}?month={to_local(s1, tz('Asia/Amman')).strftime('%Y-%m')}").json()
    statuses = [d["status"] for d in rep["days"]]
    assert statuses[0] == "missing_depart"
    if len(statuses) > 1:  # second shift may fall into next month
        assert statuses[1] == "absent"


# ------------------------------------------------------------ leave interlock
def _future_shift(uid, sid, days_ahead):
    start = now_utc().replace(minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    _shift(uid, sid, start)
    return start


def _local(dt):
    return to_local(dt, tz('Asia/Amman')).isoformat(timespec="minutes")


def test_annual_leave_interlock_full_flow(admin):
    a = make_employee(admin, "أحمد", "0791000001")
    b = make_employee(admin, "بلال", "0791000002")
    c = make_employee(admin, "كريم", "0791000003")
    s = _future_shift(a.uid, admin.plant_id, 3)
    _shift(c.uid, admin.plant_id, s)  # كريم busy at the same time
    lo, hi = _local(s - timedelta(hours=1)), _local(s + timedelta(hours=13))

    r = a.post("/api/leaves", json={"kind": "annual", "start": lo, "end": hi})
    assert r.status_code == 400 and "إنترلوك" in r.json()["detail"]
    r = a.post("/api/leaves", json={"kind": "annual", "start": lo, "end": hi, "substitute_id": c.uid})
    assert r.status_code == 400 and "دوام" in r.json()["detail"]
    cands = {x["name"]: x["ok"] for x in a.get("/api/leaves/candidates",
                                               params={"kind": "annual", "start": lo, "end": hi}).json()}
    assert cands["بلال"] is True and cands["كريم"] is False

    lid = a.post("/api/leaves", json={"kind": "annual", "start": lo, "end": hi,
                                      "substitute_id": b.uid}).json()["id"]
    # admin can't approve before the substitute accepts
    assert admin.post(f"/api/admin/leaves/{lid}/decide", json={"approve": True}).status_code == 400
    assert c.post(f"/api/leaves/{lid}/respond", json={"accept": True}).status_code == 400
    assert b.post(f"/api/leaves/{lid}/respond", json={"accept": True}).json()["status"] == "pending_admin"
    r = admin.post(f"/api/admin/leaves/{lid}/decide", json={"approve": True})
    assert r.json()["status"] == "approved"

    db = SessionLocal()
    assert db.get(User, a.uid).annual_balance == 13
    moved = db.query(Shift).filter(Shift.start == s, Shift.user_id == b.uid).one()
    assert moved.source == "substitute" and moved.covering_for == a.uid
    db.close()
    assert any("مغطّي" in n["text"] for n in b.get("/api/me/notifications").json())


def test_substitute_decline(admin):
    a = make_employee(admin, "أحمد", "0791000001")
    b = make_employee(admin, "بلال", "0791000002")
    s = _future_shift(a.uid, admin.plant_id, 2)
    lid = a.post("/api/leaves", json={"kind": "annual", "start": _local(s), "end": _local(s + timedelta(hours=12)),
                                      "substitute_id": b.uid}).json()["id"]
    assert b.post(f"/api/leaves/{lid}/respond", json={"accept": False}).json()["status"] == "rejected"


def test_leave_balance_and_no_shift(admin):
    a = make_employee(admin, "أحمد", "0791000001")
    b = make_employee(admin, "بلال", "0791000002")
    s = now_utc() + timedelta(days=5)
    r = a.post("/api/leaves", json={"kind": "annual", "start": _local(s), "end": _local(s + timedelta(days=1)),
                                    "substitute_id": b.uid})
    assert "ما عندك أي شفت" in r.json()["detail"]


def test_departure_needs_present_substitute(admin):
    a = make_employee(admin, "أحمد", "0791000001")
    b = make_employee(admin, "بلال", "0791000002")
    start = now_utc().replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _shift(a.uid, admin.plant_id, start)
    _shift(b.uid, admin.plant_id, start)
    lo = _local(now_utc() + timedelta(hours=1))
    hi = _local(now_utc() + timedelta(hours=3))
    r = a.post("/api/leaves", json={"kind": "departure", "start": lo, "end": hi, "substitute_id": b.uid})
    assert r.status_code == 400 and "مش موجود بالمحطة" in r.json()["detail"]
    assert ev(b, "arrive", INSIDE_PLANT).status_code == 200
    r = a.post("/api/leaves", json={"kind": "departure", "start": lo, "end": hi, "substitute_id": b.uid})
    assert r.status_code == 200 and r.json()["amount"] == 2
    lid = r.json()["id"]
    b.post(f"/api/leaves/{lid}/respond", json={"accept": True})
    admin.post(f"/api/admin/leaves/{lid}/decide", json={"approve": True})
    db = SessionLocal()
    assert db.get(User, a.uid).departure_hours_balance == 6
    db.close()


def test_sos_notifies_admins(admin):
    a = make_employee(admin, "أحمد", "0791000001")
    assert a.post("/api/me/sos", json={"lat": 31.91, "lng": 35.91}).status_code == 200
    live = admin.get("/api/admin/live").json()
    assert live["alerts"][0]["kind"] == "sos"
    assert any("طوارئ" in n["text"] for n in admin.get("/api/me/notifications").json())


def test_ping_only_while_checked_in(admin):
    a = make_employee(admin, "أحمد", "0791000001")
    assert a.post("/api/me/ping", json={"lat": 31.91, "lng": 35.91}).json()["tracking"] is False
    ev(a, "arrive", INSIDE_PLANT)
    r = a.post("/api/me/ping", json={"lat": OUTSIDE[0], "lng": OUTSIDE[1], "accuracy": 10}).json()
    assert r["tracking"] and r["warning"]
    assert admin.get("/api/admin/live").json()["alerts"][0]["kind"] == "left_geofence"
