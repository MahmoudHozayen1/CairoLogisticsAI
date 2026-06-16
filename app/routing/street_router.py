"""Street-following route geometry, traffic model and road-closure avoidance.

This module is intentionally dependency-free (standard library only). It powers
three things the maps need:

1. **Street geometry** – ``route_geometry(a, b, closures)`` returns a polyline
   that follows real roads. By default it asks a public **OSRM** server (no API
   key, results cached on disk). It degrades gracefully to a straight line when
   OSRM is disabled or unreachable, so the app never hangs or fails.

2. **Road-closure avoidance** – if the road geometry passes through an active
   closure, the router tries a perpendicular detour around it. If nothing clears
   the closure the leg is returned with ``blocked=True`` so the UI can warn.

3. **Traffic model** – ``build_overlay(points, closures)`` splits a polyline into
   coloured segments by congestion level (free / moderate / heavy / severe). The
   congestion is a deterministic, time-of-day-aware *simulation* (there is no
   live traffic feed), peaking during the morning and evening rush hours.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.request
from datetime import datetime
from typing import List, Sequence

from flask import current_app

# Congestion levels and their map colours (Google-Maps style).
FREE, MODERATE, HEAVY, SEVERE = "free", "moderate", "heavy", "severe"
LEVEL_COLORS = {
    FREE: "#2ecc71",      # green
    MODERATE: "#f1c40f",  # amber
    HEAVY: "#e67e22",     # orange
    SEVERE: "#e74c3c",    # red
}
LEVEL_LABELS = {
    FREE: "Clear",
    MODERATE: "Moderate",
    HEAVY: "Busy",
    SEVERE: "Heavy / blocked",
}
# How much each level slows a courier down (travel-time multiplier).
LEVEL_FACTOR = {FREE: 1.0, MODERATE: 1.3, HEAVY: 1.7, SEVERE: 2.4}

# Simple circuit-breaker so we stop calling OSRM after repeated failures
# (e.g. offline) instead of timing out on every leg.
_OSRM_STATE = {"failures": 0, "disabled_until": 0.0}


# --------------------------------------------------------------------------- #
#  Geometry helpers
# --------------------------------------------------------------------------- #
def _haversine_km(p1: Sequence[float], p2: Sequence[float]) -> float:
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _path_distance_km(points: List[List[float]]) -> float:
    return sum(_haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1))


def _point_in_closure(pt: Sequence[float], closure: dict) -> bool:
    return _haversine_km(pt, [closure["lat"], closure["lon"]]) * 1000.0 <= closure["radius_m"]


def _path_blocked(points: List[List[float]], closures: List[dict]) -> bool:
    return any(_point_in_closure(p, c) for p in points for c in closures)


def _first_blocking_closure(points, closures):
    for p in points:
        for c in closures:
            if _point_in_closure(p, c):
                return c
    return None


def _perp_offset(a, b, center, dist_m, sign):
    """A point offset from ``center`` perpendicular to the a→b direction."""
    dlon, dlat = b[1] - a[1], b[0] - a[0]
    px, py = -dlat, dlon  # perpendicular in (lon, lat)
    norm = math.hypot(px, py) or 1.0
    px, py = px / norm, py / norm
    cos_lat = math.cos(math.radians(center[0])) or 1e-6
    via_lat = center[0] + sign * py * (dist_m / 111_320.0)
    via_lon = center[1] + sign * px * (dist_m / (111_320.0 * cos_lat))
    return [via_lat, via_lon]


# --------------------------------------------------------------------------- #
#  OSRM client (+ disk cache + circuit breaker)
# --------------------------------------------------------------------------- #
def _osrm_available() -> bool:
    return _OSRM_STATE["disabled_until"] <= time.time()


def _note_failure():
    _OSRM_STATE["failures"] += 1
    if _OSRM_STATE["failures"] >= 2:
        _OSRM_STATE["disabled_until"] = time.time() + 120  # back off 2 minutes


def _note_success():
    _OSRM_STATE["failures"] = 0
    _OSRM_STATE["disabled_until"] = 0.0


def _cache_dir() -> str:
    d = os.path.join(current_app.instance_path, "route_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_key(waypoints) -> str:
    raw = ";".join(f"{p[0]:.5f},{p[1]:.5f}" for p in waypoints)
    return hashlib.sha1(raw.encode()).hexdigest()


def _cache_get(waypoints):
    try:
        path = os.path.join(_cache_dir(), _cache_key(waypoints) + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _cache_put(waypoints, points):
    try:
        path = os.path.join(_cache_dir(), _cache_key(waypoints) + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(points, f)
    except Exception:
        pass


def _osrm(waypoints, base_url, timeout):
    """Query OSRM for road geometry through ``waypoints``. Returns [[lat, lon], …] or None."""
    coords = ";".join(f"{p[1]:.6f},{p[0]:.6f}" for p in waypoints)
    url = f"{base_url.rstrip('/')}/route/v1/driving/{coords}?overview=full&geometries=geojson"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SwiftRoute/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        return [[c[1], c[0]] for c in data["routes"][0]["geometry"]["coordinates"]]
    except Exception:
        return None


def road_path(a, b):
    """Road-following polyline a→b (cached). Falls back to a straight line."""
    cfg = current_app.config
    a, b = list(a), list(b)
    provider = cfg.get("ROUTING_PROVIDER", "osrm")

    if provider == "straight":
        return [a, b]

    if provider == "osmnx":
        try:  # lazy import to avoid a hard dependency and circular import
            from .optimizer import _street_path  # type: ignore
            return _street_path(a, b)
        except Exception:
            return [a, b]

    # Default: OSRM with disk cache + circuit breaker.
    if not _osrm_available():
        return [a, b]
    cached = _cache_get([a, b])
    if cached is not None:
        return cached
    points = _osrm([a, b], cfg["OSRM_BASE_URL"], cfg.get("ROUTING_TIMEOUT", 6))
    if points and len(points) >= 2:
        _note_success()
        _cache_put([a, b], points)
        return points
    _note_failure()
    return [a, b]


# --------------------------------------------------------------------------- #
#  Public API: geometry with closure avoidance
# --------------------------------------------------------------------------- #
def route_geometry(a, b, closures=None):
    """Return ``{"points", "blocked", "distance_km"}`` for the leg a→b.

    If the road geometry crosses an active closure, attempt a perpendicular
    detour; if no detour clears it, return the route with ``blocked=True``.
    """
    closures = closures or []
    a, b = list(a), list(b)
    points = road_path(a, b)

    blocked = False
    if closures and _path_blocked(points, closures):
        blocker = _first_blocking_closure(points, closures)
        detoured = None
        if blocker is not None and current_app.config.get("ROUTING_PROVIDER") == "osrm" and _osrm_available():
            for sign in (1, -1):
                via = _perp_offset(a, b, [blocker["lat"], blocker["lon"]], blocker["radius_m"] * 2.5, sign)
                cand = _cache_get([a, via, b])
                if cand is None:
                    cand = _osrm([a, via, b], current_app.config["OSRM_BASE_URL"],
                                 current_app.config.get("ROUTING_TIMEOUT", 6))
                    if cand:
                        _cache_put([a, via, b], cand)
                if cand and not _path_blocked(cand, closures):
                    detoured = cand
                    break
        if detoured:
            points = detoured
        else:
            blocked = True

    return {"points": points, "blocked": blocked, "distance_km": _path_distance_km(points)}


def active_closure_dicts():
    """Active closures as plain dicts (safe to use outside a request)."""
    from ..models import RoadClosure
    return [c.to_dict() for c in RoadClosure.query.filter_by(is_active=True).all()]


# --------------------------------------------------------------------------- #
#  Traffic model + map overlay
# --------------------------------------------------------------------------- #
def _time_factor(hour: float) -> float:
    """0..1 congestion factor across the day, peaking at the rush hours."""
    morning = math.exp(-((hour - 9.0) ** 2) / 3.0)
    evening = math.exp(-((hour - 18.0) ** 2) / 4.0)
    return min(1.0, 0.22 + 0.85 * max(morning, evening))


# Relative traffic per weekday (Mon=0 .. Sun=6). Cairo's working week runs
# Sunday–Thursday, so Friday is the quietest day and Saturday is light; the
# mid-week days carry the heaviest commuter load. Picking a calmer day/time is
# exactly what lets a dispatcher schedule a courier for the fastest run.
_DAY_FACTOR = {
    0: 1.00,  # Monday
    1: 1.00,  # Tuesday
    2: 1.00,  # Wednesday
    3: 0.97,  # Thursday (pre-weekend wind-down)
    4: 0.68,  # Friday  (weekend — lightest)
    5: 0.82,  # Saturday (weekend)
    6: 0.98,  # Sunday  (work day)
}


def _day_factor(weekday: int) -> float:
    """Traffic multiplier for a weekday (``datetime.weekday()``: Mon=0 .. Sun=6)."""
    return _DAY_FACTOR.get(weekday % 7, 1.0)


def congestion_for(lat, lon, when=None) -> str:
    """Deterministic, time-of-day- and day-of-week-aware congestion level."""
    when = when or datetime.now()
    key = f"{round(lat, 3)}:{round(lon, 3)}"
    road = (int(hashlib.sha1(key.encode()).hexdigest(), 16) % 1000) / 1000.0
    tod = _time_factor(when.hour + when.minute / 60.0) * _day_factor(when.weekday())
    score = 0.55 * tod + 0.45 * road
    if score < 0.40:
        return FREE
    if score < 0.62:
        return MODERATE
    if score < 0.80:
        return HEAVY
    return SEVERE


def traffic_factor_at(lat, lon, when=None) -> float:
    """Travel-time multiplier (>= 1.0) for a point at a given moment.

    Wraps :func:`congestion_for` and maps the resulting level onto its
    slow-down factor, so the optimiser can turn a planned dispatch *time* into a
    realistic ETA (rush hour stretches the same distance; a quiet Friday morning
    shrinks it).
    """
    return LEVEL_FACTOR[congestion_for(lat, lon, when)]


def build_overlay(points, closures=None, when=None):
    """Split a polyline into coloured congestion segments + closure flags.

    Returns ``{"segments": [...], "blocked": bool, "distance_km": float,
    "eta_factor": float}``. Each segment is ``{"points", "level", "color",
    "blocked"}`` ready to draw as a Leaflet polyline.
    """
    closures = closures or []
    if not points or len(points) < 2:
        return {"segments": [], "blocked": False, "distance_km": 0.0, "eta_factor": 1.0}

    when = when or datetime.now()
    n = len(points)
    target_chunks = max(1, min(18, n // 6 or 1))
    seg_size = max(1, n // target_chunks)

    segments = []
    blocked_any = False
    weighted_factor = 0.0
    total_km = 0.0
    i = 0
    while i < n - 1:
        j = min(i + seg_size, n - 1)
        chunk = points[i:j + 1]
        mid = chunk[len(chunk) // 2]
        seg_blocked = _path_blocked(chunk, closures)
        if seg_blocked:
            level = SEVERE
            blocked_any = True
        else:
            level = congestion_for(mid[0], mid[1], when)
        seg_km = _path_distance_km(chunk)
        total_km += seg_km
        weighted_factor += seg_km * LEVEL_FACTOR[level]
        segments.append({
            "points": chunk,
            "level": level,
            "color": LEVEL_COLORS[level],
            "blocked": seg_blocked,
        })
        i = j

    eta_factor = (weighted_factor / total_km) if total_km else 1.0
    return {
        "segments": segments,
        "blocked": blocked_any,
        "distance_km": round(total_km, 2),
        "eta_factor": round(eta_factor, 2),
    }
