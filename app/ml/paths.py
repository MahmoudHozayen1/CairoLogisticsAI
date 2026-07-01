"""Filesystem locations for the predictive ML layer.

Artifacts (trained models, metrics, the synthetic history) live under the Flask
``instance/`` folder so they are never committed and are writable on every host.
The helpers work both inside an application context (using ``instance_path``) and
in stand-alone scripts / tests (falling back to the project-local ``instance/``).
"""
from __future__ import annotations

from pathlib import Path


def instance_dir() -> Path:
    """Return the active ``instance/`` directory, context-aware."""
    try:  # inside a request/app context this resolves to the real instance path
        from flask import current_app

        return Path(current_app.instance_path)
    except Exception:  # pragma: no cover - no app context (scripts, tests)
        return Path(__file__).resolve().parents[2] / "instance"


def ml_dir() -> Path:
    d = instance_dir() / "ml"
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifacts_dir() -> Path:
    d = ml_dir() / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def history_csv() -> Path:
    return ml_dir() / "history.csv"


def metrics_json() -> Path:
    return artifacts_dir() / "metrics.json"


def model_path(name: str) -> Path:
    return artifacts_dir() / f"{name}.joblib"
