"""Transparent demand, cost & growth forecasting.

A deliberately white-box forecaster: daily volume (or cost) is decomposed into a
linear **trend** plus additive **weekly seasonality**, with a prediction band
from the residual spread. Because every component is explicit, the forecast is
fully explainable ("baseline X/day, growing +Y/day, Fridays run Z below trend"),
which is exactly what the project needs — no opaque black box for the headline
business numbers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class SeasonalTrendForecaster:
    """Linear trend + weekly seasonal offsets with a 95% prediction band."""

    def __init__(self):
        self.slope = 0.0
        self.intercept = 0.0
        self.seasonal = np.zeros(7)
        self.resid_std = 0.0
        self.n = 0
        self.last_index = -1

    def fit(self, y: np.ndarray, dow: np.ndarray) -> "SeasonalTrendForecaster":
        y = np.asarray(y, dtype=float)
        dow = np.asarray(dow, dtype=int)
        x = np.arange(len(y))
        self.n = len(y)
        self.last_index = int(x[-1]) if self.n else -1

        # Trend via least squares.
        self.slope, self.intercept = np.polyfit(x, y, 1)
        detrended = y - (self.intercept + self.slope * x)

        # Weekly seasonal offsets (mean-centred so they sum to ~0).
        seasonal = np.zeros(7)
        for d in range(7):
            mask = dow == d
            seasonal[d] = detrended[mask].mean() if mask.any() else 0.0
        self.seasonal = seasonal - seasonal.mean()

        resid = detrended - self.seasonal[dow]
        self.resid_std = float(resid.std(ddof=1)) if self.n > 2 else 0.0
        return self

    def _trend_at(self, idx):
        return self.intercept + self.slope * np.asarray(idx, dtype=float)

    def forecast(self, horizon: int, start_dow: int) -> dict:
        """Forecast ``horizon`` future days following the fitted series."""
        idx = np.arange(self.last_index + 1, self.last_index + 1 + horizon)
        dows = (start_dow + np.arange(horizon)) % 7
        trend = self._trend_at(idx)
        seasonal = self.seasonal[dows]
        yhat = np.clip(trend + seasonal, 0, None)
        band = 1.96 * self.resid_std
        return {
            "point": yhat.tolist(),
            "lower": np.clip(yhat - band, 0, None).tolist(),
            "upper": (yhat + band).tolist(),
            "dow": dows.tolist(),
        }

    def fitted(self, dow: np.ndarray) -> list:
        x = np.arange(self.n)
        return (self._trend_at(x) + self.seasonal[np.asarray(dow, dtype=int)]).tolist()

    def growth_summary(self, window: int = 30) -> dict:
        """Recent vs. previous window average and the implied monthly growth."""
        idx_recent = np.arange(self.last_index - window + 1, self.last_index + 1)
        idx_prev = np.arange(self.last_index - 2 * window + 1, self.last_index - window + 1)
        recent = float(self._trend_at(idx_recent).mean())
        prev = float(self._trend_at(idx_prev).mean())
        pct = ((recent - prev) / prev * 100.0) if prev else 0.0
        return {
            "recent_avg": round(recent, 2),
            "previous_avg": round(prev, 2),
            "monthly_growth_pct": round(pct, 1),
            "daily_slope": round(float(self.slope), 3),
        }

    def reasoning(self) -> list:
        """Plain-language decomposition of the forecast drivers."""
        best = int(np.argmax(self.seasonal))
        worst = int(np.argmin(self.seasonal))
        trend_word = "growing" if self.slope > 0 else ("shrinking" if self.slope < 0 else "flat")
        return [
            {"label": "Baseline level",
             "detail": f"~{self.intercept + self.slope * self.last_index:.1f} per day at the latest date"},
            {"label": "Trend",
             "detail": f"{trend_word} by {self.slope:+.2f} per day"},
            {"label": "Weekly pattern",
             "detail": f"{WEEKDAY_NAMES[best]} runs highest "
                       f"({self.seasonal[best]:+.1f}), {WEEKDAY_NAMES[worst]} lowest "
                       f"({self.seasonal[worst]:+.1f})"},
            {"label": "Uncertainty",
             "detail": f"±{1.96 * self.resid_std:.1f} 95% band from day-to-day noise"},
        ]

    # -- persistence ------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "slope": self.slope, "intercept": self.intercept,
            "seasonal": self.seasonal.tolist(), "resid_std": self.resid_std,
            "n": self.n, "last_index": self.last_index,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SeasonalTrendForecaster":
        f = cls()
        f.slope = d["slope"]
        f.intercept = d["intercept"]
        f.seasonal = np.array(d["seasonal"])
        f.resid_std = d["resid_std"]
        f.n = d["n"]
        f.last_index = d["last_index"]
        return f


def backtest_mape(y: np.ndarray, dow: np.ndarray, holdout: int = 14) -> float:
    """Mean absolute percentage error of a trailing ``holdout``-day backtest."""
    y = np.asarray(y, dtype=float)
    dow = np.asarray(dow, dtype=int)
    if len(y) <= holdout + 7:
        return float("nan")
    f = SeasonalTrendForecaster().fit(y[:-holdout], dow[:-holdout])
    fc = f.forecast(holdout, start_dow=int(dow[-holdout]))
    pred = np.array(fc["point"])
    actual = y[-holdout:]
    mask = actual > 0
    return float(np.mean(np.abs((actual[mask] - pred[mask]) / actual[mask])) * 100.0)
