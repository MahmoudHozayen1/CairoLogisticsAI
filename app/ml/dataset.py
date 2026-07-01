"""Deterministic synthetic delivery history for the predictive ML layer.

Real operational data (a few dozen demo shipments) is far too small to train or
evaluate models, so we generate a large, believable history of *completed*
deliveries with a fixed seed. The generative process bakes in the real signals
the models are expected to learn:

* distance / traffic / vehicle drive delivery time,
* store busyness drives pickup time,
* an SLA buffer that traffic and noise occasionally blow through -> lateness,
* a growth trend + weekly seasonality in daily order volume and cost.

The result is cached to ``instance/ml/history.csv`` and is reproducible: the same
seed always yields the same table, so training metrics are stable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import paths
from .features import (
    haversine_km, sla_promised_minutes, store_congestion, traffic_factor,
    vehicle_speed_kmh,
)

SEED = 42
DEFAULT_DAYS = 180

# Two hubs (Maadi, Nasr City) with the delivery footprint the app already uses.
HUBS = [
    {"hub_id": 1, "name": "Maadi Hub", "lat": 29.9600, "lon": 31.2569},
    {"hub_id": 2, "name": "Nasr City Hub", "lat": 30.0566, "lon": 31.3300},
]
VEHICLES = ["Bicycle", "Motorcycle", "Car", "Van"]
VEHICLE_P = [0.10, 0.45, 0.30, 0.15]

# Weekly order-volume multipliers (Mon..Sun). Cairo weekend = Fri/Sat quieter.
WEEK_MULT = [1.05, 1.02, 1.00, 1.08, 0.70, 0.78, 0.95]
BASE_ORDERS = 20.0        # orders/day at the start of the window
GROWTH_PER_DAY = 0.16     # linear growth -> visible upward trend for forecasting

# Customer-note fragments (used later by the NLP slice; included so the history
# is genuinely rich end-to-end).
NOTE_FRAGMENTS = [
    "please call before arriving",
    "fragile, handle with care",
    "leave with the doorman",
    "deliver between 6 and 9 pm",
    "do not place other items on top",
    "ring the bell twice",
    "meet me at the building gate",
    "flat is on the 4th floor, no lift",
    "cash ready, exact change",
    "leave at reception if I don't answer",
]


def _sample_hour(rng: np.random.Generator) -> int:
    """Delivery hour, concentrated across the working day with rush-hour mass."""
    hours = np.arange(7, 22)
    weights = np.array([
        0.5, 0.8, 1.2, 1.3, 1.1, 1.0, 1.0, 1.0, 1.1, 1.3, 1.5, 1.4, 1.0, 0.7, 0.4
    ])
    weights = weights / weights.sum()
    return int(rng.choice(hours, p=weights))


def _sample_notes(rng: np.random.Generator) -> str:
    k = rng.choice([0, 1, 2], p=[0.45, 0.4, 0.15])
    if k == 0:
        return ""
    idx = rng.choice(len(NOTE_FRAGMENTS), size=int(k), replace=False)
    return "; ".join(NOTE_FRAGMENTS[i] for i in idx)


def generate_history(n_days: int = DEFAULT_DAYS, seed: int = SEED) -> pd.DataFrame:
    """Build the synthetic history as a DataFrame (no disk I/O)."""
    rng = np.random.default_rng(seed)
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                             microsecond=0, tzinfo=None)
    start = end - timedelta(days=n_days - 1)

    rows = []
    rec = 0
    for d in range(n_days):
        date = start + timedelta(days=d)
        dow = date.weekday()
        lam = max(1.0, (BASE_ORDERS + GROWTH_PER_DAY * d) * WEEK_MULT[dow])
        n = int(rng.poisson(lam))

        for _ in range(n):
            rec += 1
            hub = HUBS[int(rng.integers(0, len(HUBS)))]
            vehicle = str(rng.choice(VEHICLES, p=VEHICLE_P))
            hour = _sample_hour(rng)
            minute = int(rng.integers(0, 60))

            # Geometry: customer and store points scattered around the hub.
            drop_dist = float(np.clip(rng.gamma(2.2, 1.9), 0.4, 14.0))
            pick_dist = float(np.clip(rng.gamma(1.8, 1.4), 0.3, 9.0))
            weight = float(np.clip(rng.gamma(2.0, 1.4), 0.2, 18.0))
            cod = 0.0
            if rng.random() < 0.55:
                cod = float(np.round(rng.uniform(80, 1500), 0))

            batch = int(np.clip(rng.poisson(6) + 1, 1, 15))
            stop_seq = int(rng.integers(1, batch + 1))

            traffic = traffic_factor(hour, dow) * float(np.clip(rng.normal(1.0, 0.06), 0.85, 1.25))
            vspeed = vehicle_speed_kmh(vehicle)
            store_busy = store_congestion(hour) * float(np.clip(rng.normal(1.0, 0.08), 0.7, 1.3))

            # --- Targets (the quantities the models learn) ---
            travel_min = drop_dist / (vspeed / traffic) * 60.0
            handover = 3.0 + weight * 0.35 + (2.5 if cod > 0 else 0.0) + rng.normal(0, 1.4)
            delivery_minutes = float(max(2.0, travel_min + handover + rng.normal(0, 3.0)))

            pickup_wait = float(max(1.0, 4.0 + store_busy * 7.0 + weight * 0.25
                                    + 0.5 * batch + pick_dist * 0.2 + rng.normal(0, 1.6)))

            # SLA promised on a free-flow estimate plus a fixed buffer; traffic /
            # noise push some deliveries past it -> a realistic late rate.
            promised = sla_promised_minutes(drop_dist, vspeed)
            late = int(delivery_minutes > promised)

            cost = (20.0 + 3.5 * drop_dist + 4.0 * weight + 0.008 * cod
                    + (6.0 if vehicle == "Van" else 0.0))

            created_at = date + timedelta(hours=int(hour), minutes=minute)
            rows.append({
                "record_id": rec,
                "created_at": created_at.isoformat(),
                "day_index": d,
                "dow": dow,
                "hour": hour,
                "hub_id": hub["hub_id"],
                "vehicle_type": vehicle,
                "dropoff_distance_km": round(drop_dist, 3),
                "pickup_distance_km": round(pick_dist, 3),
                "weight_kg": round(weight, 2),
                "cod_amount": round(cod, 2),
                "traffic_factor": round(traffic, 3),
                "store_congestion": round(store_busy, 3),
                "stop_sequence": stop_seq,
                "parcels_on_route": batch,
                "promised_minutes": round(promised, 2),
                "delivery_minutes": round(delivery_minutes, 2),
                "pickup_wait_minutes": round(pickup_wait, 2),
                "late": late,
                "cost_egp": round(cost, 2),
                "notes": _sample_notes(rng),
            })

    return pd.DataFrame(rows)


def load_history(regenerate: bool = False, n_days: int = DEFAULT_DAYS) -> pd.DataFrame:
    """Return the cached history, generating (and caching) it on first use."""
    path = paths.history_csv()
    if path.exists() and not regenerate:
        df = pd.read_csv(path)
    else:
        df = generate_history(n_days=n_days)
        df.to_csv(path, index=False)
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    return df


def daily_aggregates(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-day order count and total cost, for the demand/cost forecaster."""
    if df is None:
        df = load_history()
    df = df.copy()
    df["date"] = pd.to_datetime(df["created_at"]).dt.normalize()
    agg = (
        df.groupby("date")
        .agg(orders=("record_id", "count"), cost_egp=("cost_egp", "sum"))
        .reset_index()
        .sort_values("date")
        .reset_index(drop=True)
    )
    agg["dow"] = agg["date"].dt.weekday
    return agg
