"""Tests for the predictive ML layer (app/ml).

Component tests (dataset determinism, exact explainability, forecasting) run
fast and offline. The training-dependent tests isolate their artifacts to a
temporary instance directory (via monkeypatching ``paths.instance_dir``) so they
never touch the developer's real models, and train on a small history slice.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np

from app.ml import dataset, features, registry
from app.ml.explain import TreeContributionExplainer
from app.ml.forecast import SeasonalTrendForecaster, backtest_mape
from app.ml.service import ModelService
from app.ml.train import train_all


def test_history_is_deterministic():
    a = dataset.generate_history(n_days=30, seed=42)
    b = dataset.generate_history(n_days=30, seed=42)
    assert a.equals(b)
    assert len(a) > 100
    for col in ("delivery_minutes", "pickup_wait_minutes", "late", "cost_egp"):
        assert col in a.columns
    assert a["late"].isin([0, 1]).all()


def test_traffic_and_sla_helpers():
    # Evening rush is busier than pre-dawn; Friday is lighter than Monday.
    assert features.traffic_factor(18, 0) > features.traffic_factor(3, 0)
    assert features.traffic_factor(18, 4) < features.traffic_factor(18, 0)
    assert features.sla_promised_minutes(5.0, 20.0) > features.SLA_FIXED_BUFFER


def test_forecaster_decomposition_and_backtest():
    df = dataset.generate_history(n_days=90, seed=1)
    agg = dataset.daily_aggregates(df)
    f = SeasonalTrendForecaster().fit(
        agg["orders"].to_numpy(float), agg["dow"].to_numpy(int))
    fc = f.forecast(14, start_dow=int(agg["dow"].iloc[-1]))
    assert len(fc["point"]) == 14
    assert all(l <= p <= u for l, p, u in zip(fc["lower"], fc["point"], fc["upper"]))
    assert len(f.reasoning()) == 4
    mape = backtest_mape(agg["orders"].to_numpy(float), agg["dow"].to_numpy(int))
    assert 0 <= mape < 60


def test_explainer_is_exactly_additive(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ml.paths.instance_dir", lambda: tmp_path)
    train_all(regenerate=True, n_days=45)
    bundle = registry.load_model("dropoff")
    ex = TreeContributionExplainer(bundle["model"], bundle["feature_names"])
    X = features.build_features(dataset.load_history(), "dropoff").iloc[:60]
    bias, contribs, raw = ex.contributions(X)
    # prediction must equal bias + sum of feature contributions.
    assert np.allclose(bias + contribs.sum(axis=1), raw, atol=1e-6)
    one = ex.explain(X.iloc[[0]])
    assert one["reasons"] and "contribution" in one["reasons"][0]


def test_service_predicts_with_reasoning(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ml.paths.instance_dir", lambda: tmp_path)
    train_all(regenerate=True, n_days=45)

    svc = ModelService()
    ship = SimpleNamespace(
        picked_up_at=None,
        created_at=datetime(2025, 6, 2, 18, 0, tzinfo=timezone.utc),
        hub=SimpleNamespace(lat=30.0566, lon=31.3300),
        courier=None, courier_id=None,
        lat=30.070, lon=31.350,
        weight_kg=4.0, cod_amount=250.0, route_sequence=3,
    )
    out = svc.predict_all(ship)
    assert out["dropoff"]["minutes"] > 0
    assert out["pickup"]["minutes"] > 0
    assert 0.0 <= out["late"]["probability"] <= 1.0
    assert out["late"]["band"] in ("low", "medium", "high")
    assert len(out["dropoff"]["reasons"]) >= 1

    fc = svc.forecast(horizon=10)
    assert len(fc["future_dates"]) == 10
    assert "monthly_growth_pct" in fc["orders_growth"]

    cards = svc.model_cards()
    assert "dropoff" in cards["models"]
