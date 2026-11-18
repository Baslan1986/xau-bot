"""Geofence helpers. Polygons are lists of [lat, lng]."""
import json
import re


class PolygonError(ValueError):
    pass


def parse_polygon(text: str) -> list[list[float]]:
    """Accept what people actually copy out of Google Maps / Google Earth.

    - KML: <coordinates>lng,lat,alt lng,lat,alt ...</coordinates>
    - JSON: [[lat, lng], ...]
    - Plain: one "lat, lng" per line (as Google Maps shows when you right-click a point)
    """
    text = (text or "").strip()
    if not text:
        raise PolygonError("الحدود فاضية")
    pts: list[list[float]] = []
    m = re.search(r"<coordinates>(.*?)</coordinates>", text, re.S | re.I)
    if m:
        for tup in m.group(1).split():
            parts = tup.split(",")
            if len(parts) >= 2:
                pts.append([float(parts[1]), float(parts[0])])
    elif text.startswith("["):
        try:
            pts = [[float(a), float(b)] for a, b in json.loads(text)]
        except (ValueError, TypeError) as e:
            raise PolygonError("صيغة JSON غير صحيحة") from e
    else:
        for line in text.splitlines():
            nums = re.findall(r"-?\d+(?:\.\d+)?", line)
            if len(nums) >= 2:
                pts.append([float(nums[0]), float(nums[1])])
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        raise PolygonError("لازم 3 نقاط على الأقل")
    for lat, lng in pts:
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise PolygonError(f"إحداثية غير صحيحة: {lat}, {lng}")
    return pts


def point_in_polygon(lat: float, lng: float, poly: list[list[float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < x_cross:
                inside = not inside
        j = i
    return inside


def load(poly_json: str | None) -> list[list[float]] | None:
    return json.loads(poly_json) if poly_json else None
