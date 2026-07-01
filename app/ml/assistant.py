"""Admin assistant (chatbot) with an always-on offline fallback.

The assistant answers operational questions about the platform — order counts,
late-delivery risk, demand forecast, courier performance, a single parcel's
status, audit-chain integrity, special handling notes and model accuracy.

Two-tier design so it **always works, and deploys anywhere**:

1. **Retrieval (offline, deterministic).** Every question is routed to an intent
   and answered by querying the live database / trained models. This layer needs
   no external service and produces the *facts* shown as the answer's sources —
   the reasoning the project requires for anything predictive.
2. **LLM phrasing (optional).** When an Ollama server is reachable
   (``OLLAMA_BASE_URL``) the retrieved facts are handed to the model, which
   phrases a natural-language reply grounded strictly in those facts. If Ollama
   is absent (e.g. free hosting), the deterministic template answer is returned
   instead — same information, no dependency.

The LLM is never trusted to invent numbers: it only rephrases the facts the
retrieval layer already computed, and those facts are always displayed.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

_TRACK_RE = re.compile(r"\bSR-?[A-Z0-9]{4,}\b", re.IGNORECASE)

# Module-level cache of Ollama reachability so a missing server costs one short
# probe per TTL window rather than a timeout on every question.
_llm_state = {"checked_at": 0.0, "ok": False, "base": None}
_LLM_TTL = 60.0


# --------------------------------------------------------------------------- #
# Ollama client (stdlib only — no new dependency)
# --------------------------------------------------------------------------- #
def _ollama_available(base_url: str, timeout: float = 1.5) -> bool:
    now = time.time()
    if _llm_state["base"] == base_url and now - _llm_state["checked_at"] < _LLM_TTL:
        return _llm_state["ok"]
    ok = False
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    _llm_state.update(checked_at=now, ok=ok, base=base_url)
    return ok


def _ollama_generate(base_url: str, model: str, prompt: str, timeout: float) -> str | None:
    payload = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return (body.get("response") or "").strip() or None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Assistant
# --------------------------------------------------------------------------- #
SUGGESTIONS = [
    "How many shipments are out for delivery?",
    "Which deliveries are most at risk of being late?",
    "What's the demand forecast for next week?",
    "How are the couriers performing today?",
    "Track a parcel by pasting its tracking number",
    "Are there any parcels with special handling notes?",
    "Is the handoff audit chain intact?",
    "How accurate are the prediction models?",
]


class Assistant:
    """Route a natural-language question to a DB-backed answer (+ optional LLM)."""

    def answer(self, question: str, config: dict | None = None) -> dict:
        config = config or {}
        q = (question or "").strip()
        if not q:
            return {"answer": "Ask me about shipments, couriers, forecasts or a "
                    "tracking number.", "intent": "empty", "sources": [],
                    "used_llm": False, "suggestions": SUGGESTIONS[:4]}

        intent, retrieval = self._route(q)
        facts = retrieval.get("facts", [])
        base_answer = retrieval.get("answer", "")

        used_llm = False
        answer = base_answer
        if config.get("ASSISTANT_USE_LLM") and facts:
            base_url = config.get("OLLAMA_BASE_URL", "http://localhost:11434")
            if _ollama_available(base_url):
                prompt = self._build_prompt(q, facts)
                text = _ollama_generate(
                    base_url, config.get("OLLAMA_MODEL", "llama3.2"),
                    prompt, float(config.get("OLLAMA_TIMEOUT", 20)))
                if text:
                    answer, used_llm = text, True

        return {
            "answer": answer,
            "intent": intent,
            "sources": facts,
            "used_llm": used_llm,
            "data": retrieval.get("data"),
            "suggestions": SUGGESTIONS[:4],
        }

    # -- LLM prompt ------------------------------------------------------- #
    @staticmethod
    def _build_prompt(question: str, facts: list[str]) -> str:
        joined = "\n".join(f"- {f}" for f in facts)
        return (
            "You are SwiftRoute's logistics operations assistant. Answer the "
            "admin's question using ONLY the facts below. Do not invent numbers "
            "or names. Be concise (2-4 sentences) and professional.\n\n"
            f"FACTS:\n{joined}\n\nQUESTION: {question}\n\nANSWER:"
        )

    # -- intent routing --------------------------------------------------- #
    def _route(self, q: str):
        ql = q.lower()
        track = _TRACK_RE.search(q)
        if track and any(w in ql for w in ("track", "where", "status", "sr-")) or (
                track and len(ql.split()) <= 3):
            return "tracking", self._r_tracking(track.group(0))
        if any(w in ql for w in ("late", "risk", "delay", "at risk", "overdue")):
            return "late_risk", self._r_late_risk()
        if any(w in ql for w in ("forecast", "demand", "next week", "predict order",
                                 "growth", "how many orders")):
            return "forecast", self._r_forecast()
        if any(w in ql for w in ("courier", "driver", "rider", "performance", "workload")):
            return "courier", self._r_couriers()
        if any(w in ql for w in ("note", "fragile", "handling", "instruction",
                                 "special", "doorman", "time window")):
            return "notes", self._r_notes()
        if any(w in ql for w in ("audit", "handoff", "chain", "tamper", "integrity",
                                 "custody")):
            return "audit", self._r_audit()
        if any(w in ql for w in ("accuracy", "accurate", "model", "mae", "how good",
                                 "performance of the model", "r2")):
            return "model", self._r_models()
        if track:
            return "tracking", self._r_tracking(track.group(0))
        if any(w in ql for w in ("help", "what can you", "hello", "hi ", "hey")):
            return "help", self._r_help()
        return "overview", self._r_overview()

    # -- retrieval intents ----------------------------------------------- #
    def _r_help(self):
        return {
            "answer": "I can report operations counts, late-delivery risk, the "
            "demand forecast, courier performance, a parcel's status by tracking "
            "number, audit-chain integrity, special handling notes and model "
            "accuracy. Every answer cites the data it used.",
            "facts": ["Assistant capabilities listed."],
        }

    def _r_overview(self):
        from ..models import Shipment, ShipmentStatus, User, Role
        from ..extensions import db
        from sqlalchemy import func
        rows = dict(db.session.query(Shipment.status, func.count(Shipment.id))
                    .group_by(Shipment.status).all())
        total = sum(rows.values())
        active_couriers = User.query.filter_by(role=Role.COURIER, is_active=True).count()
        facts = [f"Total shipments: {total}."]
        for st in ShipmentStatus.ORDER + [ShipmentStatus.RETURNED, ShipmentStatus.FAILED]:
            if rows.get(st):
                facts.append(f"{ShipmentStatus.label(st)}: {rows[st]}.")
        facts.append(f"Active couriers: {active_couriers}.")
        ofd = rows.get(ShipmentStatus.OUT_FOR_DELIVERY, 0)
        pending = rows.get(ShipmentStatus.PENDING, 0)
        answer = (f"There are {total} shipments in the system: {ofd} out for "
                  f"delivery, {pending} pending pickup, and "
                  f"{rows.get(ShipmentStatus.DELIVERED, 0)} delivered. "
                  f"{active_couriers} couriers are active.")
        return {"answer": answer, "facts": facts, "data": rows}

    def _r_late_risk(self):
        from ..models import Shipment, ShipmentStatus
        from .service import get_service
        svc = get_service()
        active = (Shipment.query
                  .filter(Shipment.status == ShipmentStatus.OUT_FOR_DELIVERY)
                  .all())
        scored = []
        for s in active:
            try:
                late = svc.predict_late(s)
                scored.append((s, late["percent"], late["band"], late["reasons"]))
            except Exception:
                continue
        scored.sort(key=lambda t: -t[1])
        top = scored[:5]
        if not top:
            return {"answer": "No shipments are currently out for delivery, so "
                    "there is no active late-risk to report.",
                    "facts": ["0 shipments out for delivery."]}
        facts = []
        for s, pct, band, reasons in top:
            why = reasons[0]["label"] if reasons else "model score"
            facts.append(f"{s.tracking_number} to {s.receiver_name}: {pct:.0f}% "
                         f"late risk ({band}); top driver: {why}.")
        hi = sum(1 for _, _, b, _ in scored if b == "high")
        answer = (f"{len(scored)} parcels are out for delivery; {hi} are high-risk. "
                  f"The most at-risk is {top[0][0].tracking_number} "
                  f"({top[0][1]:.0f}% late probability).")
        return {"answer": answer, "facts": facts,
                "data": [{"tracking": s.tracking_number, "percent": p}
                         for s, p, _, _ in top]}

    def _r_forecast(self):
        from .service import get_service
        svc = get_service()
        fc = svc.forecast(horizon=7)
        orders = fc["orders"]["point"]
        cost = fc["cost"]["point"]
        og = fc["orders_growth"]
        cg = fc["cost_growth"]
        next_orders = round(sum(orders))
        next_cost = round(sum(cost))
        o_pct = og.get("monthly_growth_pct", 0.0)
        c_pct = cg.get("monthly_growth_pct", 0.0)
        o_dir = "up" if o_pct > 0 else ("down" if o_pct < 0 else "flat")
        facts = [
            "Forecast horizon: next 7 days.",
            f"Projected orders (7d total): {next_orders}.",
            f"Projected delivery cost (7d total): EGP {next_cost:,}.",
            f"Order trend: {o_pct:+.1f}% vs previous 30-day baseline.",
            f"Cost trend: {c_pct:+.1f}%.",
        ]
        answer = (f"Over the next 7 days I project about {next_orders} orders "
                  f"(~EGP {next_cost:,} in delivery cost). Demand is trending "
                  f"{o_dir} ({o_pct:+.1f}% vs the prior month).")
        return {"answer": answer, "facts": facts,
                "data": {"orders": orders, "cost": cost}}

    def _r_couriers(self):
        from ..models import Shipment, ShipmentStatus, User, Role
        from ..extensions import db
        from sqlalchemy import func
        couriers = User.query.filter_by(role=Role.COURIER).all()
        load = dict(db.session.query(Shipment.courier_id, func.count(Shipment.id))
                    .filter(Shipment.status == ShipmentStatus.OUT_FOR_DELIVERY)
                    .group_by(Shipment.courier_id).all())
        delivered = dict(db.session.query(Shipment.courier_id, func.count(Shipment.id))
                         .filter(Shipment.status == ShipmentStatus.DELIVERED)
                         .group_by(Shipment.courier_id).all())
        facts = []
        rows = []
        for c in couriers:
            active = load.get(c.id, 0)
            done = delivered.get(c.id, 0)
            cap = c.route_capacity
            facts.append(f"{c.display_name} ({c.vehicle_type}): {active}/{cap} on "
                         f"route, {done} delivered all-time.")
            rows.append({"name": c.display_name, "active": active, "cap": cap,
                         "delivered": done})
        if not facts:
            return {"answer": "No couriers are registered yet.",
                    "facts": ["0 couriers."]}
        busiest = max(rows, key=lambda r: r["active"], default=None)
        answer = (f"{len(couriers)} couriers registered. "
                  + (f"{busiest['name']} is busiest with {busiest['active']} "
                     f"parcels on route." if busiest and busiest["active"] else
                     "None are currently mid-route."))
        return {"answer": answer, "facts": facts, "data": rows}

    def _r_tracking(self, code: str):
        from ..models import Shipment, ShipmentStatus
        from ..extensions import db
        from sqlalchemy import func
        code = code.upper().replace(" ", "")
        if not code.startswith("SR-") and code.startswith("SR"):
            code = "SR-" + code[2:]
        s = Shipment.query.filter(
            func.upper(Shipment.tracking_number) == code).first()
        if s is None:
            return {"answer": f"I couldn't find a shipment with tracking number "
                    f"{code}.", "facts": [f"No match for {code}."]}
        facts = [
            f"{s.tracking_number}: status {ShipmentStatus.label(s.status)}.",
            f"Receiver: {s.receiver_name}, {s.district or 'n/a'}.",
            f"Courier: {s.courier.display_name if s.courier else 'unassigned'}.",
        ]
        if s.delivery_confirmation:
            dc = s.delivery_confirmation
            facts.append(f"GIS confirmation: {'verified' if dc.verified else 'outside geofence'} "
                         f"({dc.distance_m:.0f} m from destination).")
        # handling notes
        if s.delivery_notes:
            try:
                from .service import get_service
                note = get_service().analyze_note(s.delivery_notes)
                if note["tags"]:
                    facts.append("Handling: " + note["summary"] + ".")
            except Exception:
                pass
        # live prediction for in-flight parcels
        if s.status == ShipmentStatus.OUT_FOR_DELIVERY:
            try:
                from .service import get_service
                late = get_service().predict_late(s)
                facts.append(f"Late risk: {late['percent']:.0f}% ({late['band']}).")
            except Exception:
                pass
        answer = (f"{s.tracking_number} is {ShipmentStatus.label(s.status)}, going "
                  f"to {s.receiver_name}"
                  + (f" via {s.courier.display_name}" if s.courier else "")
                  + ".")
        return {"answer": answer, "facts": facts,
                "data": {"tracking": s.tracking_number, "status": s.status}}

    def _r_notes(self):
        from ..models import Shipment, ShipmentStatus
        from .service import get_service
        svc = get_service()
        active = (Shipment.query
                  .filter(Shipment.delivery_notes.isnot(None),
                          Shipment.status.in_([ShipmentStatus.PENDING,
                                               ShipmentStatus.AT_WAREHOUSE,
                                               ShipmentStatus.OUT_FOR_DELIVERY]))
                  .all())
        flagged = []
        counts: dict[str, int] = {}
        for s in active:
            res = svc.analyze_note(s.delivery_notes)
            if res["tags"]:
                flagged.append((s, res))
                for t in res["tags"]:
                    counts[t["label"]] = counts.get(t["label"], 0) + 1
        if not flagged:
            return {"answer": "No in-flight parcels currently carry special "
                    "handling notes.", "facts": ["0 flagged parcels."]}
        flagged.sort(key=lambda x: -x[1]["priority_score"])
        facts = [f"{len(flagged)} in-flight parcels have handling notes."]
        for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            facts.append(f"{label}: {n} parcel(s).")
        for s, res in flagged[:5]:
            facts.append(f"{s.tracking_number}: {res['summary']}.")
        answer = (f"{len(flagged)} active parcels need special handling. "
                  f"Most common: "
                  + ", ".join(f"{lbl} ({n})" for lbl, n in
                              sorted(counts.items(), key=lambda kv: -kv[1])[:3])
                  + ".")
        return {"answer": answer, "facts": facts}

    def _r_audit(self):
        from ..models import Shipment
        from ..audit import verify_chain
        recent = Shipment.query.order_by(Shipment.id.desc()).limit(50).all()
        checked = broken = links = 0
        bad = []
        for s in recent:
            res = verify_chain(s)
            if res["count"] == 0:
                continue
            checked += 1
            links += res["count"]
            if not res["ok"]:
                broken += 1
                bad.append(s.tracking_number)
        if checked == 0:
            return {"answer": "No handoff records exist yet to audit.",
                    "facts": ["0 handoff chains."]}
        facts = [f"Audited {checked} shipment chains ({links} handoff links).",
                 f"Intact chains: {checked - broken}.",
                 f"Tampered/broken chains: {broken}."]
        if bad:
            facts.append("Broken: " + ", ".join(bad[:5]) + ".")
        answer = (f"All {checked} handoff chains are cryptographically intact "
                  f"({links} links verified)." if broken == 0 else
                  f"Warning: {broken} of {checked} handoff chains failed "
                  f"verification.")
        return {"answer": answer, "facts": facts}

    def _r_models(self):
        from .service import get_service
        cards = get_service().model_cards()
        m = cards.get("models", {})
        facts = [f"Models trained on {cards.get('n_rows', 'n/a')} records."]
        if "dropoff" in m:
            facts.append(f"Drop-off ETA: MAE {m['dropoff']['mae']} min, "
                         f"R² {m['dropoff']['r2']}.")
        if "pickup" in m:
            facts.append(f"Pickup time: MAE {m['pickup']['mae']} min, "
                         f"R² {m['pickup']['r2']}.")
        if "late" in m:
            facts.append(f"Late-risk: AUC {m['late']['roc_auc']}, "
                         f"base rate {m['late']['base_rate']}.")
        if "forecast" in m:
            facts.append(f"Forecast MAPE: orders {m['forecast']['orders_mape']}%, "
                         f"cost {m['forecast']['cost_mape']}%.")
        if "notes" in m:
            facts.append(f"Notes NLP: micro-F1 {m['notes']['micro_f1']} across "
                         f"{m['notes']['n_tags']} handling tags.")
        answer = ("Model scorecard — "
                  + (f"drop-off MAE {m['dropoff']['mae']} min, "
                     if "dropoff" in m else "")
                  + (f"late-risk AUC {m['late']['roc_auc']}, "
                     if "late" in m else "")
                  + (f"forecast orders MAPE {m['forecast']['orders_mape']}%."
                     if "forecast" in m else "")).rstrip(", ")
        return {"answer": answer, "facts": facts, "data": m}


_assistant = None


def get_assistant() -> Assistant:
    global _assistant
    if _assistant is None:
        _assistant = Assistant()
    return _assistant
