"""
Provenance Guard — Flask app.

M4 scope: POST /submit now runs both signals (stylometric + LLM),
         combines them via confidence_scorer, and maps the combined
         score to a verdict tier. If the LLM signal is unavailable
         (no API key, network failure, parse error) the system falls
         back to stylometric-only and FORCES the verdict to 'uncertain'
         — a single signal isn't trustworthy enough to make a confident
         call. See planning.md §1 ("Failure mode handling").

         M5 will replace the placeholder label with the three §3
         variants and add POST /appeal.
"""

import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from stylometric_analyzer import analyze_stylometric
from llm_classifier import analyze_llm, GroqUnavailable
from confidence_scorer import combine, verdict_tier
from audit_log import init_db, log_decision, get_recent_entries

app = Flask(__name__)

# Rate limiting. Limits chosen for M5 — see README for justification.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
)

init_db()


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute; 100 per hour")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "field 'text' is required and must be non-empty"}), 400

    creator_id = data.get("creator_id") or "anonymous"

    # --- Signal 1: stylometric (local, fast) ---
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

    # --- Combine + verdict ---
    if llm_score is not None:
        confidence = combine(styl_score, llm_score)
        attribution = verdict_tier(confidence)
        fallback_note = ""
    else:
        # Fallback per planning.md §1: stylometric-only + force uncertain.
        confidence = round(styl_score, 3)
        attribution = "uncertain"
        fallback_note = f" (LLM unavailable: {llm_error}; verdict forced to uncertain)"

    # M4 placeholder label. M5 will replace with the §3 variants.
    label = (
        f"[M4 PLACEHOLDER - to be replaced in M5] "
        f"attribution={attribution}, confidence={confidence:.2f}{fallback_note}"
    )

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

    response_body = {
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
    }
    return jsonify(response_body), 200


@app.route("/log", methods=["GET"])
def log():
    """Return the most recent audit entries. No auth in v1 — grading-only."""
    entries = get_recent_entries(limit=50)
    return jsonify({"entries": entries, "count": len(entries)}), 200


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "rate limit exceeded",
        "detail": str(e.description),
    }), 429


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
