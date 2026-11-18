import os
import secrets
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["ATTEND_DB_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ATTEND_SECRET"] = secrets.token_hex(32)  # generated per run; never committed
os.environ["ATTEND_TICK_SECONDS"] = "0"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config, notify  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.security import limiter  # noqa: E402

# Fake plant A: everything below is test data, not configuration in code.
PLANT = "31.90, 35.90\n31.90, 35.92\n31.92, 35.92\n31.92, 35.90"
SITE = "31.905, 35.905\n31.905, 35.915\n31.915, 35.915\n31.915, 35.905"
INSIDE_SITE = (31.91, 35.91)
INSIDE_PLANT = (31.918, 35.918)  # in plant, outside site
OUTSIDE = (31.95, 35.95)
SHIFTS_A = "D, 07:00, 12, نهاري\nN, 19:00, 12, ليلي"
TZ_A = "Asia/Amman"


def client():
    c = TestClient(app)
    c.headers["X-Requested-With"] = "fetch"
    return c


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    limiter.fails.clear()
    for k in ("NOTIFY_URL", "NOTIFY_SECRET", "INTEGRATION_TOKEN", "SOLNERVE_URL"):
        monkeypatch.setattr(config, k, "")
    monkeypatch.setattr(config, "PEOPLE_SOURCE", "local")
    monkeypatch.setattr(notify, "sender", notify.http_send)
    yield


@pytest.fixture
def admin():
    c = client()
    r = c.post("/api/setup", json={"admin_name": "مدير تجريبي", "phone": "0790000000",
                                   "password": "secret1", "plant_name": "محطة تجريبية",
                                   "polygon": PLANT, "timezone": TZ_A})
    assert r.status_code == 200, r.text
    pid = c.get("/api/admin/plants").json()[0]["id"]
    r = c.put(f"/api/admin/plants/{pid}", json={
        "name": "محطة تجريبية", "timezone": TZ_A, "polygon": PLANT, "site_polygon": SITE,
        "shifts": SHIFTS_A, "min_staff": 1, "grace_minutes": 10})
    assert r.status_code == 200, r.text
    c.plant_id = pid
    return c


def make_employee(admin, name, phone, team_id=None, aliases=None):
    r = admin.post("/api/admin/employees", json={
        "name": name, "phone": phone, "password": "pass123", "plant_id": admin.plant_id,
        "team_id": team_id, "aliases": aliases or []})
    assert r.status_code == 200, r.text
    c = client()
    assert c.post("/api/login", json={"phone": phone, "password": "pass123"}).status_code == 200
    c.uid = r.json()["id"]
    return c
