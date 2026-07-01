"""The learning feedback loop: predicted vs. actual, drift and calibration.

Two halves:

* **Backtest** (:func:`feedback_report`) — replays every trained model over the
  full synthetic history and measures how far predictions land from the recorded
  actuals (MAE / bias / R²), how prediction error drifts week-over-week (the
  retraining trigger), and how well the late-risk probabilities are calibrated.
* **Live loop** (:func:`log_predictions` / :func:`resolve_predictions`) — records
  the prediction made for a real shipment at dispatch and, once it completes,
  fills in the actual outcome so the loop closes on live data too.
"""
from __future__ import annotations

from datetime import timezone

import numpy as np
import pandas as pd

from ..extensions import db
from ..models import PredictionLog, ShipmentStatus, utcnow
from . import registry
from .dataset import load_history
from .features import build_features
from .service import get_service

TARGETS = {"dropoff": "delivery_minutes", "pickup": "pickup_wait_minutes"}


# --------------------------------------------------------------------------- #
#  Backtest report
# --------------------------------------------------------------------------- #
def _model_predict(name, df):
    bundle = registry.load_model(name)
    X = build_features(df, bundle["feature_set"])
    for col in bundle["feature_names"]:
        if col in X:
            X[col] = X[col].fillna(bundle["medians"].get(col))
    model = bundle["model"]
    if bundle["kind"] == "classifier":
        return model.predict_proba(X)[:, 1], bundle
    return model.predict(X), bundle


def _regression_metrics(name, df):
    target = TARGETS[name]
    pred, _ = _model_predict(name, df)
    actual = df[target].to_numpy(dtype=float)
    resid = pred - actual
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((actual - actual.mean()) ** 2)) or 1.0
    return {
        "n": int(len(actual)),
        "mae": round(float(np.mean(np.abs(resid))), 2),
        "rmse": round(float(np.sqrt(np.mean(resid ** 2))), 2),
        "bias": round(float(np.mean(resid)), 2),
        "r2": round(1.0 - ss_res / ss_tot, 3),
    }, pred, actual, resid


def _weekly_drift(df, resid):
    """Rolling weekly MAE of drop-off error — the drift/retrain trigger."""
    weeks = pd.to_datetime(df["created_at"]).dt.to_period("W").astype(str)
    frame = pd.DataFrame({"week": weeks.to_numpy(), "abs_err": np.abs(resid)})
    grouped = frame.groupby("week")["abs_err"].mean().reset_index()
    grouped = grouped.sort_values("week")
    labels = grouped["week"].tolist()
    values = [round(float(v), 2) for v in grouped["abs_err"].tolist()]
    baseline = float(np.mean(values[:-4])) if len(values) > 5 else float(np.mean(values))
    recent = float(np.mean(values[-4:])) if len(values) >= 4 else float(np.mean(values))
    drift_flag = recent > baseline * 1.25 if baseline else False
    return {
        "weeks": labels,
        "mae": values,
        "baseline_mae": round(baseline, 2),
        "recent_mae": round(recent, 2),
        "drift_flag": bool(drift_flag),
        "delta_pct": round((recent - baseline) / baseline * 100, 1) if baseline else 0.0,
    }


def _residual_histogram(resid, bins=15):
    counts, edges = np.histogram(resid, bins=bins)
    centers = [round(float((edges[i] + edges[i + 1]) / 2), 1) for i in range(len(edges) - 1)]
    return {"centers": centers, "counts": [int(c) for c in counts]}


def _late_calibration(df, n_bins=10):
    prob, _ = _model_predict("late", df)
    actual = df["late"].to_numpy(dtype=float)
    brier = float(np.mean((prob - actual) ** 2))
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    bins = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (prob >= lo) & (prob < hi if i < n_bins - 1 else prob <= hi)
        if not mask.any():
            continue
        bins.append({
            "predicted": round(float(prob[mask].mean()), 3),
            "observed": round(float(actual[mask].mean()), 3),
            "n": int(mask.sum()),
        })
    return {"brier": round(brier, 4), "bins": bins, "base_rate": round(float(actual.mean()), 3)}


def _live_stats():
    """Aggregate the live predicted-vs-actual log (going-forward learning)."""
    out = {"logged": 0, "resolved": 0, "dropoff_mae": None, "late_brier": None}
    try:
        out["logged"] = PredictionLog.query.count()
        resolved = PredictionLog.query.filter(PredictionLog.resolved_at.isnot(None)).all()
        out["resolved"] = len(resolved)
        drop_err = [r.error for r in resolved if r.kind == "dropoff" and r.error is not None]
        if drop_err:
            out["dropoff_mae"] = round(float(np.mean(np.abs(drop_err))), 2)
        late = [(r.predicted, r.actual) for r in resolved if r.kind == "late" and r.actual is not None]
        if late:
            out["late_brier"] = round(float(np.mean([(p - a) ** 2 for p, a in late])), 4)
    except Exception:
        pass
    return out


def feedback_report():
    """Full predicted-vs-actual report used by the feedback dashboard."""
    svc = get_service()
    svc.ensure_trained()
    df = load_history()

    dropoff, d_pred, d_actual, d_resid = _regression_metrics("dropoff", df)
    pickup, *_ = _regression_metrics("pickup", df)
    drift = _weekly_drift(df, d_resid)
    residuals = _residual_histogram(d_resid)
    calibration = _late_calibration(df)

    return {
        "n_rows": int(len(df)),
        "model_version": (registry.load_metrics() or {}).get("trained_at", ""),
        "dropoff": dropoff,
        "pickup": pickup,
        "drift": drift,
        "residuals": residuals,
        "calibration": calibration,
        "live": _live_stats(),
    }


# --------------------------------------------------------------------------- #
#  Live loop: log at dispatch, resolve at completion
# --------------------------------------------------------------------------- #
def log_predictions(shipment):
    """Persist the model's predictions for a shipment at dispatch time.

    Best-effort and idempotent-ish: skips if the shipment already has unresolved
    logs. Safe to call from a request path — never raises.
    """
    try:
        svc = get_service()
        if not svc.is_trained or shipment.id is None:
            return
        if shipment.predictions.filter_by(resolved_at=None).count():
            return
        preds = svc.predict_all(shipment)
        version = (registry.load_metrics() or {}).get("trained_at", "")
        rows = [
            ("dropoff", preds["dropoff"]["minutes"]),
            ("pickup", preds["pickup"]["minutes"]),
            ("late", preds["late"]["probability"]),
        ]
        for kind, value in rows:
            db.session.add(PredictionLog(
                shipment_id=shipment.id, kind=kind,
                predicted=float(value), model_version=version,
            ))
    except Exception:
        db.session.rollback()


def _dispatch_time(shipment):
    """When the parcel went out for delivery (start of the drop-off clock)."""
    try:
        events = [e for e in shipment.events
                  if e.status == ShipmentStatus.OUT_FOR_DELIVERY and e.created_at]
        if events:
            return max(events, key=lambda e: e.created_at).created_at
    except Exception:
        pass
    return shipment.picked_up_at


def _to_naive(dt):
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def resolve_predictions(shipment):
    """Fill in actual outcomes for a completed shipment, closing the loop."""
    try:
        start = _to_naive(_dispatch_time(shipment))
        delivered = _to_naive(shipment.delivered_at)
        if not (start and delivered):
            return
        actual_min = (delivered - start).total_seconds() / 60.0
        if actual_min <= 0:
            return

        promised = None
        try:
            from .service import get_service as _gs
            raw = _gs()._shipment_raw(shipment)
            promised = raw.get("promised_minutes")
        except Exception:
            promised = None

        pending = shipment.predictions.filter_by(resolved_at=None).all()
        for log in pending:
            if log.kind == "dropoff":
                log.actual = round(actual_min, 2)
                log.error = round(actual_min - log.predicted, 2)
            elif log.kind == "late":
                actual_late = 1.0 if (promised and actual_min > promised) else 0.0
                log.actual = actual_late
                log.error = round(abs(log.predicted - actual_late), 3)
            else:
                continue
            log.resolved_at = utcnow()
    except Exception:
        db.session.rollback()
