"""AI-assisted delivery route optimization.

The optimizer solves a simplified *capacitated vehicle routing problem* (VRP):

1. **Assign** each shipment to its nearest hub (already implied by the hub the
   parcel sits in once it reaches the warehouse).
2. **Cluster** a hub's shipments into ``k`` balanced groups, one per available
   courier, using k-means on geographic coordinates.
3. **Sequence** each courier's stops with a nearest-neighbour heuristic (a fast
   approximation of the Travelling Salesman Problem), then refine with 2-opt.
4. **Geometry**: build the polyline drawn on the map. If OSMnx + a cached street
   graph are available we follow real roads (Dijkstra on travel-time); otherwise
   we fall back to straight lines so the system never fails.

Heavy scientific dependencies (numpy, scikit-learn, osmnx, networkx) are imported
lazily and are entirely optional — the module ships pure-Python fallbacks for
clustering, distance and routing so it runs flawlessly anywhere.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta
from typing import Callable, List, Sequence

from flask import current_app

from ..extensions import db
from ..models import Shipment, Hub, RouteStop, ShipmentStatus, User, Role
from .street_router import traffic_factor_at

# Average courier speed (km/h) used to convert distance into an ETA.
AVERAGE_SPEED_KMH = 22.0
SERVICE_TIME_MIN = 4.0  # minutes spent per stop handing over the parcel

# Human-readable weekday names (Mon=0 .. Sun=6), exposed for dispatch planning.
WEEKDAY_LABELS = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]


# --------------------------------------------------------------------------- #
#  Distance helpers
# --------------------------------------------------------------------------- #
def haversine_km(p1: Sequence[float], p2: Sequence[float]) -> float:
    """Great-circle distance between two ``[lat, lon]`` points in kilometres."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
#  Clustering  (k-means with a pure-Python fallback)
# --------------------------------------------------------------------------- #
def _kmeans_labels(coords: List[List[float]], k: int) -> List[int]:
    if k <= 1:
        return [0] * len(coords)
    try:  # Preferred: scikit-learn
        from sklearn.cluster import KMeans  # type: ignore
        import numpy as np  # type: ignore

        model = KMeans(n_clusters=k, random_state=42, n_init=10)
        return model.fit_predict(np.array(coords)).tolist()
    except Exception:
        return _kmeans_pure(coords, k)


def _kmeans_pure(coords: List[List[float]], k: int, iters: int = 25) -> List[int]:
    """Tiny deterministic k-means so we never hard-depend on scikit-learn."""
    rng = random.Random(42)
    centroids = rng.sample(coords, k)
    labels = [0] * len(coords)
    for _ in range(iters):
        # Assign
        for i, c in enumerate(coords):
            labels[i] = min(range(k), key=lambda j: haversine_km(c, centroids[j]))
        # Update
        new_centroids = []
        for j in range(k):
            members = [coords[i] for i in range(len(coords)) if labels[i] == j]
            if members:
                lat = sum(m[0] for m in members) / len(members)
                lon = sum(m[1] for m in members) / len(members)
                new_centroids.append([lat, lon])
            else:  # empty cluster -> reseed
                new_centroids.append(rng.choice(coords))
        if new_centroids == centroids:
            break
        centroids = new_centroids
    return labels


# --------------------------------------------------------------------------- #
#  Sequencing  (nearest-neighbour + 2-opt)
# --------------------------------------------------------------------------- #
def _nearest_neighbour(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    remaining = list(stops)
    ordered: List[Shipment] = []
    current = start
    while remaining:
        nxt = min(remaining, key=lambda s: haversine_km(current, s.coords))
        ordered.append(nxt)
        remaining.remove(nxt)
        current = nxt.coords
    return ordered


def _route_distance(start: List[float], stops: List[Shipment]) -> float:
    total, current = 0.0, start
    for s in stops:
        total += haversine_km(current, s.coords)
        current = s.coords
    return total


def _two_opt(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    """Local-search refinement of the nearest-neighbour tour."""
    if len(stops) < 4:
        return stops
    best = stops
    best_dist = _route_distance(start, best)
    improved = True
    while improved:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                candidate = best[:i] + best[i:j + 1][::-1] + best[j + 1:]
                d = _route_distance(start, candidate)
                if d < best_dist - 1e-9:
                    best, best_dist, improved = candidate, d, True
    return best


def _or_opt(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    """Relocate short chains of 1–3 stops to a cheaper position (Or-opt moves).

    Complements 2-opt (which only reverses segments) and often shaves the last
    few minutes off a tour. Restarts the scan whenever it finds an improvement.
    """
    if len(stops) < 5:
        return stops
    best = list(stops)
    best_dist = _route_distance(start, best)
    improved = True
    while improved:
        improved = False
        for seg_len in (1, 2, 3):
            for i in range(len(best) - seg_len + 1):
                segment = best[i:i + seg_len]
                rest = best[:i] + best[i + seg_len:]
                for j in range(len(rest) + 1):
                    candidate = rest[:j] + segment + rest[j:]
                    if candidate == best:
                        continue
                    d = _route_distance(start, candidate)
                    if d < best_dist - 1e-9:
                        best, best_dist, improved = candidate, d, True
                        break
                if improved:
                    break
            if improved:
                break
    return best


# --------------------------------------------------------------------------- #
#  Route-optimization techniques (selectable sequencing strategies)
# --------------------------------------------------------------------------- #
def _seq_fifo(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    return list(stops)


def _seq_nearest(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    return _nearest_neighbour(start, stops)


def _seq_two_opt(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    return _two_opt(start, _nearest_neighbour(start, stops))


def _seq_or_opt(start: List[float], stops: List[Shipment]) -> List[Shipment]:
    return _or_opt(start, _two_opt(start, _nearest_neighbour(start, stops)))


# Each technique: a label, a one-line description and the sequencing function.
STRATEGIES = {
    "fifo": {
        "label": "As received (FIFO)",
        "blurb": "Visit parcels in the order they arrived — no optimisation. Baseline to beat.",
        "func": _seq_fifo,
    },
    "nearest": {
        "label": "Nearest Neighbour",
        "blurb": "Always drive to the closest remaining stop. Fast greedy heuristic.",
        "func": _seq_nearest,
    },
    "two_opt": {
        "label": "Nearest Neighbour + 2-opt",
        "blurb": "Greedy tour refined by 2-opt edge swaps. Strong, reliable all-rounder.",
        "func": _seq_two_opt,
    },
    "or_opt": {
        "label": "2-opt + Or-opt (thorough)",
        "blurb": "Adds segment relocation on top of 2-opt. Highest quality, slightly slower.",
        "func": _seq_or_opt,
    },
}
# Display / evaluation order, from simplest to most thorough.
STRATEGY_ORDER = ["fifo", "nearest", "two_opt", "or_opt"]
DEFAULT_STRATEGY = "two_opt"


def _sequence(strategy: str, start: List[float], stops: List[Shipment]) -> List[Shipment]:
    """Order ``stops`` using the named technique (falls back to the default)."""
    spec = STRATEGIES.get(strategy) or STRATEGIES[DEFAULT_STRATEGY]
    return spec["func"](start, stops)


# --------------------------------------------------------------------------- #
#  Dispatch-time planning  (estimate ETAs for a chosen day & time)
# --------------------------------------------------------------------------- #
def _weekday_index(day) -> int | None:
    """Resolve a weekday name/abbreviation/index to 0..6, or ``None``."""
    if day is None:
        return None
    if isinstance(day, int):
        return day % 7
    s = str(day).strip().lower()
    if not s or s == "today":
        return None
    if s.isdigit():
        return int(s) % 7
    for i, name in enumerate(WEEKDAY_LABELS):
        if name.lower().startswith(s):
            return i
    return None


def resolve_departure(day=None, hour=None, minute=0, now=None) -> datetime:
    """Build a concrete dispatch ``datetime`` from a day and time-of-day.

    * ``day`` — ``None``/"today" keeps today; a weekday name or index 0..6 rolls
      forward to the next occurrence of that weekday.
    * ``hour`` — ``None`` means "right now" (the rest is ignored).
    """
    now = now or datetime.now()
    if hour is None:
        return now
    target = now.replace(hour=int(hour) % 24, minute=int(minute) % 60, second=0, microsecond=0)
    wd = _weekday_index(day)
    if wd is None:
        return target
    target += timedelta(days=(wd - now.weekday()) % 7)
    return target


def _estimate_route(start: List[float], ordered: List[Shipment], departure: datetime):
    """Distance (km) and duration (min) for a visiting order.

    The duration scales every leg by the simulated congestion at the moment the
    courier is expected to drive it (``departure`` + time already spent), so the
    estimate reflects the chosen dispatch day and time. Network-free.
    """
    total_km = 0.0
    minutes = 0.0
    current = start
    for s in ordered:
        leg_km = haversine_km(current, s.coords)
        when = departure + timedelta(minutes=minutes)
        factor = traffic_factor_at(s.coords[0], s.coords[1], when)
        minutes += (leg_km / AVERAGE_SPEED_KMH) * 60.0 * factor + SERVICE_TIME_MIN
        total_km += leg_km
        current = s.coords
    return total_km, minutes


def _routable_clusters(hubs):
    """Group routable parcels into per-courier clusters (shared by compare/persist).

    Returns a list of ``(hub, courier_or_None, [shipments])`` tuples using the
    same k-means assignment the optimiser persists, so previews and results
    line up exactly.
    """
    clusters = []
    for h in hubs:
        shipments = (
            Shipment.query.filter(
                Shipment.hub_id == h.id,
                Shipment.status.in_([ShipmentStatus.AT_WAREHOUSE, ShipmentStatus.OUT_FOR_DELIVERY]),
            ).all()
        )
        if not shipments:
            continue
        couriers = [c for c in h.couriers if c.is_active and c.is_available]
        if not couriers:
            clusters.append((h, None, shipments))
            continue
        k = min(len(shipments), len(couriers))
        labels = _kmeans_labels([s.coords for s in shipments], k)
        for c_idx in range(k):
            load = [shipments[i] for i in range(len(shipments)) if labels[i] == c_idx]
            if load:
                clusters.append((h, couriers[c_idx], load))
    return clusters


def compare_strategies(hub: Hub | None = None, departure: datetime | None = None) -> dict:
    """Estimate every technique on the current workload (no writes, no network).

    For each strategy we report total fleet distance and the *completion time*
    (makespan — when the last parcel lands, since couriers run in parallel) for
    the chosen ``departure``. The technique with the lowest completion time (then
    shortest distance, then simplest) is flagged ``recommended``.
    """
    departure = departure or datetime.now()
    hubs = [hub] if hub else Hub.query.all()
    clusters = _routable_clusters(hubs)

    results = []
    for key in STRATEGY_ORDER:
        func = STRATEGIES[key]["func"]
        total_km = 0.0
        makespan = 0.0
        for h, _courier, load in clusters:
            ordered = func(h.coords, load)
            d_km, d_min = _estimate_route(h.coords, ordered, departure)
            total_km += d_km
            makespan = max(makespan, d_min)
        results.append({
            "key": key,
            "label": STRATEGIES[key]["label"],
            "blurb": STRATEGIES[key]["blurb"],
            "distance_km": round(total_km, 2),
            "duration_min": int(round(makespan)),
        })

    if results:
        order = {k: i for i, k in enumerate(STRATEGY_ORDER)}
        best = min(results, key=lambda r: (r["duration_min"], r["distance_km"], order[r["key"]]))
        recommended = best["key"]
    else:
        recommended = DEFAULT_STRATEGY
    for r in results:
        r["recommended"] = (r["key"] == recommended)

    return {
        "departure": departure,
        "results": results,
        "recommended": recommended,
        "stops": sum(len(c[2]) for c in clusters),
        "couriers": sum(1 for c in clusters if c[1] is not None),
    }


# --------------------------------------------------------------------------- #
#  Street geometry  (optional OSMnx, cached on the app object)
# --------------------------------------------------------------------------- #
def _get_street_graph():
    """Lazily download & cache the street graph on the Flask app. Returns None on failure."""
    if not current_app.config.get("ENABLE_STREET_ROUTING"):
        return None
    if hasattr(current_app, "_street_graph"):
        return current_app._street_graph
    graph = None
    try:
        import osmnx as ox  # type: ignore

        center = (current_app.config["SERVICE_CENTER_LAT"], current_app.config["SERVICE_CENTER_LON"])
        graph = ox.graph_from_point(center, dist=current_app.config["SERVICE_RADIUS_M"], network_type="drive")
        graph = ox.add_edge_speeds(graph)
        graph = ox.add_edge_travel_times(graph)
    except Exception as exc:  # pragma: no cover - network/heavy dep
        current_app.logger.warning("Street routing disabled (%s)", exc)
        graph = None
    current_app._street_graph = graph
    return graph


def _street_path(a: List[float], b: List[float]) -> List[List[float]]:
    graph = _get_street_graph()
    if graph is None:
        return [a, b]
    try:
        import osmnx as ox  # type: ignore
        import networkx as nx  # type: ignore

        o = ox.distance.nearest_nodes(graph, X=a[1], Y=a[0])
        d = ox.distance.nearest_nodes(graph, X=b[1], Y=b[0])
        nodes = nx.shortest_path(graph, o, d, weight="travel_time")
        return [[graph.nodes[n]["y"], graph.nodes[n]["x"]] for n in nodes]
    except Exception:
        return [a, b]


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def optimize_and_persist(hub: Hub | None = None, departure: datetime | None = None,
                         strategy: str | None = None):
    """Optimise routes for ``hub`` (or every hub) and persist the result.

    Parameters
    ----------
    hub : limit the run to a single hub (default: all hubs).
    departure : the planned dispatch ``datetime``; ETAs are scaled by the
        simulated congestion at that day & time (default: now).
    strategy : the sequencing technique key (see :data:`STRATEGIES`). ``None`` or
        ``"auto"`` picks the recommended technique from :func:`compare_strategies`.

    Side effects (committed to the database):
      * unassigned/at-warehouse shipments get a ``courier_id`` and ``route_sequence``;
      * a ``RouteStop`` row (with map geometry + ETA) is written per shipment.

    Returns a summary dict suitable for rendering on the dashboard.
    """
    departure = departure or datetime.now()
    if strategy in (None, "", "auto"):
        strategy = compare_strategies(hub, departure)["recommended"]
    elif strategy not in STRATEGIES:
        strategy = DEFAULT_STRATEGY

    hubs = [hub] if hub else Hub.query.all()
    summary = {
        "routes": [], "total_distance_km": 0.0, "assigned": 0, "unassigned": 0,
        "strategy": strategy, "strategy_label": STRATEGIES[strategy]["label"],
        "departure": departure,
    }

    # Active road closures are avoided by the drawn geometry (see street_router).
    from .street_router import route_geometry, active_closure_dicts
    closures = active_closure_dicts()

    for h in hubs:
        # Only route parcels that are physically at the hub and not yet delivered.
        shipments = (
            Shipment.query.filter(
                Shipment.hub_id == h.id,
                Shipment.status.in_([ShipmentStatus.AT_WAREHOUSE, ShipmentStatus.OUT_FOR_DELIVERY]),
            ).all()
        )
        couriers = [c for c in h.couriers if c.is_active and c.is_available]
        if not shipments:
            continue
        if not couriers:
            summary["unassigned"] += len(shipments)
            continue

        k = min(len(shipments), len(couriers))
        coords = [s.coords for s in shipments]
        labels = _kmeans_labels(coords, k)

        for c_idx in range(k):
            courier = couriers[c_idx]
            load = [shipments[i] for i in range(len(shipments)) if labels[i] == c_idx]
            if not load:
                continue

            ordered = _sequence(strategy, h.coords, load)

            current = h.coords
            cumulative_min = 0.0
            route_info = {
                "hub": h.name,
                "courier": courier.name,
                "courier_id": courier.id,
                "stops": [],
                "distance_km": 0.0,
            }
            for seq, s in enumerate(ordered, start=1):
                # Road-following geometry that avoids active closures.
                geom = route_geometry(current, s.coords, closures)
                path = geom["points"]
                leg_km = geom["distance_km"] or haversine_km(current, s.coords)
                # Scale the leg by the traffic expected when the courier drives
                # it, so ETAs reflect the chosen dispatch day & time.
                when = departure + timedelta(minutes=cumulative_min)
                factor = traffic_factor_at(s.coords[0], s.coords[1], when)
                cumulative_min += (leg_km / AVERAGE_SPEED_KMH) * 60 * factor + SERVICE_TIME_MIN

                # Persist assignment on the shipment.
                s.courier_id = courier.id
                s.route_sequence = seq
                if s.status == ShipmentStatus.AT_WAREHOUSE:
                    s.add_event(
                        ShipmentStatus.OUT_FOR_DELIVERY,
                        note=f"Assigned to courier {courier.name}",
                        location=h.name,
                    )

                # Replace any previous route stop.
                RouteStop.query.filter_by(shipment_id=s.id).delete()
                db.session.add(RouteStop(
                    shipment_id=s.id, hub_id=h.id, courier_id=courier.id,
                    sequence=seq, path_json=json.dumps(path),
                    eta_minutes=int(cumulative_min),
                ))

                route_info["stops"].append({
                    "tracking_number": s.tracking_number,
                    "receiver": s.receiver_name,
                    "sequence": seq,
                    "eta_minutes": int(cumulative_min),
                })
                route_info["distance_km"] += leg_km
                current = s.coords

            summary["routes"].append(route_info)
            summary["total_distance_km"] += route_info["distance_km"]
            summary["assigned"] += len(ordered)

    db.session.commit()
    summary["total_distance_km"] = round(summary["total_distance_km"], 2)
    return summary
