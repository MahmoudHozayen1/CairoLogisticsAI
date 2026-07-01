"""Per-prediction reasoning via exact tree contributions.

Gradient-boosted trees are accurate but opaque. This module recovers an *exact*
additive decomposition of a scikit-learn ``GradientBoosting`` prediction:

    raw_prediction(x) = bias + Σ_f contribution_f(x)

by walking each regression tree's decision path and attributing every change in
node value to the feature that produced the split (the classic "treeinterpreter"
decomposition, scaled by the learning rate and summed across boosting stages).

For regressors the decomposition is in the target's own units (minutes); for the
binary classifier it is in log-odds, and we also expose the resulting
probability. No SHAP / heavy dependency required, and the reconstruction matches
``model.predict`` / ``model.decision_function`` to floating-point precision.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import expit
from sklearn.base import is_classifier

from .features import label_for


class TreeContributionExplainer:
    """Exact additive feature attributions for a fitted GradientBoosting model."""

    def __init__(self, model, feature_names):
        self.model = model
        self.feature_names = list(feature_names)
        self.is_classifier = is_classifier(model)
        self.learning_rate = float(getattr(model, "learning_rate", 1.0))

    # -- core ------------------------------------------------------------- #
    def _raw(self, X: np.ndarray) -> np.ndarray:
        if self.is_classifier:
            return self.model.decision_function(X)
        return self.model.predict(X)

    def _path_contributions(self, X: np.ndarray) -> np.ndarray:
        """Sum of decision-path value deltas per feature (excludes tree roots)."""
        n, n_feat = X.shape
        contrib = np.zeros((n, n_feat), dtype=float)
        # scikit-learn casts inputs to float32 before comparing against split
        # thresholds; mirror that exactly so we always reach the same leaf as
        # ``predict`` (otherwise boundary samples branch differently).
        Xf = X.astype(np.float32)

        # estimators_ is (n_stages, K); binary/regression use K == 1.
        for stage in self.model.estimators_:
            tree = stage[0].tree_
            feat = tree.feature
            thr = tree.threshold
            left = tree.children_left
            right = tree.children_right
            val = tree.value[:, 0, 0]
            for i in range(n):
                node = 0
                while left[node] != -1:  # internal node
                    f = feat[node]
                    nxt = left[node] if Xf[i, f] <= thr[node] else right[node]
                    contrib[i, f] += val[nxt] - val[node]
                    node = nxt
        return contrib * self.learning_rate

    def contributions(self, X):
        """Return ``(bias, contribs, raw)`` for a feature matrix ``X``.

        ``bias`` is the (constant) model intercept, ``contribs`` is an
        ``(n_samples, n_features)`` array and ``raw`` the raw predictions such
        that ``raw ≈ bias + contribs.sum(axis=1)``.
        """
        Xa = np.asarray(X, dtype=float)
        if Xa.ndim == 1:
            Xa = Xa.reshape(1, -1)
        contribs = self._path_contributions(Xa)
        # Pass a named frame so scikit-learn doesn't warn about missing names.
        raw = self._raw(pd.DataFrame(Xa, columns=self.feature_names))
        # bias is constant across samples; recover it exactly per-sample.
        bias = float(np.mean(raw - contribs.sum(axis=1)))
        return bias, contribs, raw

    # -- presentation ----------------------------------------------------- #
    def explain(self, row, top_k: int = 5) -> dict:
        """Explain a single sample (dict or 1-row DataFrame).

        Returns a dict with the prediction, bias, probability (classifier only)
        and a ranked list of human-readable ``reasons``.
        """
        if isinstance(row, pd.DataFrame):
            X = row[self.feature_names].to_numpy(dtype=float)
            values = row[self.feature_names].iloc[0].to_dict()
        elif isinstance(row, dict):
            X = np.array([[float(row[f]) for f in self.feature_names]])
            values = {f: row[f] for f in self.feature_names}
        else:
            X = np.asarray(row, dtype=float).reshape(1, -1)
            values = {f: X[0, j] for j, f in enumerate(self.feature_names)}

        bias, contribs, raw = self.contributions(X)
        c = contribs[0]
        raw0 = float(raw[0])

        order = np.argsort(-np.abs(c))
        reasons = []
        for j in order[:top_k]:
            f = self.feature_names[j]
            reasons.append({
                "feature": f,
                "label": label_for(f),
                "value": _round(values.get(f)),
                "contribution": round(float(c[j]), 3),
                "direction": "increases" if c[j] > 0 else "decreases",
            })

        out = {
            "bias": round(bias, 3),
            "raw": round(raw0, 4),
            "reasons": reasons,
            "feature_contributions": {
                self.feature_names[j]: round(float(c[j]), 4) for j in range(len(c))
            },
        }
        if self.is_classifier:
            out["probability"] = float(expit(raw0))
        else:
            out["prediction"] = raw0
        return out


def _round(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return v
