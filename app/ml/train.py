"""Train and evaluate every model in the predictive layer.

Run via ``flask train-ml`` or ``python -m app.ml.train``. Trains the three
gradient-boosted predictors (drop-off time, pickup time, late-risk) and the
seasonal-trend forecaster on the synthetic history, writes joblib bundles plus a
``metrics.json`` scorecard, and returns a summary for the CLI to print.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
from sklearn.metrics import (
    accuracy_score, brier_score_loss, r2_score, roc_auc_score,
    root_mean_squared_error, mean_absolute_error,
)
from sklearn.model_selection import (
    KFold, StratifiedKFold, cross_val_score, train_test_split,
)

from . import dataset, registry
from .features import (
    DROPOFF_FEATURES, LATE_FEATURES, PICKUP_FEATURES, build_features,
)
from .forecast import (
    SeasonalTrendForecaster, backtest_mape, naive_baseline_mape,
    timeseries_cv_mape,
)
from .nlp import train_notes

RANDOM_STATE = 42
_REG_PARAMS = dict(n_estimators=220, max_depth=3, learning_rate=0.08,
                   subsample=0.9, random_state=RANDOM_STATE)
_CLF_PARAMS = dict(n_estimators=240, max_depth=3, learning_rate=0.07,
                   subsample=0.9, random_state=RANDOM_STATE)
_CV_FOLDS = 5


def _train_regressor(df, features, target, feature_set):
    X = build_features(df, feature_set)
    y = df[target].astype(float)
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.2, random_state=RANDOM_STATE)
    model = GradientBoostingRegressor(**_REG_PARAMS).fit(X_tr, y_tr)
    pred = model.predict(X_te)

    # --- k-fold cross-validation (out-of-fold generalisation estimate) ------
    kf = KFold(n_splits=_CV_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    cv_est = GradientBoostingRegressor(**_REG_PARAMS)
    cv_mae = -cross_val_score(cv_est, X, y, cv=kf,
                              scoring="neg_mean_absolute_error")
    cv_r2 = cross_val_score(cv_est, X, y, cv=kf, scoring="r2")

    # --- naive baseline: always predict the training mean -------------------
    baseline_pred = np.full_like(y_te, float(y_tr.mean()))
    baseline_mae = float(mean_absolute_error(y_te, baseline_pred))
    model_mae = float(mean_absolute_error(y_te, pred))
    skill = (1.0 - model_mae / baseline_mae) * 100.0 if baseline_mae else 0.0

    metrics = {
        "mae": round(model_mae, 3),
        "rmse": round(float(root_mean_squared_error(y_te, pred)), 3),
        "r2": round(float(r2_score(y_te, pred)), 4),
        "target_mean": round(float(y.mean()), 3),
        "cv_folds": _CV_FOLDS,
        "cv_mae_mean": round(float(cv_mae.mean()), 3),
        "cv_mae_std": round(float(cv_mae.std()), 3),
        "cv_r2_mean": round(float(cv_r2.mean()), 4),
        "cv_r2_std": round(float(cv_r2.std()), 4),
        "baseline": "mean-predictor",
        "baseline_mae": round(baseline_mae, 3),
        "skill_vs_baseline_pct": round(skill, 1),
        "importances": _importances(model, features),
    }
    bundle = {
        "model": model, "feature_names": features, "feature_set": feature_set,
        "target": target, "kind": "regressor",
        "medians": X.median().to_dict(),
    }
    return bundle, metrics


def _train_late(df):
    features, feature_set, target = LATE_FEATURES, "late", "late"
    X = build_features(df, feature_set)
    y = df[target].astype(int)
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.2, random_state=RANDOM_STATE, stratify=y)
    model = GradientBoostingClassifier(**_CLF_PARAMS).fit(X_tr, y_tr)
    proba = model.predict_proba(X_te)[:, 1]
    pred = (proba >= 0.5).astype(int)

    # --- stratified k-fold cross-validation ---------------------------------
    skf = StratifiedKFold(n_splits=_CV_FOLDS, shuffle=True,
                          random_state=RANDOM_STATE)
    cv_est = GradientBoostingClassifier(**_CLF_PARAMS)
    cv_auc = cross_val_score(cv_est, X, y, cv=skf, scoring="roc_auc")

    # --- naive baseline: always predict the base rate -----------------------
    base_rate = float(y_tr.mean())
    baseline_brier = float(brier_score_loss(
        y_te, np.full(len(y_te), base_rate)))
    model_brier = float(brier_score_loss(y_te, proba))
    skill = (1.0 - model_brier / baseline_brier) * 100.0 if baseline_brier else 0.0

    metrics = {
        "roc_auc": round(float(roc_auc_score(y_te, proba)), 4),
        "accuracy": round(float(accuracy_score(y_te, pred)), 4),
        "brier": round(model_brier, 4),
        "base_rate": round(float(y.mean()), 4),
        "cv_folds": _CV_FOLDS,
        "cv_auc_mean": round(float(cv_auc.mean()), 4),
        "cv_auc_std": round(float(cv_auc.std()), 4),
        "baseline": "base-rate",
        "baseline_auc": 0.5,
        "baseline_brier": round(baseline_brier, 4),
        "skill_vs_baseline_pct": round(skill, 1),
        "importances": _importances(model, features),
    }
    bundle = {
        "model": model, "feature_names": features, "feature_set": feature_set,
        "target": target, "kind": "classifier",
        "medians": X.median().to_dict(),
    }
    return bundle, metrics


def _train_forecast(df):
    agg = dataset.daily_aggregates(df)
    y_orders = agg["orders"].to_numpy(float)
    y_cost = agg["cost_egp"].to_numpy(float)
    dow = agg["dow"].to_numpy(int)

    orders_f = SeasonalTrendForecaster().fit(y_orders, dow)
    cost_f = SeasonalTrendForecaster().fit(y_cost, dow)

    bundle = {
        "orders": orders_f, "cost": cost_f,
        "last_date": agg["date"].iloc[-1].isoformat(),
        "history": {
            "dates": [d.isoformat() for d in agg["date"]],
            "orders": y_orders.tolist(),
            "cost": y_cost.tolist(),
            "dow": dow.tolist(),
        },
    }
    # Rolling-origin (time-series) cross-validation + a seasonal-naive baseline
    # so the forecast is judged the way a time series should be: only ever
    # predicting the future from the past.
    orders_cv = timeseries_cv_mape(y_orders, dow)
    cost_cv = timeseries_cv_mape(y_cost, dow)
    metrics = {
        "orders_mape": round(backtest_mape(y_orders, dow), 2),
        "cost_mape": round(backtest_mape(y_cost, dow), 2),
        "orders_cv_mape_mean": round(orders_cv["mean"], 2),
        "orders_cv_mape_std": round(orders_cv["std"], 2),
        "cost_cv_mape_mean": round(cost_cv["mean"], 2),
        "cost_cv_mape_std": round(cost_cv["std"], 2),
        "cv_folds": orders_cv["folds"],
        "baseline": "seasonal-naive",
        "orders_baseline_mape": round(naive_baseline_mape(y_orders), 2),
        "cost_baseline_mape": round(naive_baseline_mape(y_cost), 2),
        "days": int(len(agg)),
    }
    return bundle, metrics


def _importances(model, features):
    imp = model.feature_importances_
    return {f: round(float(v), 4) for f, v in sorted(
        zip(features, imp), key=lambda kv: -kv[1])}


def train_all(regenerate: bool = False, n_days: int = dataset.DEFAULT_DAYS) -> dict:
    """Train every model, persist artifacts and return the metrics scorecard."""
    df = dataset.load_history(regenerate=regenerate, n_days=n_days)

    dropoff_bundle, dropoff_m = _train_regressor(
        df, DROPOFF_FEATURES, "delivery_minutes", "dropoff")
    pickup_bundle, pickup_m = _train_regressor(
        df, PICKUP_FEATURES, "pickup_wait_minutes", "pickup")
    late_bundle, late_m = _train_late(df)
    forecast_bundle, forecast_m = _train_forecast(df)
    notes_bundle, notes_m = train_notes()

    registry.save_model("dropoff", dropoff_bundle)
    registry.save_model("pickup", pickup_bundle)
    registry.save_model("late", late_bundle)
    registry.save_model("forecast", forecast_bundle)
    registry.save_model("notes", notes_bundle)

    metrics = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_rows": int(len(df)),
        "models": {
            "dropoff": dropoff_m,
            "pickup": pickup_m,
            "late": late_m,
            "forecast": forecast_m,
            "notes": notes_m,
        },
    }
    registry.save_metrics(metrics)
    return metrics


def main():
    m = train_all(regenerate=False)
    print(f"Trained on {m['n_rows']} records.")
    d, p, l, f = (m["models"][k] for k in ("dropoff", "pickup", "late", "forecast"))
    print(f"  Drop-off ETA   : MAE {d['mae']} min (CV {d['cv_mae_mean']}±{d['cv_mae_std']}), "
          f"R2 {d['r2']}, {d['skill_vs_baseline_pct']}% better than mean-predictor")
    print(f"  Pickup time    : MAE {p['mae']} min (CV {p['cv_mae_mean']}±{p['cv_mae_std']}), "
          f"R2 {p['r2']}, {p['skill_vs_baseline_pct']}% better than mean-predictor")
    print(f"  Late-risk      : AUC {l['roc_auc']} (CV {l['cv_auc_mean']}±{l['cv_auc_std']}), "
          f"acc {l['accuracy']}, base {l['base_rate']}")
    print(f"  Forecast (MAPE): orders {f['orders_mape']}% (CV {f['orders_cv_mape_mean']}±"
          f"{f['orders_cv_mape_std']}, naive {f['orders_baseline_mape']}%), cost {f['cost_mape']}%")
    n = m["models"]["notes"]
    print(f"  Notes NLP      : micro-F1 {n['micro_f1']}, {n['n_tags']} tags, {n['n_samples']} samples")


if __name__ == "__main__":
    main()
