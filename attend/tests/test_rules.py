"""Tests for the lead's rules: plant profiles, humane times, notification hook, catch-up jobs,
names without guessing, SolNerve integration, privacy."""
import hashlib
import hmac
import json
import logging
from datetime import date, timedelta

import pytest

from app import config, jobs, notify
from app.db import SessionLocal
from app.models import Event, JobRun, Notification, Shift, User
from app.timeutil import now_utc, to_local, tz

from .conftest import INSIDE_PLANT, client, make_employee

TOKEN = "integration-test-token"  # test-only value, set via monkeypatch below
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Fake plant B: a different tenant, timezone, locale and THREE 8-hour shifts.
PLANT_B = {
    "tenantId": "tenant-b", "plantId": "plant-b-1", "name": "Fake Plant B",
    "timezone": "Asia/Dubai", "locale": "en",
    "polygons": {"plant": [[24.40, 54.60], [24.40, 54.62], [24.42, 54.62], [24.42, 54.60]],
                 "site": None},
    "shifts": [{"code": "M", "start": "06:00", "hours": 8, "label": "Morning"},
               {"code": "A", "start": "14:00", "hours": 8, "label": "Afternoon"},
               {"code": "N", "start": "22:00", "hours": 8, "label": "Night"}],
    "graceMinutes": 5, "minStaff": 1,
}
IN_B = (24.41, 54.61)


@pytest.fixture
def integ(monkeypatch):
    monkeypatch.setattr(config, "INTEGRATION_TOKEN", TOKEN)
    c = client()
    c.headers.update(AUTH)
    return c


def _db():
    return SessionLocal()


# ------------------------------------------------------------ rule 4: plant profiles
def test_second_plant_runs_with_no_code_change(admin, integ):
    """A plant with another tenant, timezone, locale and shift set works end to end."""
    r = integ.put("/api/integration/plants", json=PLANT_B)
    assert r.status_code == 200, r.text
    r = integ.put("/api/integration/people", json={"tenantId": "tenant-b", "people": [
        {"personRef": "b-1", "displayName": "Worker One", "plantId": "plant-b-1",
         "roles": ["technician"], "loginPhone": "0500000001"},
        {"personRef": "b-2", "displayName": "Boss B", "plantId": "plant-b-1",
         "roles": ["attend_admin"], "loginPhone": "0500000002"}]})
    assert r.json()["upserted"] == 2, r.text
    db = _db()
    w = db.query(User).filter_by(person_ref="b-1").one()
    db.close()
    b_admin = client()
    # Attend owns the app login (SolNerve owns the person); set test passwords directly
    db = _db()
    from app.security import hash_password
    for u in db.query(User).filter(User.tenant_id == "tenant-b"):
        u.password_hash = hash_password("pass123")
    db.commit()
    db.close()
    assert b_admin.post("/api/login", json={"phone": "0500000002", "password": "pass123"}).status_code == 200

    plants = b_admin.get("/api/admin/plants").json()
    assert [p["plantId"] for p in plants] == ["plant-b-1"]  # tenant A's plant is invisible
    pid = plants[0]["id"]
    t = b_admin.post("/api/admin/teams", json={"name": "Blue", "plant_id": pid,
                                               "pattern": "M,A,N,O", "anchor_date": "2026-01-01"})
    assert t.status_code == 200, t.text
    # D is plant A's code, not plant B's
    bad = b_admin.post("/api/admin/teams", json={"name": "X", "plant_id": pid,
                                                 "pattern": "D,N", "anchor_date": "2026-01-01"})
    assert bad.status_code == 400
    db = _db()
    db.get(User, w.id).team_id = t.json()["id"]
    db.commit()
    db.close()
    start = date(2026, 1, 1)
    r = b_admin.post("/api/admin/schedule/generate", json={"team_id": t.json()["id"],
                                                           "start": start.isoformat(), "days": 4})
    assert r.json()["created"] == 3
    sch = b_admin.get(f"/api/admin/schedule?plant_id={pid}&start={start}&days=4").json()
    starts = sorted(s["start"] for s in sch["shifts"])
    assert starts == ["2026-01-01T06:00+04:00", "2026-01-02T14:00+04:00", "2026-01-03T22:00+04:00"]
    assert {s["label"] for s in sch["shifts"]} == {"Morning", "Afternoon", "Night"}

    worker = client()
    assert worker.post("/api/login", json={"phone": "0500000001", "password": "pass123"}).status_code == 200
    me = worker.get("/api/me").json()
    assert me["plant"]["timezone"] == "Asia/Dubai" and me["plant"]["locale"] == "en"
    assert worker.post("/api/events", json={"type": "arrive", "lat": INSIDE_PLANT[0],
                                            "lng": INSIDE_PLANT[1]}).status_code == 400  # plant A coords
    assert worker.post("/api/events", json={"type": "arrive", "lat": IN_B[0], "lng": IN_B[1]}).status_code == 200
    # tenant A admin cannot see tenant B people
    assert all(p["name"] != "Worker One" for p in admin.get("/api/admin/employees").json())
    assert admin.get(f"/api/admin/schedule?plant_id={pid}&start={start}").status_code == 404


def test_profile_validation(integ):
    bad = dict(PLANT_B, timezone="Mars/Olympus")
    assert integ.put("/api/integration/plants", json=bad).status_code == 400
    bad = dict(PLANT_B, shifts=[{"code": "O", "start": "06:00", "hours": 8}])
    assert integ.put("/api/integration/plants", json=bad).status_code == 400
    bad = dict(PLANT_B, plantId="bad id!")
    assert integ.put("/api/integration/plants", json=bad).status_code == 400


def test_no_plant_specific_values_in_code():
    """Plant names, ids, coordinates and timezones must come from profiles, not code."""
    import pathlib
    import re
    src = "".join(p.read_text(encoding="utf-8") for p in
                  (pathlib.Path(__file__).parent.parent / "app").rglob("*.py"))
    assert not re.search(r"Asia/|Europe/|America/", src)
    assert not re.search(r"\b3[01]\.\d{3,}\b", src)  # no hard-coded latitudes


# ------------------------------------------------------------ rule 6: humane times
def _shift(uid, pid, start, hours=12, code="D"):
    db = _db()
    db.add(Shift(user_id=uid, plant_id=pid, code=code, start=start, end=start + timedelta(hours=hours)))
    db.commit()
    db.close()


def _event(uid, pid, t, ts):
    db = _db()
    db.add(Event(user_id=uid, plant_id=pid, type=t, ts=ts))
    db.commit()
    db.close()


def test_grace_counts_as_on_time(admin):
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    start = now_utc().replace(minute=0, second=0, microsecond=0) - timedelta(hours=30)
    _shift(emp.uid, admin.plant_id, start)
    _event(emp.uid, admin.plant_id, "arrive", start + timedelta(minutes=10))  # 10:10 for 10:00
    _event(emp.uid, admin.plant_id, "depart", start + timedelta(hours=12, minutes=4))
    month = to_local(start, tz("Asia/Amman")).strftime("%Y-%m")
    d = emp.get(f"/api/me/attendance?month={month}").json()["days"][0]
    assert d["late_min"] == 0
    assert d["worked_min"] == 12 * 60  # counted as exactly the shift
    assert d["overtime_min"] == 0 and d["early_min"] == 0


def test_beyond_grace_counts_in_full(admin):
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    start = now_utc().replace(minute=0, second=0, microsecond=0) - timedelta(hours=30)
    _shift(emp.uid, admin.plant_id, start)
    _event(emp.uid, admin.plant_id, "arrive", start + timedelta(minutes=11))
    _event(emp.uid, admin.plant_id, "depart", start + timedelta(hours=11, minutes=40))
    month = to_local(start, tz("Asia/Amman")).strftime("%Y-%m")
    d = emp.get(f"/api/me/attendance?month={month}").json()["days"][0]
    assert d["late_min"] == 11 and d["early_min"] == 20


# ------------------------------------------------------------ rule 3: notification hook
def test_hook_signed_retried_and_delivered_once(admin, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_URL", "https://shams.invalid/hook")
    monkeypatch.setattr(config, "NOTIFY_SECRET", "hook-test-key")
    calls = []

    def fake(body, headers):
        calls.append((body, headers))
        return 503 if len(calls) == 1 else 200  # first attempt fails

    monkeypatch.setattr(notify, "sender", fake)
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    emp.post("/api/me/sos", json={"lat": 31.91, "lng": 35.91})  # dispatches immediately
    assert len(calls) == 1
    body, headers = calls[0]
    sig = hmac.new(b"hook-test-key", body, hashlib.sha256).hexdigest()
    assert headers["X-Attend-Signature"] == f"sha256={sig}"
    p = json.loads(body)
    assert p["kind"] == "sos" and p["tenantId"] and p["plantId"] and p["id"].startswith("attend-notification-")
    db = _db()
    for n in db.query(Notification):
        n.next_try = None  # skip the back-off wait
    db.commit()
    jobs.tick(db)
    jobs.tick(db)
    db.close()
    sos_ids = [json.loads(b)["id"] for b, _ in calls[1:] if json.loads(b)["kind"] == "sos"]
    assert sos_ids == [p["id"]]  # retried once, then never sent again
    db = _db()
    assert db.query(Notification).filter_by(kind="sos").one().out_status == "delivered"
    db.close()


def test_no_hook_means_in_app_only(admin, monkeypatch):
    called = []
    monkeypatch.setattr(notify, "sender", lambda b, h: called.append(1) or 200)
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    emp.post("/api/me/sos", json={})
    db = _db()
    jobs.tick(db)
    assert not called
    assert {n.out_status for n in db.query(Notification)} == {"disabled"}
    db.close()


def test_no_telegram_anywhere():
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".js", ".html", ".md", ".json", ".css"} \
                and "test_rules.py" not in p.name:
            text = p.read_text(encoding="utf-8").lower()
            assert "telegram" not in text and "تلغرام" not in text, p


# ------------------------------------------------------------ rule 6: missed step runs once
def test_missed_depart_reminded_once_after_downtime(admin):
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    start = now_utc() - timedelta(hours=20)
    _shift(emp.uid, admin.plant_id, start)
    _event(emp.uid, admin.plant_id, "arrive", start)
    db = _db()
    r1 = jobs.tick(db)
    r2 = jobs.tick(db)
    assert r1["missed_departs"] == 1 and r2["missed_departs"] == 0
    assert db.query(Notification).filter_by(kind="missed_depart").count() == 1
    db.close()


def test_missed_arrival_reminded_once(admin):
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    _shift(emp.uid, admin.plant_id, now_utc() - timedelta(hours=1))
    db = _db()
    assert jobs.tick(db)["missed_arrivals"] == 1
    assert jobs.tick(db)["missed_arrivals"] == 0
    assert db.query(Notification).filter_by(kind="missed_arrival").count() == 1
    assert db.query(Notification).filter_by(kind="missed_arrival_admin").count() == 1
    db.close()


def test_daily_job_runs_once_after_three_missed_days(admin):
    db = _db()
    jobs.tick(db, now=now_utc() - timedelta(days=4))  # last ran 4 days ago
    before = db.query(Notification).filter_by(kind="coverage_gap").count()
    jobs.tick(db)  # server was down 3 days: runs once now
    jobs.tick(db)  # same day: does not run again
    after = db.query(Notification).filter_by(kind="coverage_gap").count()
    assert after - before == 1
    assert db.query(JobRun).one().period == to_local(now_utc(), tz("Asia/Amman")).date().isoformat()
    db.close()


# ------------------------------------------------------------ rule 5: names, never guessed
def test_name_forms_resolve_to_one_person_unknown_is_asked(admin, integ):
    db = _db()
    tenant = db.query(User).first().tenant_id
    db.close()
    emp = make_employee(admin, "أنس الخطيب", "0791000001", aliases=["انس", "م انس", "الخطيب"])
    for form in ["انس", "م انس", "الخطيب", "أنس الخطيب", "م. انس", "  انَس  "]:
        r = integ.post("/api/integration/names/resolve", json={"tenantId": tenant, "form": form}).json()
        assert r == {"status": "matched", "attendUserId": emp.uid, "personRef": None}, form
    r = integ.post("/api/integration/names/resolve", json={"tenantId": tenant, "form": "أبو أنس"}).json()
    assert r["status"] == "unknown"  # close, but never guessed
    qs = admin.get("/api/admin/name-questions").json()
    assert [q["form"] for q in qs] == ["أبو أنس"]
    assert admin.post(f"/api/admin/name-questions/{qs[0]['id']}/answer", json={"user_id": emp.uid}).status_code == 200
    r = integ.post("/api/integration/names/resolve", json={"tenantId": tenant, "form": "ابو انس"}).json()
    assert r["status"] == "matched"
    # the same form cannot belong to two people
    r = admin.post("/api/admin/employees", json={"name": "شخص ثاني", "phone": "0791000009",
                                                 "password": "pass123", "aliases": ["انس"]})
    assert r.status_code == 400


# ------------------------------------------------------------ rule 5: SolNerve integration
def _tenant_a():
    db = _db()
    t = db.query(User).first().tenant_id
    pref = None
    db.close()
    return t, pref


def test_integration_requires_token(admin, monkeypatch):
    c = client()
    assert c.get("/api/integration/presence?tenantId=x&plantId=y").status_code == 503
    monkeypatch.setattr(config, "INTEGRATION_TOKEN", TOKEN)
    assert c.get("/api/integration/presence?tenantId=x&plantId=y").status_code == 401
    c.headers["Authorization"] = "Bearer wrong"
    assert c.get("/api/integration/presence?tenantId=x&plantId=y").status_code == 401


def test_solnerve_people_leave_interlock_and_attendance(admin, integ, monkeypatch):
    tenant, _ = _tenant_a()
    plant_ref = admin.get("/api/admin/plants").json()[0]["plantId"]
    r = integ.put("/api/integration/people", json={"tenantId": tenant, "people": [
        {"personRef": "sn-1", "displayName": "أنس الخطيب", "aliases": ["انس", "م انس", "الخطيب"],
         "plantId": plant_ref, "roles": ["technician"], "team": "Ghost"},
        {"personRef": "sn-2", "displayName": "بلال", "plantId": plant_ref, "roles": ["technician"]},
        {"personRef": "sn-3", "displayName": "كريم", "plantId": plant_ref}]})
    assert r.json()["upserted"] == 3 and r.json()["unknownTeams"] == ["Ghost"]
    db = _db()
    u1, u2, u3 = (db.query(User).filter_by(person_ref=x).one() for x in ("sn-1", "sn-2", "sn-3"))
    db.close()
    s = now_utc().replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
    _shift(u1.id, admin.plant_id, s)
    _shift(u3.id, admin.plant_id, s)  # كريم busy
    win = {"start": (s - timedelta(hours=1)).isoformat() + "+00:00",
           "end": (s + timedelta(hours=13)).isoformat() + "+00:00"}
    r = integ.post("/api/integration/interlock/check", json={
        "tenantId": tenant, "personRef": "sn-1", "kind": "annual", **win, "substituteRef": "sn-3"}).json()
    assert r["ok"] is False and r["reason"]
    r = integ.post("/api/integration/interlock/check", json={
        "tenantId": tenant, "personRef": "sn-1", "kind": "annual", **win}).json()
    assert {c["personRef"]: c["ok"] for c in r["candidates"]} == {"sn-2": True, "sn-3": False}
    assert r["amount"] == 1
    r = integ.put("/api/integration/leaves", json={"tenantId": tenant, "leaves": [
        {"leaveRef": "lv-1", "personRef": "sn-1", "kind": "annual", **win, "status": "approved",
         "substituteRef": "sn-2"}]}).json()
    assert r["results"][0] == {"leaveRef": "lv-1", "ok": True, "applied": True, "shiftsMoved": 1}
    # replaying the same sync is a no-op (idempotent)
    r = integ.put("/api/integration/leaves", json={"tenantId": tenant, "leaves": [
        {"leaveRef": "lv-1", "personRef": "sn-1", "kind": "annual", **win, "status": "approved",
         "substituteRef": "sn-2"}]}).json()
    assert r["results"][0]["ok"] is True
    db = _db()
    moved = db.query(Shift).filter(Shift.start == s, Shift.user_id == u2.id).one()
    assert moved.source == "substitute" and moved.covering_for == u1.id
    db.close()
    out = integ.get(f"/api/integration/attendance?tenantId={tenant}").json()
    blob = json.dumps(out, ensure_ascii=False)
    assert "أنس" not in blob and "بلال" not in blob  # people by ref only
    assert not any(k in blob.lower() for k in ("salary", "wage", "pay", "amount", "rate"))
    assert {p["personRef"] for p in out["people"]} == {"sn-1", "sn-2", "sn-3"}


def test_solnerve_mode_disables_local_people_and_leave(admin, monkeypatch):
    emp = make_employee(admin, "موظف تجريبي", "0791000001")
    monkeypatch.setattr(config, "PEOPLE_SOURCE", "solnerve")
    assert admin.post("/api/admin/employees", json={"name": "x y", "phone": "0791000002",
                                                    "password": "pass123"}).status_code == 409
    assert admin.post("/api/admin/employees/bulk", json={"text": "a, 0791000003",
                                                         "default_password": "pass123"}).status_code == 409
    s = (now_utc() + timedelta(days=1)).isoformat()
    assert emp.post("/api/leaves", json={"kind": "annual", "start": s, "end": s}).status_code == 409
    assert emp.get("/api/me").json()["people_source"] == "solnerve"


# ------------------------------------------------------------ rule 7: privacy
def test_personal_data_never_logged(admin, caplog, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_URL", "https://shams.invalid/hook")

    def boom(body, headers):
        raise ConnectionError("down")

    monkeypatch.setattr(notify, "sender", boom)
    caplog.set_level(logging.DEBUG)
    emp = make_employee(admin, "اسم سري جدا", "0799999999")
    emp.post("/api/events", json={"type": "arrive", "lat": INSIDE_PLANT[0], "lng": INSIDE_PLANT[1]})
    emp.post("/api/me/sos", json={"lat": 31.91, "lng": 35.91})
    db = _db()
    jobs.tick(db)
    db.close()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "اسم سري" not in text and "0799999999" not in text and "31.91" not in text


def test_report_csv_has_no_money_or_phone(admin):
    make_employee(admin, "موظف تجريبي", "0791000001")
    csv = admin.get("/api/admin/report.csv").text
    assert "0791000001" not in csv
    for word in ("راتب", "أجر", "دينار", "salary", "JOD"):
        assert word not in csv
