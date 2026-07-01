"""High-level predictive service consumed by the web layer.

Loads the trained artifacts (training them once on first use if missing), turns a
live :class:`~app.models.Shipment` into the exact feature vector each model was
trained on, and returns predictions **with reasoning** plus the demand/cost
forecast and model scorecards.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import registry
from .dataset import load_history
from .explain import TreeContributionExplainer
from .features import build_features, haversine_km, sla_promised_minutes, traffic_factor, vehicle_speed_kmh, store_congestion
from .nlp import NoteAnalyzer
from .train import train_all

_service = None


def get_service() -> "ModelService":
    global _service
    if _service is None:
        _service = ModelService()
    return _service


class ModelService:
    def __init__(self):
        self._bundles: dict = {}
        self._explainers: dict = {}
        self._note_analyzer = None
        self._metrics = None
        self._loaded = False

    # -- lifecycle -------------------------------------------------------- #
    def ensure_trained(self, force: bool = False):
        if force or not registry.artifacts_exist():
            train_all(regenerate=force)
            self._loaded = False
        if not self._loaded:
            for name in ("dropoff", "pickup", "late"):
                bundle = registry.load_model(name)
                self._bundles[name] = bundle
                self._explainers[name] = TreeContributionExplainer(
                    bundle["model"], bundle["feature_names"])
            self._bundles["forecast"] = registry.load_model("forecast")
            self._note_analyzer = NoteAnalyzer(registry.load_model("notes"))
            self._metrics = registry.load_metrics()
            self._loaded = True

    @property
    def is_trained(self) -> bool:
        return registry.artifacts_exist()

    # -- feature assembly ------------------------------------------------- #
    def _shipment_raw(self, shipment, when=None) -> dict:
        when = when or shipment.picked_up_at or shipment.created_at or datetime.now(timezone.utc)
        hour, dow = when.hour, when.weekday()

        hub = shipment.hub or (shipment.courier.hub if shipment.courier else None)
        drop_dist = None
        if hub and shipment.lat is not None and shipment.lon is not None:
            drop_dist = haversine_km(hub.lat, hub.lon, shipment.lat, shipment.lon)

        vehicle = shipment.courier.vehicle_type if shipment.courier else None
        vspeed = vehicle_speed_kmh(vehicle)

        parcels = self._parcels_on_route(shipment)
        promised = None
        if drop_dist is not None:
            promised = sla_promised_minutes(drop_dist, vspeed)

        return {
            "dropoff_distance_km": drop_dist,
            "pickup_distance_km": None,  # store not geocoded live -> median fill
            "traffic_factor": traffic_factor(hour, dow),
            "store_congestion": store_congestion(hour),
            "hour": hour,
            "dow": dow,
            "weight_kg": shipment.weight_kg,
            "cod_amount": shipment.cod_amount or 0.0,
            "vehicle_type": vehicle,
            "stop_sequence": shipment.route_sequence or 1,
            "parcels_on_route": parcels,
            "promised_minutes": promised,
            "_when": when,
        }

    @staticmethod
    def _parcels_on_route(shipment) -> int:
        if not shipment.courier_id:
            return 1
        try:
            from ..models import Shipment, ShipmentStatus
            n = Shipment.query.filter_by(
                courier_id=shipment.courier_id,
                status=ShipmentStatus.OUT_FOR_DELIVERY,
            ).count()
            return max(1, n)
        except Exception:
            return 1

    def _features(self, name, raw):
        bundle = self._bundles[name]
        X = build_features(raw, bundle["feature_set"])
        for col in bundle["feature_names"]:
            X[col] = X[col].fillna(bundle["medians"].get(col))
        return X

    # -- predictions ------------------------------------------------------ #
    def predict_dropoff(self, shipment, when=None) -> dict:
        self.ensure_trained()
        raw = self._shipment_raw(shipment, when)
        X = self._features("dropoff", raw)
        exp = self._explainers["dropoff"].explain(X)
        minutes = max(1.0, float(exp["prediction"]))
        base = raw["_when"]
        return {
            "minutes": round(minutes, 1),
            "eta": (base + timedelta(minutes=minutes)).isoformat(),
            "bias": exp["bias"],
            "reasons": exp["reasons"],
        }

    def predict_pickup(self, shipment, when=None) -> dict:
        self.ensure_trained()
        raw = self._shipment_raw(shipment, when)
        X = self._features("pickup", raw)
        exp = self._explainers["pickup"].explain(X)
        minutes = max(1.0, float(exp["prediction"]))
        return {
            "minutes": round(minutes, 1),
            "bias": exp["bias"],
            "reasons": exp["reasons"],
        }

    def predict_late(self, shipment, when=None) -> dict:
        self.ensure_trained()
        raw = self._shipment_raw(shipment, when)
        X = self._features("late", raw)
        exp = self._explainers["late"].explain(X)
        p = float(exp["probability"])
        band = "high" if p >= 0.6 else ("medium" if p >= 0.3 else "low")
        return {
            "probability": round(p, 3),
            "percent": round(p * 100, 1),
            "band": band,
            "bias": exp["bias"],
            "reasons": exp["reasons"],
        }

    def predict_all(self, shipment, when=None) -> dict:
        return {
            "dropoff": self.predict_dropoff(shipment, when),
            "pickup": self.predict_pickup(shipment, when),
            "late": self.predict_late(shipment, when),
        }

    # -- note understanding (NLP) ---------------------------------------- #
    def analyze_note(self, text) -> dict:
        """Extract explained handling tags from a free-text delivery note."""
        self.ensure_trained()
        return self._note_analyzer.analyze(text)

    def analyze_shipment_notes(self, shipment) -> dict:
        """Analyse the note stored on a shipment (``delivery_notes``)."""
        return self.analyze_note(getattr(shipment, "delivery_notes", None))

    # -- forecasting ------------------------------------------------------ #
    def forecast(self, horizon: int = 14) -> dict:
        self.ensure_trained()
        fb = self._bundles["forecast"]
        orders_f, cost_f = fb["orders"], fb["cost"]
        last_date = datetime.fromisoformat(fb["last_date"])
        start_dow = (last_date + timedelta(days=1)).weekday()
        future = [(last_date + timedelta(days=i + 1)).date().isoformat()
                  for i in range(horizon)]
        return {
            "history": fb["history"],
            "future_dates": future,
            "orders": orders_f.forecast(horizon, start_dow),
            "cost": cost_f.forecast(horizon, start_dow),
            "orders_growth": orders_f.growth_summary(),
            "cost_growth": cost_f.growth_summary(),
            "orders_reasoning": orders_f.reasoning(),
            "cost_reasoning": cost_f.reasoning(),
        }

    # -- scorecards ------------------------------------------------------- #
    def model_cards(self) -> dict:
        self.ensure_trained()
        return self._metrics or {}
