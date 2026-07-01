# 🤖 SwiftRoute — AI / ML Feature Guide

This document describes the **predictive & intelligence layer** built on top of the core logistics
platform. It was delivered as five slices covering the project's machine-learning asks. Everything
here is **pure `numpy` / `scikit-learn`** — no `torch`, no GPU — so the whole system trains in
seconds and deploys on a free tier. **Every prediction explains its reasoning.**

> Code lives under [`app/ml/`](../app/ml). Admin screens live under `/admin/ai/*`.
> Screenshots referenced below are in [SCREENSHOTS.md](SCREENSHOTS.md).

---

## Design principles

| Principle | How it's honoured |
| --- | --- |
| **Explainability first** | Every model returns human-readable reasoning — exact tree contributions, linear token weights, feature z-scores or per-step attention weights. Nothing is a black box. |
| **Deploy-anywhere** | Only `numpy`, `scikit-learn`, `pandas`, `scipy`, `joblib`. No deep-learning runtime. Untrained models still return valid, safe output. |
| **Separation from core** | New capabilities use **new tables** or **deterministic simulation** — no destructive changes to the existing `Shipment` schema, so `create_all` upgrades old databases cleanly. |
| **Reproducible** | Everything is seeded (`seed=42`). Synthetic datasets, training and metrics are deterministic. |
| **Offline-safe** | The assistant works with or without an LLM; heavy libraries always have fallbacks. |

---

## Slice 1 — ETA prediction & demand forecasting

**Module:** [`app/ml/`](../app/ml) — `features.py`, `dataset.py`, `train.py`, `explain.py`,
`forecast.py`, `service.py`
**Screen:** AI overview → `16-admin-ai-overview.png`, shipment detail → `13-admin-shipment-detail.png`

Three gradient-boosting models plus a forecaster, trained on a seeded synthetic history
(~5.8k rows over 180 days):

- **Drop-off ETA** (regressor) — MAE ≈ 2.7 min, R² ≈ 0.94
- **Pick-up ETA** (regressor) — MAE ≈ 1.3 min, R² ≈ 0.63
- **Late-delivery risk** (classifier) — AUC ≈ 0.82 (base rate 26%)
- **Demand forecast** — a custom `SeasonalTrendForecaster` (linear trend + weekly seasonality +
  95% band), orders MAPE ≈ 14%, cost ≈ 16%

**Reasoning:** `TreeContributionExplainer` computes **exact additive contributions** for each
sklearn gradient-boosting prediction (it walks the trees in `float32` to match sklearn's leaf
selection precisely), so each ETA comes with a signed breakdown of *why*. The forecaster is
hand-written in numpy and is fully transparent.

**Feature engineering** (`features.py`) is a single shared source of truth for both training and
serving: haversine distance, time-of-day & day-of-week traffic factor, store congestion, vehicle
speed and the promised SLA minutes.

Train with `flask train-ml` (or `python -m app.ml.train`); models auto-train on first boot.

---

## Slice 2 — Feedback loop, chain-of-custody audit & GIS

**Modules:** [`app/ml/feedback.py`](../app/ml/feedback.py), [`app/audit.py`](../app/audit.py)
**Screens:** feedback → `17-admin-ai-feedback.png`, audit → `22-admin-audit.png`,
shipment audit → `14-admin-shipment-audit.png`

Three **new tables** (no changes to `Shipment`): `HandoffRecord`, `DeliveryConfirmation`,
`PredictionLog`.

- **Feedback / drift monitor** — `feedback_report()` back-tests every model over the full history
  (honestly labelled, in-sample) and reports MAE/bias/R², weekly **drift** flags, residual
  histograms and late-risk **calibration** bins + Brier score, alongside live prediction-vs-actual
  logs. Predictions are logged at dispatch and resolved at delivery.
- **Chain of custody** — every status change appends a **SHA-256 hash-chained** `HandoffRecord`
  (merchant → hub → courier → customer). `verify_chain()` recomputes the chain and detects any
  tampering. Hashes are computed over naive-UTC timestamps so they re-verify after a SQLite
  round-trip.
- **GIS delivery confirmation** — `confirm_delivery_location()` geofences the delivery point
  (haversine vs. destination, default 200 m radius). Couriers capture GPS on *Delivered*; the
  public tracking page shows a confirmation badge and the API exposes `geo_confirmation`.

---

## Slice 3 — Handling-notes NLP & operations assistant

**Modules:** [`app/ml/nlp.py`](../app/ml/nlp.py), [`app/ml/assistant.py`](../app/ml/assistant.py)
**Screens:** note analysis on shipment detail → `13-admin-shipment-detail.png` /
`26-courier-shipment-detail.png`, assistant → `21-admin-assistant.png`

- **Handling-notes model** — a **hybrid** multi-label classifier over free-text delivery notes.
  A lexicon/regex layer covers a taxonomy (fragile, do-not-stack, time-window, doorman,
  call-ahead, ring-bell, exact-change, no-lift, meet-outside, leave-at-door), and a learned
  `TfidfVectorizer` + `OneVsRestClassifier(LogisticRegression)` (trained on ~1.8k seeded
  paraphrases) generalises to unseen wording (e.g. *"the vase can shatter"* → fragile).
  **Reasoning** is exact: per-tag token contributions (`tfidf × coef`). It also extracts delivery
  **time windows** ("between 6 and 9 pm" → 18:00–21:00) and target floor. Rule-only mode is a safe
  fallback with no model bundle.
- **Operations assistant** — a two-tier chatbot. Tier 1 is a **deterministic DB-retrieval
  responder** that always works: intent routing answers questions about out-for-delivery counts,
  late-risk, the demand forecast, courier workload, a pasted tracking number, handling notes, audit
  integrity and model accuracy. Tier 2 optionally calls a local **Ollama** LLM to phrase the answer
  over the retrieved facts *only* (it never invents numbers), using stdlib `urllib` — no `requests`
  or `torch`. **Every answer lists its exact sources / reasoning.**

---

## Slice 4 — Learning-to-route (neural pointer policy)

**Module:** [`app/ml/neural_router.py`](../app/ml/neural_router.py)
**Screen:** neural router → `18-admin-ai-router.png`

A **pure-numpy pointer-network-style policy** (`RoutePolicy`) that learns to sequence a courier's
stops. It's a linear-softmax attention over unvisited stops using 7 scale-normalised features
(distance here/to depot/to medoid, mean distance to the rest, is-nearest, rank, isolation).

- **Training** — REINFORCE with a **greedy-rollout baseline** (Kool self-critical), using an
  **exact** softmax policy gradient (no autodiff): `∇logπ(a) = φ(a) − Σπ·φ`. Trains on random
  unit-square Euclidean instances and transfers to real geography via scale-normalised features
  and haversine distances at inference. The best checkpoint is kept.
- **Inference gain** — greedy decoding collapses to nearest-neighbour, so the real improvement comes
  from **best-of-N sampled rollouts** (`samples=32`): ~5% shorter than nearest-neighbour and within
  ~1.5–2% of a 2-opt reference.
- **Reasoning** — each pick reports its top-3 feature contributions (`θ·φ`), and the screen shows
  the learned weights and a learning curve. Untrained policies still return a valid tour.

Train with `flask train-router`. The artifact is kept **separate** from the core model registry so
deploy-time training stays light.

---

## Slice 5 — Courier behaviour modelling (persona clustering)

**Module:** [`app/ml/behavior.py`](../app/ml/behavior.py)
**Screens:** fleet → `19-admin-ai-behavior-fleet.png`, detail → `20-admin-ai-behavior-detail.png`

Simulates a GPS shift per courier → detects stops → clusters into **personas** with z-score
reasoning. **No new tables** — traces are simulated deterministically from `courier.id`.

- **Simulation** — four ground-truth archetypes (efficient / steady / idle-prone / wanderer) drive
  `simulate_shift()`, which emits a timestamped GPS trace. Detours use a perpendicular **dog-leg**
  geometry so the driven distance actually reflects the detour ratio.
- **Stop detection** — `detect_states()` classifies each dwell as driving / delivery / idle / break
  by how long the courier is stationary (measured from **arrival**, a subtlety that was essential to
  recover deliveries correctly).
- **Clustering** — `StandardScaler` + `KMeans(4)` over 7 behavioural features
  (deliveries/hour, break ratio, idle ratio, detour ratio, avg speed, avg delivery minutes,
  stopped ratio). Personas are **ranked by productivity** into tiers (Efficient → Steady →
  Idle-prone → At-risk) with a distinctive trait from centroid z-scores. Quality: silhouette ≈ 0.36,
  **ARI ≈ 0.99** vs. the ground-truth archetypes.
- **Explanation** — `analyze()` returns the persona + confidence, a 0–100 productivity score,
  behavioural **flags** (long breaks, frequent idling, detour-heavy, speeding, low throughput) and
  per-feature **z-score reasoning**. The detail screen colour-codes the GPS map by state and shows a
  time-budget donut.

Train with `flask train-behavior`.

---

## Where it lives (map)

```
app/ml/
├── paths.py          → instance/ml artifact + history paths
├── features.py       → shared feature engineering (train + serve)
├── dataset.py        → seeded synthetic history (~5.8k rows / 180d)
├── explain.py        → exact tree-contribution explainer
├── forecast.py       → SeasonalTrendForecaster (numpy, explainable)
├── train.py          → train_all: ETA regressors + late classifier + forecaster + notes
├── registry.py       → model registry (core models)
├── service.py        → ModelService singleton: predict_all / forecast / model_cards
├── feedback.py       → back-test, drift, calibration, prediction logging
├── nlp.py            → NoteAnalyzer (hybrid rule + TF-IDF multi-label)
├── assistant.py      → Assistant (DB-retrieval + optional Ollama)
├── neural_router.py  → RoutePolicy (pure-numpy pointer policy, REINFORCE)
└── behavior.py       → behaviour simulation + KMeans personas
app/audit.py          → chain-of-custody verification + GIS confirmation
```

## CLI

```powershell
flask --app run train-ml         # ETA regressors, late classifier, forecaster, notes
flask --app run train-router     # neural pointer policy
flask --app run train-behavior   # courier persona clustering
```

## Tests

The AI/ML layer is covered by dedicated suites — `test_ml.py`, `test_audit.py`, `test_nlp.py`,
`test_router.py`, `test_behavior.py` — as part of the **74-test** suite. Run everything with:

```powershell
pytest -q
```
