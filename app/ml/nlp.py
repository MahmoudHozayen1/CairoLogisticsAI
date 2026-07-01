"""NLP model for customer / courier delivery notes.

Free-text delivery instructions ("fragile, handle with care", "leave with the
doorman", "deliver between 6 and 9 pm", "don't stack anything on top") carry
operational signal that never reaches the numeric predictors. This module turns
that text into **structured handling tags** the app can act on, and — like every
other predictor in the project — it explains *why* each tag fired.

Design (deploy-light, fully reproducible)
-----------------------------------------
It is a **hybrid** analyzer combining two layers:

1. A high-precision **lexicon / regex** layer. Each tag owns a set of phrase
   patterns; a match yields the exact substring as human-readable evidence. This
   also auto-labels the synthetic training corpus, so no hand-labelling is
   needed.
2. A **learned multi-label classifier** — ``TfidfVectorizer`` (word 1-2 grams)
   feeding a ``OneVsRestClassifier(LogisticRegression)`` — trained on a
   deterministic corpus of paraphrased notes (seed=42). It generalises to
   phrasings the regexes never saw and yields a calibrated confidence. Because
   the estimator is linear, the per-tag reasoning is *exact*: the top tokens are
   ``tfidf * coefficient`` contributions for that tag.

A separate regex **entity extractor** pulls concrete time windows
("between 6 and 9 pm" -> 18:00–21:00) and floor numbers.

The classifier is optional: with no trained bundle the analyzer degrades to the
rule layer alone, so it always returns an answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

RANDOM_STATE = 42
DEFAULT_THRESHOLD = 0.45


# --------------------------------------------------------------------------- #
# Tag taxonomy
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Tag:
    key: str
    label: str
    icon: str            # emoji used in the UI badges
    severity: str        # high | medium | low
    action: str          # short courier-facing instruction
    patterns: tuple      # regex strings (compiled lazily)
    phrases: tuple = field(default=())  # paraphrases used to build the corpus


SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}

TAGS: tuple[Tag, ...] = (
    Tag(
        key="fragile", label="Fragile", icon="🧨", severity="high",
        action="Handle with care — breakable contents.",
        patterns=(
            r"fragile", r"handle\s+with\s+care", r"breakable", r"\bglass\b",
            r"delicate", r"be\s+(?:very\s+)?gentle", r"easily\s+damaged",
        ),
        phrases=(
            "fragile, handle with care", "this is breakable please be careful",
            "contains glass be gentle", "delicate item inside handle with care",
            "careful it's fragile", "easily damaged, don't drop",
        ),
    ),
    Tag(
        key="do_not_stack", label="Do not stack", icon="📦", severity="medium",
        action="Keep on top — do not place other parcels over it.",
        patterns=(
            r"do\s*n['o]?t\s+stack", r"don'?t\s+stack", r"no\s+stacking",
            r"nothing\s+(?:heavy\s+)?on\s+top", r"do\s+not\s+(?:place|put|stack)[^.]*on\s+top",
            r"keep\s+(?:it\s+)?on\s+top", r"keep\s+upright",
        ),
        phrases=(
            "do not place other items on top", "don't stack anything on this",
            "please keep it on top, nothing heavy above",
            "no stacking, keep upright", "do not put boxes on top of it",
            "keep this parcel upright at all times",
        ),
    ),
    Tag(
        key="time_window", label="Time window", icon="⏰", severity="high",
        action="Deliver only inside the requested time window.",
        patterns=(
            r"between\s+\d", r"\bafter\s+\d", r"\bbefore\s+(?:\d|noon|midday)",
            r"\d\s*(?:am|pm)", r"\d\s*[-–to]+\s*\d\s*(?:am|pm)",
            r"\b(?:morning|afternoon|evening|night)\s+(?:only|delivery)?",
            r"\bonly\s+(?:in\s+the\s+)?(?:morning|afternoon|evening)",
        ),
        phrases=(
            "deliver between 6 and 9 pm", "only after 5 pm please",
            "before noon if possible", "between 2 and 4 pm",
            "evening delivery only 7-9 pm", "morning only, before 11 am",
            "please come after 6pm", "deliver in the afternoon 1-3",
        ),
    ),
    Tag(
        key="doorman", label="Leave with doorman", icon="🛎️", severity="low",
        action="Hand the parcel to the doorman / reception.",
        patterns=(
            r"door\s*man", r"concierge", r"reception", r"security\s+(?:desk|guard)?",
            r"front\s+desk", r"baww?ab", r"leave\s+(?:it\s+)?at\s+reception",
            r"give\s+it\s+to\s+(?:the\s+)?(?:guard|security|doorman)",
        ),
        phrases=(
            "leave with the doorman", "give it to the concierge",
            "leave at reception if I don't answer", "hand it to security at the gate",
            "the bawab downstairs will receive it", "drop at the front desk",
        ),
    ),
    Tag(
        key="call_ahead", label="Call ahead", icon="📞", severity="medium",
        action="Phone the customer before arriving.",
        patterns=(
            r"call\s+(?:me\s+)?(?:before|first|ahead|when)",
            r"phone\s+(?:me\s+)?before", r"ring\s+me\s+(?:before|when)",
            r"give\s+me\s+a\s+call", r"call\s+(?:on\s+)?arrival",
        ),
        phrases=(
            "please call before arriving", "call me first",
            "phone before delivery", "ring me when you are close",
            "give me a call 10 minutes before", "call on arrival at the gate",
        ),
    ),
    Tag(
        key="ring_bell", label="Ring / knock", icon="🔔", severity="low",
        action="Ring the bell or knock — intercom may be off.",
        patterns=(
            r"ring\s+the\s+bell", r"\bknock\b", r"buzz(?:er|\s+the)",
            r"intercom", r"ring\s+the\s+door",
        ),
        phrases=(
            "ring the bell twice", "please knock loudly",
            "buzz the intercom", "ring the doorbell and wait",
        ),
    ),
    Tag(
        key="exact_change", label="Exact change (COD)", icon="💵", severity="medium",
        action="Bring change ready — cash-on-delivery.",
        patterns=(
            r"exact\s+change", r"\bhave\s+change", r"cash\s+ready",
            r"bring\s+change", r"no\s+change\s+available",
        ),
        phrases=(
            "cash ready, exact change", "I have the exact amount",
            "please bring change for 500", "have change ready for cod",
        ),
    ),
    Tag(
        key="no_lift", label="No lift / stairs", icon="🪜", severity="medium",
        action="No elevator — plan for stairs to an upper floor.",
        patterns=(
            r"no\s+lift", r"no\s+elevator", r"\bstairs\b", r"walk\s*[- ]?up",
            r"\d+(?:st|nd|rd|th)\s+floor", r"top\s+floor",
        ),
        phrases=(
            "flat is on the 4th floor, no lift", "no elevator take the stairs",
            "walk up to the 3rd floor", "top floor, lift is broken",
            "5th floor no lift please be ready",
        ),
    ),
    Tag(
        key="meet_outside", label="Meet outside", icon="🚪", severity="low",
        action="Customer will meet you at the gate / entrance.",
        patterns=(
            r"meet\s+(?:me\s+)?(?:at|outside|downstairs)", r"at\s+the\s+gate",
            r"building\s+(?:gate|entrance)", r"\bdownstairs\b", r"i'?ll\s+come\s+down",
            r"wait\s+outside",
        ),
        phrases=(
            "meet me at the building gate", "I'll be downstairs waiting",
            "come to the building entrance", "wait outside, I'll come down",
            "meet me at the main gate",
        ),
    ),
    Tag(
        key="leave_at_door", label="Leave at door", icon="🚪", severity="low",
        action="Leave the parcel at the door if no answer.",
        patterns=(
            r"leave\s+(?:it\s+)?(?:at|by)\s+(?:the\s+)?door",
            r"drop\s+(?:it\s+)?at\s+(?:the\s+)?door", r"leave\s+at\s+the\s+doorstep",
        ),
        phrases=(
            "leave at the door", "drop it at my door if I'm out",
            "leave by the door please", "just leave it at the doorstep",
        ),
    ),
)

TAG_BY_KEY = {t.key: t for t in TAGS}
TAG_KEYS = tuple(t.key for t in TAGS)

# Neutral filler sentences (no handling tag) so the classifier learns a
# meaningful "no tag" region and does not fire on ordinary text.
NEUTRAL_PHRASES = (
    "thanks for the fast delivery", "the package is a gift",
    "please deliver as soon as possible", "standard delivery is fine",
    "address is near the main square", "second building on the right",
    "apartment number is 12", "the receiver is my brother",
    "order placed this morning", "regular parcel nothing special",
)

_COMPILED: dict[str, list] = {}


def _compiled(tag: Tag):
    cache = _COMPILED.get(tag.key)
    if cache is None:
        cache = [re.compile(p, re.IGNORECASE) for p in tag.patterns]
        _COMPILED[tag.key] = cache
    return cache


# --------------------------------------------------------------------------- #
# Entity extraction: time windows + floor
# --------------------------------------------------------------------------- #
_NOON_WORDS = {"noon": 12, "midday": 12, "midnight": 0}


def _to_24h(hour: int, meridiem: str | None) -> int:
    hour = int(hour) % 24
    if meridiem:
        m = meridiem.lower()
        if m == "pm" and hour < 12:
            hour += 12
        elif m == "am" and hour == 12:
            hour = 0
    return hour


def extract_time_window(text: str) -> dict | None:
    """Best-effort structured delivery window from free text.

    Returns ``{"raw", "start_hour", "end_hour"}`` (hours in 24h, either may be
    ``None`` for open-ended windows) or ``None`` when no time is mentioned.
    """
    t = text.lower()

    # "between 6 and 9 pm" / "6-9 pm" / "6 to 9pm"
    m = re.search(
        r"(?:between\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|to|and)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        t,
    )
    if m:
        h1, _, mer1, h2, _, mer2 = m.groups()
        # If only the second half names am/pm, apply it to both.
        mer1 = mer1 or mer2
        start = _to_24h(int(h1), mer1)
        end = _to_24h(int(h2), mer2 or mer1)
        return {"raw": m.group(0).strip(), "start_hour": start, "end_hour": end}

    # "after 5 pm"
    m = re.search(r"after\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        h, _, mer = m.groups()
        return {"raw": m.group(0).strip(), "start_hour": _to_24h(int(h), mer or "pm"),
                "end_hour": None}

    # "before noon" / "before 11 am"
    m = re.search(r"before\s+(noon|midday|midnight|\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if m:
        word, _, mer = m.groups()
        end = _NOON_WORDS.get(word)
        if end is None:
            end = _to_24h(int(word), mer or "am")
        return {"raw": m.group(0).strip(), "start_hour": None, "end_hour": end}

    # part-of-day words
    for word, (s, e) in {"morning": (7, 12), "afternoon": (12, 17),
                         "evening": (17, 21), "night": (20, 23)}.items():
        if re.search(rf"\b{word}\b", t):
            return {"raw": word, "start_hour": s, "end_hour": e}
    return None


def extract_floor(text: str) -> int | None:
    m = re.search(r"(\d+)(?:st|nd|rd|th)\s+floor", text.lower())
    return int(m.group(1)) if m else None


def _fmt_window(win: dict) -> str:
    def hh(h):
        return f"{h:02d}:00" if h is not None else "…"
    return f"{hh(win['start_hour'])}–{hh(win['end_hour'])}"


# --------------------------------------------------------------------------- #
# Rule layer
# --------------------------------------------------------------------------- #
def _rule_hits(text: str) -> dict[str, list[str]]:
    """Return ``{tag_key: [matched phrase, ...]}`` for every regex that fires."""
    hits: dict[str, list[str]] = {}
    for tag in TAGS:
        found = []
        for rx in _compiled(tag):
            m = rx.search(text)
            if m:
                found.append(m.group(0).strip())
        if found:
            hits[tag.key] = found
    return hits


# --------------------------------------------------------------------------- #
# Synthetic training corpus
# --------------------------------------------------------------------------- #
def build_corpus(seed: int = RANDOM_STATE) -> tuple[list[str], np.ndarray]:
    """Deterministically synthesise labelled notes for the classifier.

    Notes are composed of 0-3 tag phrases (plus occasional neutral filler),
    joined the way customers write them. Labels are the tags whose phrases went
    in — a genuine multi-label target. Reproducible via ``seed``.
    """
    rng = np.random.default_rng(seed)
    texts: list[str] = []
    labels: list[list[int]] = []
    key_index = {k: i for i, k in enumerate(TAG_KEYS)}

    def emit(phrase_parts, active_keys):
        vec = [0] * len(TAG_KEYS)
        for k in active_keys:
            vec[key_index[k]] = 1
        joiner = rng.choice(["; ", ", ", ". ", " and "])
        texts.append(joiner.join(phrase_parts))
        labels.append(vec)

    # Single-tag examples (each paraphrase, several times with light noise).
    for tag in TAGS:
        for phrase in tag.phrases:
            for _ in range(3):
                emit([phrase], [tag.key])

    # Multi-tag combinations (1-3 tags) drawn at random.
    for _ in range(1400):
        k = int(rng.choice([1, 2, 2, 3]))
        chosen = list(rng.choice(TAG_KEYS, size=k, replace=False))
        parts = [str(rng.choice(TAG_BY_KEY[c].phrases)) for c in chosen]
        if rng.random() < 0.25:
            parts.insert(int(rng.integers(0, len(parts) + 1)),
                         str(rng.choice(NEUTRAL_PHRASES)))
        emit(parts, chosen)

    # Pure-neutral examples (no tag) so the model can abstain.
    for _ in range(220):
        k = int(rng.choice([1, 2]))
        parts = [str(rng.choice(NEUTRAL_PHRASES)) for _ in range(k)]
        emit(parts, [])

    return texts, np.asarray(labels)


def train_notes(seed: int = RANDOM_STATE) -> tuple[dict, dict]:
    """Fit the TF-IDF + multi-label logistic model; return (bundle, metrics)."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import f1_score, precision_score, recall_score
    from sklearn.model_selection import train_test_split
    from sklearn.multiclass import OneVsRestClassifier

    texts, Y = build_corpus(seed)
    X_tr, X_te, y_tr, y_te = train_test_split(
        texts, Y, test_size=0.2, random_state=seed)

    vectorizer = TfidfVectorizer(
        lowercase=True, ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    Xtr = vectorizer.fit_transform(X_tr)
    Xte = vectorizer.transform(X_te)

    clf = OneVsRestClassifier(
        LogisticRegression(max_iter=1000, C=6.0, class_weight="balanced"))
    clf.fit(Xtr, y_tr)

    proba = np.column_stack([est.predict_proba(Xte)[:, 1] for est in clf.estimators_])
    pred = (proba >= DEFAULT_THRESHOLD).astype(int)
    metrics = {
        "micro_f1": round(float(f1_score(y_te, pred, average="micro", zero_division=0)), 4),
        "macro_f1": round(float(f1_score(y_te, pred, average="macro", zero_division=0)), 4),
        "precision": round(float(precision_score(y_te, pred, average="micro", zero_division=0)), 4),
        "recall": round(float(recall_score(y_te, pred, average="micro", zero_division=0)), 4),
        "n_samples": int(len(texts)),
        "n_tags": len(TAG_KEYS),
        "per_tag_f1": {
            TAG_KEYS[i]: round(float(f1_score(y_te[:, i], pred[:, i], zero_division=0)), 3)
            for i in range(len(TAG_KEYS))
        },
    }
    bundle = {
        "vectorizer": vectorizer,
        "classifier": clf,
        "tag_keys": list(TAG_KEYS),
        "threshold": DEFAULT_THRESHOLD,
        "kind": "multilabel_text",
    }
    return bundle, metrics


# --------------------------------------------------------------------------- #
# Analyzer
# --------------------------------------------------------------------------- #
class NoteAnalyzer:
    """Turn a delivery note into structured, explained handling tags.

    Construct from a trained bundle (rule + learned layers) or with ``None`` for
    a rule-only analyzer that still works with zero artifacts.
    """

    def __init__(self, bundle: dict | None = None):
        self.bundle = bundle
        self._vocab = None
        if bundle:
            self.tag_keys = bundle["tag_keys"]
            self.threshold = bundle.get("threshold", DEFAULT_THRESHOLD)
            # inverse vocabulary for token-level reasoning
            self._vocab = {i: t for t, i in bundle["vectorizer"].vocabulary_.items()}
        else:
            self.tag_keys = list(TAG_KEYS)
            self.threshold = DEFAULT_THRESHOLD

    # -- model layer ------------------------------------------------------ #
    def _model_scores(self, text: str):
        if not self.bundle:
            return {}, None
        vec = self.bundle["vectorizer"]
        clf = self.bundle["classifier"]
        X = vec.transform([text])
        scores = {}
        for i, key in enumerate(self.tag_keys):
            p = float(clf.estimators_[i].predict_proba(X)[0, 1])
            scores[key] = p
        return scores, X

    def _top_tokens(self, key: str, X, k: int = 3) -> list[str]:
        """Exact linear reasoning: tokens with the largest tfidf*coef push."""
        if X is None:
            return []
        idx = self.tag_keys.index(key)
        coef = self.bundle["classifier"].estimators_[idx].coef_[0]
        row = X.tocoo()
        contribs = [(self._vocab.get(j, "?"), row.data[p] * coef[j])
                    for p, j in enumerate(row.col)]
        contribs = [c for c in contribs if c[1] > 0]
        contribs.sort(key=lambda c: -c[1])
        return [tok for tok, _ in contribs[:k]]

    # -- public API ------------------------------------------------------- #
    def analyze(self, text: str | None) -> dict:
        text = (text or "").strip()
        if not text:
            return {"text": "", "tags": [], "time_window": None,
                    "floor": None, "priority_score": 0, "summary": "No notes."}

        rule_hits = _rule_hits(text)
        model_scores, X = self._model_scores(text)

        tags = []
        for key in self.tag_keys:
            meta = TAG_BY_KEY[key]
            in_rules = key in rule_hits
            prob = model_scores.get(key, 0.0)
            in_model = prob >= self.threshold
            if not (in_rules or in_model):
                continue

            evidence, source = [], []
            if in_rules:
                evidence.extend(f'matched "{p}"' for p in rule_hits[key][:2])
                source.append("rule")
            if in_model:
                toks = self._top_tokens(key, X)
                if toks:
                    evidence.append("key phrase: " + ", ".join(toks))
                source.append("model")
            # rule match is treated as high confidence; blend with model prob.
            confidence = max(prob, 0.9 if in_rules else 0.0)

            tags.append({
                "key": key,
                "label": meta.label,
                "icon": meta.icon,
                "severity": meta.severity,
                "action": meta.action,
                "confidence": round(float(confidence), 3),
                "source": "+".join(source),
                "evidence": evidence,
            })

        # Order by severity then confidence for display.
        tags.sort(key=lambda d: (-SEVERITY_WEIGHT[d["severity"]], -d["confidence"]))

        window = extract_time_window(text)
        floor = extract_floor(text)
        if window:
            for d in tags:
                if d["key"] == "time_window":
                    d["window"] = window
                    d["evidence"].append(f"window {_fmt_window(window)}")

        total = sum(SEVERITY_WEIGHT[d["severity"]] for d in tags)
        priority = min(100, round(100 * total / 8))

        summary = self._summary(tags, window)
        return {
            "text": text,
            "tags": tags,
            "time_window": window,
            "floor": floor,
            "priority_score": priority,
            "summary": summary,
        }

    @staticmethod
    def _summary(tags: list[dict], window: dict | None) -> str:
        if not tags:
            return "No special handling detected."
        parts = [t["label"] for t in tags]
        if window:
            parts = [f"Time window {_fmt_window(window)}" if p == "Time window" else p
                     for p in parts]
        return " · ".join(parts)
