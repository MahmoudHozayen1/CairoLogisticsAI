"""Courier behaviour modelling (Slice 5, ask #10).

Given a courier's GPS trace for a shift, we (1) reconstruct what they were doing
minute-by-minute — driving, delivering, idling or taking a break — using
dwell-time **stop detection** (the standard telematics technique), then (2) turn
each shift into behavioural features and learn courier *personas* by clustering.

* **Stop detection** — consecutive low-speed GPS points are grouped into "stop
  events"; a stop's dwell time classifies it as a brief idle (traffic/wait), a
  delivery, or a long break. No labels required, so it works on any raw trace.
* **Behaviour model** — many synthetic shifts (four hidden archetypes) are
  reduced to features (throughput, break ratio, idle ratio, detour, speeding, …)
  and clustered with K-Means into interpretable personas. Adjusted-Rand against
  the hidden archetypes confirms the clusters recover real behaviour.
* **Reasoning** — every courier's persona, productivity score and flags come with
  the feature deviations (z-scores vs the fleet) that produced them, satisfying
  the project-wide "show your reasoning" requirement.

Pure NumPy / scikit-learn, deterministic (seed 42), and — like the rest of the
ML layer — degrades to a rule-only summary if the trained artifact is missing.
"""
from __future__ import annotations

import numpy as np

from . import paths
from .features import haversine_km

SEED = 42
BEHAVIOR_ARTIFACT = "behavior"

# Speed / dwell thresholds for stop detection.
STOP_SPEED_KMH = 3.0          # below this a courier is "stopped"
IDLE_MAX_MIN = 2.0            # < 2 min stopped = a traffic/wait idle
DELIVERY_MAX_MIN = 10.0       # 2–10 min stopped = a delivery hand-over
CITY_SPEED_LIMIT_KMH = 45.0   # above this counts as speeding

# The clustering features (order defines the vector).
FEATURE_NAMES = (
    "deliveries_per_hour",   # throughput while actually working
    "break_ratio",           # share of the shift spent on long breaks
    "idle_ratio",            # share of the shift spent idling
    "detour_ratio",          # driven distance / ideal route distance
    "avg_speed_kmh",         # average moving speed
    "avg_delivery_min",      # average time spent per delivery stop
    "stopped_ratio",         # share of the shift not moving
)
FEATURE_LABELS = {
    "deliveries_per_hour": "deliveries per active hour",
    "break_ratio": "time on long breaks",
    "idle_ratio": "time idling",
    "detour_ratio": "route detour",
    "avg_speed_kmh": "average driving speed",
    "avg_delivery_min": "time per delivery",
    "stopped_ratio": "time stopped",
}
# Whether a *higher* value is operationally good (+1) or bad (-1). Drives the
# plain-English direction in the reasoning ("above/below the fleet average").
FEATURE_POLARITY = {
    "deliveries_per_hour": +1,
    "break_ratio": -1,
    "idle_ratio": -1,
    "detour_ratio": -1,
    "avg_speed_kmh": 0,
    "avg_delivery_min": -1,
    "stopped_ratio": -1,
}
N_FEATURES = len(FEATURE_NAMES)

# Four hidden behaviour archetypes used to synthesise training traces.
ARCHETYPES = {
    "efficient":  dict(speed=26, speed_sd=3, delivery=(2, 5), n_break=(0, 1),
                       break_len=(8, 12), idle_prob=0.05, detour=1.05, deliveries=(15, 21)),
    "steady":     dict(speed=21, speed_sd=4, delivery=(3, 7), n_break=(1, 1),
                       break_len=(12, 18), idle_prob=0.12, detour=1.12, deliveries=(11, 16)),
    "idle_prone": dict(speed=18, speed_sd=4, delivery=(4, 9), n_break=(2, 3),
                       break_len=(18, 32), idle_prob=0.30, detour=1.20, deliveries=(7, 12)),
    "wanderer":   dict(speed=24, speed_sd=9, delivery=(2, 8), n_break=(1, 2),
                       break_len=(10, 22), idle_prob=0.22, detour=1.45, deliveries=(9, 15)),
}
ARCHETYPE_ORDER = ("efficient", "steady", "idle_prone", "wanderer")

HUBS = [(29.9600, 31.2569), (30.0566, 31.3300)]


# --------------------------------------------------------------------------- #
# GPS trace simulation
# --------------------------------------------------------------------------- #
def _offset(lat, lon, km_e, km_n):
    """Shift a point by (east, north) kilometres (small-angle approximation)."""
    dlat = km_n / 110.574
    dlon = km_e / (111.320 * np.cos(np.radians(lat)))
    return lat + dlat, lon + dlon


def simulate_shift(archetype: str, seed: int = 0, hub=None) -> dict:
    """Synthesize a GPS trace (list of {t, lat, lon}) for one courier shift."""
    rng = np.random.default_rng(seed)
    p = ARCHETYPES[archetype]
    hub = hub or HUBS[seed % len(HUBS)]

    n_del = int(rng.integers(p["deliveries"][0], p["deliveries"][1] + 1))
    # Delivery points scattered 0.5–6 km around the hub.
    pts = []
    for _ in range(n_del):
        ang = rng.uniform(0, 2 * np.pi)
        r = float(np.clip(rng.gamma(2.0, 1.4), 0.4, 6.5))
        pts.append(_offset(hub[0], hub[1], r * np.cos(ang), r * np.sin(ang)))
    # Nearest-neighbour visiting order from the hub (a sensible base path).
    order, cur, remaining = [], hub, list(range(len(pts)))
    while remaining:
        j = min(remaining, key=lambda k: haversine_km(cur[0], cur[1], pts[k][0], pts[k][1]))
        order.append(j)
        cur = pts[j]
        remaining.remove(j)
    ideal_km = 0.0
    cur = hub
    for j in order:
        ideal_km += haversine_km(cur[0], cur[1], pts[j][0], pts[j][1])
        cur = pts[j]

    trace = []
    t = 0.0  # seconds
    cur = hub
    trace.append({"t": 0.0, "lat": hub[0], "lon": hub[1]})
    n_breaks = int(rng.integers(p["n_break"][0], p["n_break"][1] + 1))
    break_after = set(rng.choice(len(order), size=min(n_breaks, len(order)),
                                 replace=False).tolist()) if order else set()

    for stop_i, j in enumerate(order):
        dest = pts[j]
        base_km = haversine_km(cur[0], cur[1], dest[0], dest[1])
        detour = p["detour"] * float(np.clip(rng.normal(1.0, 0.05), 0.9, 1.4))
        speed = max(6.0, rng.normal(p["speed"], p["speed_sd"]))
        leg_min = base_km * detour / speed * 60.0
        steps = max(1, int(np.ceil(leg_min)))
        # Perpendicular bulge so the driven path is ~detour x the straight line
        # (a realistic dogleg), which is what the detour feature measures.
        h = (base_km / 2.0) * np.sqrt(max(detour ** 2 - 1.0, 0.0))
        dl = dest[0] - cur[0]
        dn = dest[1] - cur[1]
        norm = np.hypot(dl, dn) or 1.0
        perp_e, perp_n = -dn / norm, dl / norm            # unit perpendicular
        side = 1.0 if rng.random() < 0.5 else -1.0
        for s in range(1, steps + 1):
            f = s / steps
            lat = cur[0] + dl * f
            lon = cur[1] + dn * f
            bulge = h * (1.0 - abs(2.0 * f - 1.0)) * side  # triangular, peak mid-leg
            lat, lon = _offset(lat, lon, perp_e * bulge, perp_n * bulge)
            # GPS jitter
            lat, lon = _offset(lat, lon, rng.normal(0, 0.01), rng.normal(0, 0.01))
            t += 60.0
            trace.append({"t": t, "lat": lat, "lon": lon})
            # occasional short idle mid-leg (traffic light / waiting)
            if rng.random() < p["idle_prob"] / max(1, steps):
                idle_s = float(rng.uniform(40, 110))
                t += idle_s
                trace.append({"t": t, "lat": lat, "lon": lon})
        cur = dest
        # delivery dwell
        dwell = float(rng.uniform(p["delivery"][0], p["delivery"][1])) * 60.0
        t += dwell
        trace.append({"t": t, "lat": cur[0], "lon": cur[1]})
        # long break
        if stop_i in break_after:
            brk = float(rng.uniform(p["break_len"][0], p["break_len"][1])) * 60.0
            t += brk
            trace.append({"t": t, "lat": cur[0], "lon": cur[1]})

    return {"trace": trace, "archetype": archetype, "n_deliveries": n_del,
            "ideal_km": ideal_km, "hub": list(hub)}


# --------------------------------------------------------------------------- #
# Stop detection + feature extraction
# --------------------------------------------------------------------------- #
def detect_states(trace: list[dict], ideal_km: float | None = None) -> dict:
    """Classify each GPS point (driving/delivery/idle/break) and summarise."""
    n = len(trace)
    if n < 2:
        return {"points": [{"lat": p["lat"], "lon": p["lon"], "state": "driving"}
                           for p in trace], "summary": _empty_summary(), "stops": []}

    lat = np.array([p["lat"] for p in trace])
    lon = np.array([p["lon"] for p in trace])
    t = np.array([p["t"] for p in trace])

    leg_km = np.zeros(n)
    dt_h = np.full(n, 1e-9)
    for i in range(1, n):
        leg_km[i] = haversine_km(lat[i - 1], lon[i - 1], lat[i], lon[i])
        dt_h[i] = max((t[i] - t[i - 1]) / 3600.0, 1e-9)
    speed = leg_km / dt_h                       # km/h into point i
    speed[0] = 0.0

    stopped = speed < STOP_SPEED_KMH            # point i is "stopped"
    stopped[0] = True

    # Group consecutive stopped points into stop events. A stop's dwell runs from
    # the arrival (the last moving point, t[i-1]) to the last stopped point.
    states = ["driving"] * n
    stops = []
    i = 0
    while i < n:
        if stopped[i]:
            j = i
            while j + 1 < n and stopped[j + 1]:
                j += 1
            start_t = t[i - 1] if i > 0 else t[i]
            dwell_min = (t[j] - start_t) / 60.0
            if dwell_min < IDLE_MAX_MIN:
                cls = "idle"
            elif dwell_min <= DELIVERY_MAX_MIN:
                cls = "delivery"
            else:
                cls = "break"
            for k in range(i, j + 1):
                states[k] = cls
            stops.append({"state": cls, "dwell_min": round(dwell_min, 2),
                          "lat": float(lat[i]), "lon": float(lon[i])})
            i = j + 1
        else:
            i += 1

    moving = ~stopped
    moving_km = float(leg_km[moving].sum())
    moving_h = float(dt_h[moving].sum())
    total_min = float((t[-1] - t[0]) / 60.0)

    def _sum_state(name):
        m = 0.0
        for s in stops:
            if s["state"] == name:
                m += s["dwell_min"]
        return m

    idle_min = _sum_state("idle")
    break_min = _sum_state("break")
    delivery_stops = [s for s in stops if s["state"] == "delivery"]
    n_deliveries = len(delivery_stops)
    avg_delivery_min = (sum(s["dwell_min"] for s in delivery_stops) / n_deliveries
                        if n_deliveries else 0.0)
    stopped_min = total_min - (moving_h * 60.0)

    avg_speed = moving_km / moving_h if moving_h > 0 else 0.0
    max_speed = float(speed.max()) if n > 1 else 0.0
    speeding_ratio = float((dt_h[moving & (speed > CITY_SPEED_LIMIT_KMH)].sum())
                           / moving_h) if moving_h > 0 else 0.0
    detour_ratio = (moving_km / ideal_km) if ideal_km and ideal_km > 0 else 1.0
    deliveries_per_hour = (n_deliveries / moving_h) if moving_h > 0 else 0.0

    summary = {
        "distance_km": round(moving_km, 2),
        "duration_min": round(total_min, 1),
        "moving_min": round(moving_h * 60.0, 1),
        "stopped_min": round(stopped_min, 1),
        "n_deliveries": n_deliveries,
        "n_breaks": sum(1 for s in stops if s["state"] == "break"),
        "n_idle": sum(1 for s in stops if s["state"] == "idle"),
        "break_min": round(break_min, 1),
        "idle_min": round(idle_min, 1),
        "avg_speed_kmh": round(avg_speed, 1),
        "max_speed_kmh": round(max_speed, 1),
        "avg_delivery_min": round(avg_delivery_min, 2),
        "deliveries_per_hour": round(deliveries_per_hour, 2),
        "break_ratio": round(break_min / total_min, 4) if total_min else 0.0,
        "idle_ratio": round(idle_min / total_min, 4) if total_min else 0.0,
        "detour_ratio": round(detour_ratio, 3),
        "speeding_ratio": round(speeding_ratio, 4),
        "stopped_ratio": round(stopped_min / total_min, 4) if total_min else 0.0,
    }
    points = [{"lat": float(lat[i]), "lon": float(lon[i]), "state": states[i]}
              for i in range(n)]
    return {"points": points, "summary": summary, "stops": stops}


def _empty_summary():
    return {k: 0.0 for k in (
        "distance_km", "duration_min", "moving_min", "stopped_min", "n_deliveries",
        "n_breaks", "n_idle", "break_min", "idle_min", "avg_speed_kmh",
        "max_speed_kmh", "avg_delivery_min", "deliveries_per_hour", "break_ratio",
        "idle_ratio", "detour_ratio", "speeding_ratio", "stopped_ratio")}


def feature_vector(summary: dict) -> np.ndarray:
    return np.array([summary.get(k, 0.0) for k in FEATURE_NAMES], float)


# --------------------------------------------------------------------------- #
# Training (K-Means persona clustering)
# --------------------------------------------------------------------------- #
def build_behavior_dataset(seed: int = SEED, shifts_per_archetype: int = 60):
    """Simulate many shifts and return (X, feature summaries, archetype labels)."""
    rows, summaries, labels = [], [], []
    sid = 0
    for a_idx, arch in enumerate(ARCHETYPE_ORDER):
        for _ in range(shifts_per_archetype):
            sid += 1
            shift = simulate_shift(arch, seed=seed * 1000 + sid)
            det = detect_states(shift["trace"], ideal_km=shift["ideal_km"])
            rows.append(feature_vector(det["summary"]))
            summaries.append(det["summary"])
            labels.append(a_idx)
    return np.array(rows), summaries, np.array(labels)


def _productivity(summary: dict, fleet_mean: np.ndarray, fleet_std: np.ndarray) -> float:
    """0–100 productivity score from feature z-scores weighted by polarity."""
    x = feature_vector(summary)
    z = (x - fleet_mean) / fleet_std
    weights = np.array([1.4, 1.2, 1.0, 0.9, 0.0, 0.8, 0.7])  # per FEATURE_NAMES
    polarity = np.array([FEATURE_POLARITY[k] for k in FEATURE_NAMES], float)
    signal = float((z * polarity * weights).sum() / weights.sum())
    return round(float(100.0 / (1.0 + np.exp(-signal))), 1)


def train_behavior(seed: int = SEED, shifts_per_archetype: int = 60):
    """Cluster shift features into personas; return (bundle, metrics)."""
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import silhouette_score, adjusted_rand_score

    X, summaries, labels = build_behavior_dataset(seed, shifts_per_archetype)
    fleet_mean = X.mean(0)
    fleet_std = X.std(0) + 1e-9

    scaler = StandardScaler().fit(X)
    Xs = scaler.transform(X)
    km = KMeans(n_clusters=4, n_init=10, random_state=seed).fit(Xs)

    sil = float(silhouette_score(Xs, km.labels_))
    ari = float(adjusted_rand_score(labels, km.labels_))

    # Rank clusters by mean productivity -> persona tier + most distinctive trait.
    prod = np.array([_productivity(s, fleet_mean, fleet_std) for s in summaries])
    centroids_raw = np.array([X[km.labels_ == c].mean(0) for c in range(4)])
    cluster_prod = np.array([prod[km.labels_ == c].mean() for c in range(4)])
    order = np.argsort(-cluster_prod)            # best first
    tier_names = ["Efficient", "Steady", "Idle-prone", "At-risk"]
    persona_map = {}
    global_mean_z = (centroids_raw - fleet_mean) / fleet_std
    for rank, c in enumerate(order):
        z = global_mean_z[c]
        # most distinctive feature (largest deviation) for a descriptive tag
        fi = int(np.argmax(np.abs(z)))
        trait = FEATURE_LABELS[FEATURE_NAMES[fi]]
        direction = "high" if z[fi] > 0 else "low"
        persona_map[int(c)] = {
            "name": tier_names[rank],
            "trait": f"{direction} {trait}",
            "productivity": round(float(cluster_prod[c]), 1),
        }

    sizes = {persona_map[c]["name"]: int((km.labels_ == c).sum()) for c in range(4)}

    bundle = {
        "scaler_mean": scaler.mean_.tolist(),
        "scaler_scale": scaler.scale_.tolist(),
        "centroids": km.cluster_centers_.tolist(),
        "persona_map": {str(k): v for k, v in persona_map.items()},
        "fleet_mean": fleet_mean.tolist(),
        "fleet_std": fleet_std.tolist(),
        "feature_names": list(FEATURE_NAMES),
        "kind": "behavior_kmeans",
    }
    metrics = {
        "n_shifts": int(len(X)),
        "silhouette": round(sil, 3),
        "adjusted_rand_vs_archetypes": round(ari, 3),
        "persona_sizes": sizes,
        "personas": {persona_map[c]["name"]: persona_map[c] for c in range(4)},
    }
    return bundle, metrics


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #
class BehaviorModel:
    """Analyse a courier shift: persona, productivity and explained flags."""

    def __init__(self, bundle: dict | None = None):
        self.bundle = bundle
        if bundle:
            self.mean = np.asarray(bundle["scaler_mean"])
            self.scale = np.asarray(bundle["scaler_scale"])
            self.centroids = np.asarray(bundle["centroids"])
            self.persona_map = {int(k): v for k, v in bundle["persona_map"].items()}
            self.fleet_mean = np.asarray(bundle["fleet_mean"])
            self.fleet_std = np.asarray(bundle["fleet_std"])
        else:
            self.fleet_mean = None
            self.fleet_std = None

    @property
    def available(self) -> bool:
        return self.bundle is not None

    def _zscores(self, summary):
        x = feature_vector(summary)
        fm = self.fleet_mean if self.fleet_mean is not None else x
        fs = self.fleet_std if self.fleet_std is not None else np.ones_like(x)
        return (x - fm) / fs

    def _reasoning(self, z):
        out = []
        idx = np.argsort(-np.abs(z))[:3]
        for i in idx:
            key = FEATURE_NAMES[i]
            pol = FEATURE_POLARITY[key]
            above = z[i] > 0
            if pol == 0:
                verdict = "note"
            else:
                good = (above and pol > 0) or (not above and pol < 0)
                verdict = "good" if good else "bad"
            out.append({
                "label": FEATURE_LABELS[key],
                "z": round(float(z[i]), 2),
                "direction": "above" if above else "below",
                "verdict": verdict,
            })
        return out

    def _flags(self, summary, z):
        flags = []
        zmap = {FEATURE_NAMES[i]: z[i] for i in range(N_FEATURES)}
        if zmap["break_ratio"] > 1.0 or summary["break_min"] > 25:
            flags.append("Long breaks")
        if zmap["idle_ratio"] > 1.0:
            flags.append("Frequent idling")
        if summary["detour_ratio"] > 1.3:
            flags.append("Detour-heavy route")
        if summary["speeding_ratio"] > 0.12:
            flags.append("Speeding")
        if zmap["deliveries_per_hour"] < -1.0:
            flags.append("Low throughput")
        return flags

    def analyze(self, trace: list[dict], ideal_km: float | None = None) -> dict:
        det = detect_states(trace, ideal_km=ideal_km)
        summary = det["summary"]
        z = self._zscores(summary)
        productivity = _productivity(
            summary,
            self.fleet_mean if self.fleet_mean is not None else feature_vector(summary),
            self.fleet_std if self.fleet_std is not None else np.ones(N_FEATURES),
        )
        persona = None
        confidence = None
        if self.available:
            xs = (feature_vector(summary) - self.mean) / self.scale
            d = np.linalg.norm(self.centroids - xs, axis=1)
            c = int(np.argmin(d))
            persona = self.persona_map[c]
            sim = np.exp(-d)
            confidence = round(float(sim[c] / sim.sum()), 3)
        return {
            "summary": summary,
            "points": det["points"],
            "stops": det["stops"],
            "persona": persona,
            "persona_confidence": confidence,
            "productivity_score": productivity,
            "flags": self._flags(summary, z),
            "reasoning": self._reasoning(z),
        }


# --------------------------------------------------------------------------- #
# Persistence + singleton
# --------------------------------------------------------------------------- #
def _artifact_path():
    return paths.model_path(BEHAVIOR_ARTIFACT)


def save_behavior(bundle: dict, metrics: dict) -> None:
    import joblib
    joblib.dump({"bundle": bundle, "metrics": metrics}, _artifact_path())


def load_behavior():
    import joblib
    p = _artifact_path()
    return joblib.load(p) if p.exists() else None


class BehaviorService:
    def __init__(self):
        self._model = None
        self._metrics = None

    def ensure_trained(self, force: bool = False):
        if force:
            bundle, metrics = train_behavior()
            save_behavior(bundle, metrics)
            self._model = BehaviorModel(bundle)
            self._metrics = metrics
            return
        if self._model is not None:
            return
        art = load_behavior()
        if art is None:
            bundle, metrics = train_behavior()
            save_behavior(bundle, metrics)
        else:
            bundle, metrics = art["bundle"], art["metrics"]
        self._model = BehaviorModel(bundle)
        self._metrics = metrics

    @property
    def is_trained(self) -> bool:
        return _artifact_path().exists()

    def analyze(self, trace, ideal_km=None):
        self.ensure_trained()
        return self._model.analyze(trace, ideal_km=ideal_km)

    def metrics(self):
        self.ensure_trained()
        return self._metrics or {}


_service = None


def get_behavior_model() -> BehaviorService:
    global _service
    if _service is None:
        _service = BehaviorService()
    return _service


def main():
    bundle, metrics = train_behavior()
    save_behavior(bundle, metrics)
    print("Courier behaviour model trained.")
    print(f"  shifts               : {metrics['n_shifts']}")
    print(f"  silhouette           : {metrics['silhouette']}")
    print(f"  ARI vs archetypes    : {metrics['adjusted_rand_vs_archetypes']}")
    print(f"  persona sizes        : {metrics['persona_sizes']}")


if __name__ == "__main__":
    main()
