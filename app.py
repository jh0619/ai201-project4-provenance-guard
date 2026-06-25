"""
Provenance Guard — Flask app.

M3 scope: POST /submit wires in the stylometric signal only.
         Confidence is the stylometric score directly; label is a placeholder.
         M4 will add the LLM signal and proper weighted scoring.
         M5 will replace the placeholder label with the three §3 variants and
         add POST /appeal.
"""

import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from stylometric_analyzer import analyze_stylometric
from audit_log import init_db, log_decision, get_recent_entries

app = Flask(__name__)

# Rate limiting. Limits chosen for M5 — see README for justification.
# Starting values: 10 per minute, 100 per hour per IP. Tightened or
# loosened in M5 after observing real usage patterns.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],  # only apply to explicitly-decorated routes
)

# Create DB tables on import. Idempotent.
init_db()


# ---- Thresholds (planning.md §2) -------------------------------------------
# Asymmetric: AI verdict requires high score (>0.80). False-positive aware.
def _attribution_for(score: float) -> str:
    if score <= 0.25:
        return "likely_human"
    if score > 0.80:
        return "likely_ai"
    return "uncertain"


# ---- Routes ----------------------------------------------------------------
@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute; 100 per hour")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "field 'text' is required and must be non-empty"}), 400

    creator_id = data.get("creator_id") or "anonymous"

    # --- Signal 1: stylometric (M3) ---
    styl_result = analyze_stylometric(text)
    styl_score = styl_result["score"]

    # M3 placeholder: confidence = stylometric score directly.
    # M4 will replace with combined = 0.4*styl + 0.6*llm.
    confidence = styl_score
    attribution = _attribution_for(confidence)

    # M3 placeholder label. M5 will replace with the §3 variants.
    label = f"[M3 PLACEHOLDER — to be replaced in M5] attribution={attribution}, confidence={confidence:.2f}"

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
        llm_score=None,  # populated in M4
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
            # llm signal added in M4
        },
        "status": "classified",
    }), 200


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
