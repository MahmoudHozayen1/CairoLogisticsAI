"""Shared, deterministic feature engineering for the predictive ML layer.

The same functions build features at **training** time (from the synthetic
history) and at **serving** time (from a live :class:`~app.models.Shipment`),
which guarantees the model sees the same feature distribution in both places.

Everything here is pure ``numpy``/``pandas`` and free of Flask / network / ORM
imports, so it can run inside scripts, tests and web requests alike.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

EARTH_RADIUS_KM = 6371.0

# Free-flow city driving speed (km/h) per vehicle. Encoding the vehicle as an
# interpretable numeric speed (rather than a one-hot) makes the per-prediction
# reasoning read naturally, e.g. "slower Van in traffic -> +6 min".
VEHICLE_SPEED_KMH = {
    "Bicycle": 12.0,
    "Motorcycle": 26.0,
    "Car": 22.0,
    "Van": 18.0,
}
DEFAULT_VEHICLE_SPEED = 22.0

# Human-readable labels used by the reasoning panels on the AI dashboard.
FEATURE_LABELS = {
    "dropoff_distance_km": "Distance to customer",
    "pickup_distance_km": "Distance to store",
    "traffic_factor": "Traffic congestion",
    "hour": "Time of day",
    "dow": "Day of week",
    "weight_kg": "Parcel weight",
    "has_cod": "Cash-on-delivery stop",
    "vehicle_speed": "Vehicle speed",
    "stop_sequence": "Position in route",
    "parcels_on_route": "Parcels on route",
    "store_congestion": "Store busyness",
    "promised_minutes": "Promised SLA window",
}

# Feature sets for each model. These are the exact columns the estimators train
# and predict on (in this order).
DROPOFF_FEATURES = [
    "dropoff_distance_km", "traffic_factor", "hour", "dow", "weight_kg",
    "has_cod", "vehicle_speed", "stop_sequence", "parcels_on_route",
]
PICKUP_FEATURES = [
    "pickup_distance_km", "hour", "dow", "weight_kg",
    "store_congestion", "parcels_on_route",
]
LATE_FEATURES = [
    "dropoff_distance_km", "traffic_factor", "hour", "dow", "weight_kg",
    "vehicle_speed", "promised_minutes", "stop_sequence",
]


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres between two WGS84 points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def vehicle_speed_kmh(vehicle_type) -> float:
    return VEHICLE_SPEED_KMH.get((vehicle_type or "").strip(), DEFAULT_VEHICLE_SPEED)


def traffic_factor(hour: int, dow: int) -> float:
    """Congestion multiplier (>= 1.0), mirroring the app's rush-hour model.

    Peaks in the morning (~09:00) and evening (~18:00); the Cairo weekend
    (Friday, then Saturday) is lighter. ``1.0`` means free-flow.
    """
    peak = 1.0
    peak += 0.55 * math.exp(-((hour - 9) ** 2) / (2 * 1.6 ** 2))   # morning rush
    peak += 0.80 * math.exp(-((hour - 18) ** 2) / (2 * 2.0 ** 2))  # evening rush
    if dow == 4:        # Friday - lightest (Cairo weekend)
        peak = 1.0 + (peak - 1.0) * 0.35
    elif dow == 5:      # Saturday
        peak = 1.0 + (peak - 1.0) * 0.70
    return round(peak, 3)


def store_congestion(hour: int) -> float:
    """Store busyness in [0, 1]; higher mid-morning and early evening."""
    morning = math.exp(-((hour - 10) ** 2) / (2 * 2.0 ** 2))
    evening = math.exp(-((hour - 18) ** 2) / (2 * 2.2 ** 2))
    return round(min(1.0, 0.15 + 0.85 * max(morning, evening)), 3)


# SLA promise = free-flow travel * speed-buffer + fixed buffer. Defined once so
# the synthetic history (training) and live serving compute an identical target.
SLA_SPEED_BUFFER = 1.30
SLA_FIXED_BUFFER = 9.0


def sla_promised_minutes(distance_km: float, vehicle_speed: float) -> float:
    """The promised delivery window (minutes) used to define lateness."""
    return distance_km / vehicle_speed * 60.0 * SLA_SPEED_BUFFER + SLA_FIXED_BUFFER


def _as_frame(data) -> pd.DataFrame:
    if isinstance(data, pd.DataFrame):
        return data
    if isinstance(data, dict):
        return pd.DataFrame([data])
    return pd.DataFrame(data)


def build_features(data, feature_set: str) -> pd.DataFrame:
    """Derive the model-ready numeric feature matrix from raw records.

    ``data`` may be a DataFrame (training) or a single dict (serving). Any
    columns not supplied are derived where possible; the caller/back-fill layer
    is responsible for imputing anything still missing.
    """
    df = _as_frame(data).copy()

    if "has_cod" not in df and "cod_amount" in df:
        df["has_cod"] = (pd.to_numeric(df["cod_amount"], errors="coerce").fillna(0) > 0).astype(int)
    if "vehicle_speed" not in df and "vehicle_type" in df:
        df["vehicle_speed"] = df["vehicle_type"].map(vehicle_speed_kmh).fillna(DEFAULT_VEHICLE_SPEED)

    cols = {
        "dropoff": DROPOFF_FEATURES,
        "pickup": PICKUP_FEATURES,
        "late": LATE_FEATURES,
    }[feature_set]

    for c in cols:
        if c not in df:
            df[c] = np.nan
    return df[cols].apply(pd.to_numeric, errors="coerce")


def label_for(feature: str) -> str:
    return FEATURE_LABELS.get(feature, feature.replace("_", " ").title())
