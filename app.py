"""
Provenance Guard — Flask app.

M5: full production layer.
   - POST /submit returns the verbatim transparency label (3 variants
     mapped from the verdict tier per planning.md §3).
   - POST /appeal accepts content_id + creator_reasoning, updates the
     submission status to 'under_review', writes an appeal row to the
     audit log (original decision preserved unmodified).
   - Rate limit on /submit: 10/min, 100/day (rationale in README).
   - GET /log returns the audit log newest-first for transparency.

Fallback behavior is unchanged from M4: if Groq is unavailable, the
LLM signal is skipped and the verdict is forced to 'uncertain'.
"""

import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from stylometric_analyzer import analyze_stylometric
from llm_classifier import analyze_llm, GroqUnavailable
from confidence_scorer import combine, verdict_tier
from label_generator import make_label
from audit_log import (
    init_db,
    log_decision,
    log_appeal,
    update_status,
    get_recent_entries,
    get_submission,
)

app = Flask(__name__)

# Rate limiting. See README for justification of the chosen limits.
# storage_uri="memory://" silences the Flask-Limiter ≥3.x warning about
# implicit in-memory storage (per M5 spec setup note).
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

init_db()


# ---- POST /submit ----------------------------------------------------------
@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute; 100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "field 'text' is required and must be non-empty"}), 400

    creator_id = data.get("creator_id") or "anonymous"

    # --- Signal 1: stylometric ---
    styl_result = analyze_stylometric(text)
    styl_score = styl_result["score"]

    # --- Signal 2: LLM classifier (Groq) ---
    llm_score = None
    llm_rationale = None
    llm_error = None
    try:
        llm_result = analyze_llm(text)
        llm_score = llm_result["score"]
        llm_rationale = llm_result["rationale"]
    except GroqUnavailable as e:
        llm_error = str(e)

    # --- Combine + verdict + label ---
    if llm_score is not None:
        confidence = combine(styl_score, llm_score)
        attribution = verdict_tier(confidence)
    else:
        # Fallback per planning.md §1: stylometric-only + forced uncertain.
        confidence = round(styl_score, 3)
        attribution = "uncertain"

    label = make_label(attribution, confidence)
    if llm_error:
        label += f"\n\n[Note: LLM signal unavailable ({llm_error}); verdict based on stylometric only.]"

    content_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    log_decision(
        content_id=content_id,
        creator_id=creator_id,
        timestamp=timestamp,
        attribution=attribution,
        confidence=confidence,
        stylometric_score=styl_score,
        stylometric_features=styl_result["features"],
        label=label,
        status="classified",
        llm_score=llm_score,
        llm_rationale=llm_rationale,
    )

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        "attribution": attribution,
        "confidence": confidence,
        "label": label,
        "signals": {
            "stylometric": {
                "score": styl_score,
                "features": styl_result["features"],
                "warning": styl_result.get("warning"),
            },
            "llm": {
                "score": llm_score,
                "rationale": llm_rationale,
                "error": llm_error,
            },
        },
        "status": "classified",
    }), 200


# ---- POST /appeal ----------------------------------------------------------
@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    creator_reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "field 'content_id' is required"}), 400
    if not creator_reasoning:
        return jsonify({"error": "field 'creator_reasoning' is required"}), 400

    original = get_submission(content_id)
    if original is None:
        return jsonify({
            "error": f"submission '{content_id}' not found in audit log",
        }), 404

    appeal_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    # Per planning.md §4:
    #   1) update the decision row's status to "under_review"
    #   2) insert a new appeal row (original verdict PRESERVED unmodified)
    update_status(content_id, "under_review")
    log_appeal(
        appeal_id=appeal_id,
        content_id=content_id,
        creator_id=original.get("creator_id", "anonymous"),
        timestamp=timestamp,
        reasoning=creator_reasoning,
    )

    return jsonify({
        "appeal_id": appeal_id,
        "content_id": content_id,
        "status": "under_review",
        "timestamp": timestamp,
        "message": "Appeal received and logged. A human reviewer will follow up.",
    }), 200


# ---- GET /log --------------------------------------------------------------
@app.route("/log", methods=["GET"])
def log():
    """Return audit entries newest-first. No auth in v1 — grading-only."""
    entries = get_recent_entries(limit=50)
    return jsonify({"entries": entries, "count": len(entries)}), 200


# ---- GET /submission/<id> --------------------------------------------------
@app.route("/submission/<content_id>", methods=["GET"])
def submission_detail(content_id):
    entry = get_submission(content_id)
    if entry is None:
        return jsonify({"error": f"submission '{content_id}' not found"}), 404
    return jsonify(entry), 200


# ---- 429 handler -----------------------------------------------------------
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "rate limit exceeded",
        "detail": str(e.description),
    }), 429


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
