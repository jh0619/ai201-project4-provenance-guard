"""
Signal 1: Stylometric Heuristics.

Pure-Python statistical analysis of text. Computes four features and maps them
to a single score in [0, 1] where 0 = strongly human-looking, 1 = strongly
AI-looking. See planning.md §1 (Signal 1) for the full spec.

The function returns the score AND the raw feature values, so the audit log
and reviewer interface can see what drove the verdict.
"""

import re
from typing import Dict, Any


# Reference ranges used to normalize each feature into an "AI-likelihood"
# contribution. These are calibrated against the small sample set in
# tests/calibration_samples.md, not against a labeled dataset — they are
# heuristic anchors, not statistically derived.
#
# - Burstiness (std-dev of sentence lengths): human writing is typically 4-10+,
#   AI text tends to cluster around 1-4. Lower burstiness = more AI.
# - TTR (type-token ratio): human writing on 100-300 word samples typically
#   shows TTR 0.55-0.70; AI text often lower (0.45-0.55). Lower TTR = more AI.
# - Punctuation density: reported for transparency but not used in the score
#   for v1 because direction depends heavily on genre.
BURSTINESS_HUMAN_ANCHOR = 8.0    # burstiness at/above this → score 0 contribution
TTR_HUMAN_ANCHOR = 0.65          # ttr at/above this → score 0 contribution
TTR_RANGE = 0.25                 # span we normalize TTR over (0.40 - 0.65)

# Minimum content size to consider stylometric analysis reliable.
# Word threshold is lenient (30) because the real safety check is the
# sentence-count threshold below — burstiness is only meaningful with
# at least 3 sentences to take variance over.
MIN_WORDS_FOR_FULL_SIGNAL = 30
MIN_SENTENCES_FOR_VARIANCE = 3


def _split_sentences(text: str) -> list:
    """Cheap sentence splitter. Splits on . ! ? followed by whitespace."""
    parts = re.split(r"[.!?]+\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _tokenize(text: str) -> list:
    """Lowercase word tokens, stripping punctuation."""
    return re.findall(r"\b[\w']+\b", text.lower())


def analyze_stylometric(text: str) -> Dict[str, Any]:
    """
    Run stylometric analysis on `text`.

    Returns:
        {
            "score": float in [0, 1],   # higher = more AI-looking
            "features": {
                "burstiness":      float,   # std-dev of sentence lengths
                "ttr":             float,   # type-token ratio
                "punct_density":   float,   # non-alnum chars / total chars
                "avg_sent_len":    float,   # mean sentence length in tokens
                "word_count":      int,
                "sentence_count":  int,
            },
            "warning": str | None,          # populated for short / degenerate input
        }
    """
    sentences = _split_sentences(text)
    words = _tokenize(text)
    word_count = len(words)
    sentence_count = len(sentences)

    # Degenerate input: empty or near-empty text.
    if word_count < 5 or sentence_count == 0:
        return {
            "score": 0.5,
            "features": {
                "burstiness": 0.0,
                "ttr": 0.0,
                "punct_density": 0.0,
                "avg_sent_len": 0.0,
                "word_count": word_count,
                "sentence_count": sentence_count,
            },
            "warning": "text too short for reliable stylometric analysis",
        }

    # Per-sentence token counts
    sent_lengths = [len(_tokenize(s)) for s in sentences]
    avg_sent_len = sum(sent_lengths) / len(sent_lengths)

    # Feature 1: burstiness (population std-dev of sentence lengths)
    if sentence_count >= 2:
        mean = avg_sent_len
        variance = sum((l - mean) ** 2 for l in sent_lengths) / sentence_count
        burstiness = variance ** 0.5
    else:
        burstiness = 0.0

    # Feature 2: type-token ratio
    ttr = len(set(words)) / word_count

    # Feature 3: punctuation density (reported, not used in score for v1)
    punct_chars = sum(1 for c in text if not c.isalnum() and not c.isspace())
    punct_density = punct_chars / max(len(text), 1)

    # ---- Normalize to AI-likelihood contributions in [0, 1] ----
    # Burstiness: 8+ → 0 (very human), 0 → 1 (very AI)
    burstiness_ai = max(0.0, min(1.0,
        (BURSTINESS_HUMAN_ANCHOR - burstiness) / BURSTINESS_HUMAN_ANCHOR))

    # TTR: 0.65+ → 0 (very human), 0.40 → 1 (very AI)
    ttr_ai = max(0.0, min(1.0,
        (TTR_HUMAN_ANCHOR - ttr) / TTR_RANGE))

    # Combine: simple average of the two normalized features.
    # If the text is too short for burstiness to be reliable, fall back to TTR only.
    warning = None
    if word_count < MIN_WORDS_FOR_FULL_SIGNAL:
        score = ttr_ai
        warning = (
            f"limited signal: only {word_count} words "
            f"(burstiness excluded; threshold is {MIN_WORDS_FOR_FULL_SIGNAL})"
        )
    elif sentence_count < MIN_SENTENCES_FOR_VARIANCE:
        score = ttr_ai
        warning = (
            f"limited signal: only {sentence_count} sentences "
            f"(burstiness excluded; threshold is {MIN_SENTENCES_FOR_VARIANCE})"
        )
    else:
        score = (burstiness_ai + ttr_ai) / 2

    score = round(max(0.0, min(1.0, score)), 3)

    return {
        "score": score,
        "features": {
            "burstiness": round(burstiness, 2),
            "ttr": round(ttr, 3),
            "punct_density": round(punct_density, 3),
            "avg_sent_len": round(avg_sent_len, 1),
            "word_count": word_count,
            "sentence_count": sentence_count,
        },
        "warning": warning,
    }
