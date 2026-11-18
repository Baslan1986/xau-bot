import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["ATTEND_DB_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["ATTEND_SECRET"] = "test-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.security import limiter  # noqa: E402

# A ~2km square near Ma'an; the work site is a smaller square inside it.
STATION = "31.90, 35.90\n31.90, 35.92\n31.92, 35.92\n31.92, 35.90"
SITE = "31.905, 35.905\n31.905, 35.915\n31.915, 35.915\n31.915, 35.905"
INSIDE_SITE = (31.91, 35.91)
INSIDE_STATION = (31.918, 35.918)  # in station, outside site
OUTSIDE = (31.95, 35.95)


def client():
    c = TestClient(app)
    c.headers["X-Requested-With"] = "fetch"
    return c


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    limiter.fails.clear()
    yield


@pytest.fixture
def admin():
    c = client()
    r = c.post("/api/setup", json={"admin_name": "المدير", "phone": "0790000000",
                                   "password": "secret1", "station_name": "محطة معان",
                                   "polygon": STATION})
    assert r.status_code == 200, r.text
    sid = c.get("/api/admin/stations").json()[0]["id"]
    r = c.put(f"/api/admin/stations/{sid}", json={"name": "محطة معان", "polygon": STATION,
                                                  "site_polygon": SITE, "min_staff": 1})
    assert r.status_code == 200, r.text
    c.station_id = sid
    return c


def make_employee(admin, name, phone, team_id=None):
    r = admin.post("/api/admin/employees", json={
        "name": name, "phone": phone, "password": "pass123",
        "station_id": admin.station_id, "team_id": team_id})
    assert r.status_code == 200, r.text
    c = client()
    assert c.post("/api/login", json={"phone": phone, "password": "pass123"}).status_code == 200
    c.uid = r.json()["id"]
    return c
