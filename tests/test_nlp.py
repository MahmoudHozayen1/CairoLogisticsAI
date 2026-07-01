"""Tests for Slice 3: NLP handling-notes model + admin operations assistant.

The NLP analyzer is tested directly (training the TF-IDF + logistic multi-label
model is fast and deterministic, seed=42). The assistant is tested in offline
retrieval mode — ``ASSISTANT_USE_LLM`` forced off so no network/Ollama call is
made — verifying intent routing and that every answer cites its data sources.
"""
from datetime import timedelta

import pytest

from app import create_app
from app.extensions import db
from app.models import (
    User, Hub, Shipment, ShipmentStatus, Role, utcnow,
)
from app.ml import nlp
from app.ml.assistant import Assistant


# --------------------------------------------------------------------------- #
#  NLP note analyzer
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def trained_analyzer():
    bundle, metrics = nlp.train_notes()
    return nlp.NoteAnalyzer(bundle), metrics


def test_rule_only_analyzer_detects_fragile():
    # No bundle -> pure lexicon layer still works (deploy-safe fallback).
    analyzer = nlp.NoteAnalyzer(None)
    res = analyzer.analyze("Fragile, handle with care please")
    keys = {t["key"] for t in res["tags"]}
    assert "fragile" in keys
    fragile = next(t for t in res["tags"] if t["key"] == "fragile")
    assert fragile["evidence"], "rule match should attach evidence"
    assert fragile["severity"] == "high"


def test_empty_note_returns_no_tags():
    analyzer = nlp.NoteAnalyzer(None)
    res = analyzer.analyze("   ")
    assert res["tags"] == []
    assert res["priority_score"] == 0


@pytest.mark.parametrize("text,start,end", [
    ("deliver between 6 and 9 pm", 18, 21),
    ("please come after 5 pm", 17, None),
    ("before noon if possible", None, 12),
    ("evening delivery only", 17, 21),
])
def test_time_window_extraction(text, start, end):
    win = nlp.extract_time_window(text)
    assert win is not None
    assert win["start_hour"] == start
    assert win["end_hour"] == end


def test_floor_extraction():
    assert nlp.extract_floor("flat is on the 4th floor, no lift") == 4
    assert nlp.extract_floor("ground floor entrance") is None


def test_do_not_stack_and_doorman_rules():
    analyzer = nlp.NoteAnalyzer(None)
    res = analyzer.analyze("Leave with the doorman and don't stack anything on top")
    keys = {t["key"] for t in res["tags"]}
    assert "doorman" in keys
    assert "do_not_stack" in keys


def test_trained_model_metrics_are_reasonable(trained_analyzer):
    _, metrics = trained_analyzer
    assert metrics["micro_f1"] >= 0.7
    assert metrics["n_tags"] == len(nlp.TAG_KEYS)


def test_trained_model_generalises_to_unseen_phrasing(trained_analyzer):
    analyzer, _ = trained_analyzer
    # Phrasing that no regex covers verbatim -> the learned layer must catch it.
    res = analyzer.analyze("the vase inside can shatter so be extremely careful")
    keys = {t["key"] for t in res["tags"]}
    assert "fragile" in keys
    fragile = next(t for t in res["tags"] if t["key"] == "fragile")
    assert "model" in fragile["source"]
    assert fragile["evidence"], "model tag should surface contributing tokens"


def test_compound_note_multi_label_with_priority(trained_analyzer):
    analyzer, _ = trained_analyzer
    res = analyzer.analyze(
        "Fragile glassware, deliver between 6 and 9 pm, leave with the doorman")
    keys = {t["key"] for t in res["tags"]}
    assert {"fragile", "time_window", "doorman"} <= keys
    assert res["time_window"]["start_hour"] == 18
    assert res["priority_score"] > 0
    # High-severity tags should sort before low-severity ones.
    assert res["tags"][0]["severity"] == "high"


# --------------------------------------------------------------------------- #
#  Admin assistant (offline retrieval)
# --------------------------------------------------------------------------- #
OFFLINE = {"ASSISTANT_USE_LLM": False}


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        _seed()
        yield app
        db.session.remove()
        db.drop_all()


def _seed():
    hub = Hub(name="Central Hub", lat=29.9602, lon=31.2569)
    merchant = User(name="Merchant", email="m@test.io", role=Role.MERCHANT,
                    business_name="Shop")
    merchant.set_password("merchant123")
    db.session.add_all([hub, merchant])
    db.session.flush()
    courier = User(name="Sam Courier", email="c@test.io", role=Role.COURIER,
                   hub_id=hub.id, vehicle_type="Motorcycle")
    courier.set_password("courier123")
    db.session.add(courier)
    db.session.flush()

    s = Shipment(
        tracking_number="SR-ABCD1234", merchant_id=merchant.id, hub_id=hub.id,
        sender_name="Shop", receiver_name="Nora", receiver_phone="0100000000",
        district="Maadi", lat=29.9605, lon=31.2575, weight_kg=2.0,
        status=ShipmentStatus.PENDING,
    )
    s.add_event(ShipmentStatus.PENDING, note="created")
    db.session.add(s)
    db.session.commit()


def test_overview_intent_cites_sources(app):
    result = Assistant().answer("give me an operations overview", OFFLINE)
    assert result["intent"] == "overview"
    assert result["used_llm"] is False
    assert result["sources"], "answer must list the data it used"
    assert "Total shipments" in " ".join(result["sources"])


def test_courier_intent(app):
    result = Assistant().answer("how are the couriers performing?", OFFLINE)
    assert result["intent"] == "courier"
    assert any("Sam Courier" in s for s in result["sources"])


def test_tracking_intent_lookup(app):
    result = Assistant().answer("track SR-ABCD1234", OFFLINE)
    assert result["intent"] == "tracking"
    assert "SR-ABCD1234" in result["answer"]
    assert any("Nora" in s for s in result["sources"])


def test_tracking_unknown_code(app):
    result = Assistant().answer("where is SR-NOPE9999", OFFLINE)
    assert result["intent"] == "tracking"
    assert "couldn't find" in result["answer"].lower()


def test_help_intent(app):
    result = Assistant().answer("hello, what can you do?", OFFLINE)
    assert result["intent"] == "help"
    assert result["sources"]


def test_empty_question(app):
    result = Assistant().answer("", OFFLINE)
    assert result["intent"] == "empty"
    assert result["used_llm"] is False


def test_assistant_ask_endpoint_offline(app, monkeypatch):
    # Force the config flag off so the endpoint never attempts an LLM call.
    app.config["ASSISTANT_USE_LLM"] = False
    admin = User(name="Boss", email="a@test.io", role=Role.ADMIN)
    admin.set_password("admin12345")
    db.session.add(admin)
    db.session.commit()

    client = app.test_client()
    client.post("/auth/login", data={
        "email": "a@test.io", "password": "admin12345"},
        follow_redirects=True)
    resp = client.post("/admin/assistant/ask", json={
        "question": "how many shipments are pending?"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "answer" in data
    assert data["used_llm"] is False
    assert data["sources"]
