"""Persistence for trained ML artifacts (joblib bundles + metrics JSON)."""
from __future__ import annotations

import json

import joblib

from . import paths

MODEL_NAMES = ("dropoff", "pickup", "late", "forecast", "notes")


def save_model(name: str, bundle) -> None:
    joblib.dump(bundle, paths.model_path(name))


def load_model(name: str):
    p = paths.model_path(name)
    return joblib.load(p) if p.exists() else None


def save_metrics(metrics: dict) -> None:
    paths.metrics_json().write_text(json.dumps(metrics, indent=2))


def load_metrics() -> dict | None:
    p = paths.metrics_json()
    return json.loads(p.read_text()) if p.exists() else None


def artifacts_exist() -> bool:
    return (
        all(paths.model_path(n).exists() for n in MODEL_NAMES)
        and paths.metrics_json().exists()
    )
