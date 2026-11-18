"""Plant profiles: the one standard every plant is described by.

{
  "tenantId": "...", "plantId": "...", "name": "...",
  "timezone": "Area/City", "locale": "ar",
  "polygons": {"plant": [[lat,lng],...], "site": [[lat,lng],...] | null},
  "shifts": [{"code": "D", "start": "07:00", "hours": 12, "label": "..."}],
  "graceMinutes": 10, "minStaff": 1
}
Polygons may also be given as text (Google Maps lines or KML) and are parsed by geo.parse_polygon.
"""
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import geo
from .models import Plant
from .timeutil import tz

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CODE_RE = re.compile(r"^[A-NP-Z][A-Z0-9]{0,7}$")  # "O" is reserved for rest days


class ProfileError(ValueError):
    pass


def _poly(v):
    if v is None or v == "" or v == []:
        return None
    if isinstance(v, str):
        return geo.parse_polygon(v)
    return geo.parse_polygon(json.dumps(v))


def parse_shifts(v) -> list[dict]:
    if isinstance(v, str):  # "code, start, hours, label" per line
        rows = []
        for line in v.splitlines():
            parts = [p.strip() for p in line.replace("،", ",").split(",")]
            if parts and parts[0]:
                if len(parts) < 3:
                    raise ProfileError("كل شفت: الرمز، البداية، عدد الساعات، الاسم")
                rows.append({"code": parts[0], "start": parts[1], "hours": parts[2],
                             "label": parts[3] if len(parts) > 3 else parts[0]})
        v = rows
    if not isinstance(v, list) or not v:
        raise ProfileError("لازم شفت واحد على الأقل")
    out, seen = [], set()
    for s in v:
        code = str(s.get("code", "")).strip().upper()
        if not CODE_RE.match(code):
            raise ProfileError(f"رمز الشفت '{code}' غير صالح (حروف/أرقام، و O محجوز للراحة)")
        if code in seen:
            raise ProfileError(f"رمز الشفت '{code}' مكرر")
        seen.add(code)
        start = str(s.get("start", ""))
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", start):
            raise ProfileError(f"وقت بداية الشفت '{start}' غير صالح")
        try:
            hours = float(s.get("hours"))
        except (TypeError, ValueError) as e:
            raise ProfileError("عدد ساعات الشفت غير صالح") from e
        if not 0 < hours <= 24:
            raise ProfileError("عدد ساعات الشفت لازم بين 0 و 24")
        out.append({"code": code, "start": start, "hours": hours,
                    "label": str(s.get("label") or code)[:40]})
    return out


def validate(p: dict) -> dict:
    try:
        tenant, plant = str(p["tenantId"]), str(p["plantId"])
    except KeyError as e:
        raise ProfileError(f"الحقل {e} مطلوب") from e
    if not ID_RE.match(tenant) or not ID_RE.match(plant):
        raise ProfileError("tenantId و plantId: حروف وأرقام و - _ . فقط")
    try:
        tz(str(p.get("timezone", "")))
    except ValueError as e:
        raise ProfileError(str(e)) from e
    polys = p.get("polygons") or {}
    try:
        plant_poly = _poly(polys.get("plant"))
        site_poly = _poly(polys.get("site"))
    except geo.PolygonError as e:
        raise ProfileError(str(e)) from e
    if not plant_poly:
        raise ProfileError("حدود المحطة مطلوبة")
    grace = int(p.get("graceMinutes", 10))
    min_staff = int(p.get("minStaff", 1))
    if not 0 <= grace <= 120 or not 0 <= min_staff <= 50:
        raise ProfileError("السماح بين 0 و 120 دقيقة، وأقل عدد بين 0 و 50")
    return {"tenantId": tenant, "plantId": plant,
            "name": str(p.get("name") or plant)[:120], "timezone": str(p["timezone"]),
            "locale": str(p.get("locale") or "ar")[:16],
            "polygons": {"plant": plant_poly, "site": site_poly},
            "shifts": parse_shifts(p.get("shifts")), "graceMinutes": grace, "minStaff": min_staff}


def upsert(db: Session, p: dict, commit: bool = True) -> Plant:
    v = validate(p)
    plant = db.scalars(select(Plant).where(Plant.tenant_id == v["tenantId"],
                                           Plant.plant_ref == v["plantId"])).first()
    if plant is None:
        plant = Plant(tenant_id=v["tenantId"], plant_ref=v["plantId"])
        db.add(plant)
    plant.name, plant.timezone, plant.locale = v["name"], v["timezone"], v["locale"]
    plant.polygon = json.dumps(v["polygons"]["plant"])
    plant.site_polygon = json.dumps(v["polygons"]["site"]) if v["polygons"]["site"] else None
    plant.shifts = json.dumps(v["shifts"], ensure_ascii=False)
    plant.grace_minutes, plant.min_staff = v["graceMinutes"], v["minStaff"]
    if commit:
        db.commit()
    else:
        db.flush()
    return plant


def to_dict(plant: Plant) -> dict:
    return {"id": plant.id, "tenantId": plant.tenant_id, "plantId": plant.plant_ref,
            "name": plant.name, "timezone": plant.timezone, "locale": plant.locale,
            "polygons": {"plant": geo.load(plant.polygon), "site": geo.load(plant.site_polygon)},
            "shifts": shifts(plant), "graceMinutes": plant.grace_minutes,
            "minStaff": plant.min_staff}


def shifts(plant: Plant) -> list[dict]:
    return json.loads(plant.shifts)


def shift_def(plant: Plant, code: str) -> dict:
    for s in shifts(plant):
        if s["code"] == code:
            return s
    raise ProfileError(f"الشفت '{code}' مش معرّف بملف المحطة")


def plant_tz(plant: Plant | None):
    return tz(plant.timezone) if plant else tz("UTC")


def load_dir(db: Session, directory: str) -> int:
    n = 0
    for f in sorted(Path(directory).glob("*.json")):
        upsert(db, json.loads(f.read_text(encoding="utf-8")))
        n += 1
    return n
