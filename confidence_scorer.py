"""
Confidence Scorer.

Combines the two detection signals into a single confidence score in [0, 1]
and maps it to a verdict tier using asymmetric thresholds. See planning.md
§1 (Combining the two signals) and §2 (Asymmetric verdict thresholds).
"""

# Weights chosen per planning.md §1:
#   LLM gets more weight (richer signal) but not enough to dominate,
#   so its non-native-English bias is dampened by stylometric.
STYLOMETRIC_WEIGHT = 0.4
LLM_WEIGHT = 0.6

# Asymmetric thresholds per planning.md §2:
#   The AI verdict has a high bar — a false positive (calling a human's
#   writing AI) is more harmful than a false negative. After M4 calibration
#   testing on the spec's 5 inputs, THRESHOLD_AI was lowered from the
#   original 0.80 to 0.70 because realistic two-signal scores rarely cross
#   0.80 even on obviously AI text. 0.70 keeps the asymmetry (still much
#   higher bar than the human side at 0.25) while making likely_ai reachable.
THRESHOLD_HUMAN = 0.25   # combined <= this → likely_human
THRESHOLD_AI = 0.70      # combined >  this → likely_ai
# Everything strictly between → uncertain (wide middle band, by design)


def combine(stylometric_score: float, llm_score: float) -> float:
    """
    Weighted average of the two signals.

    Both inputs are floats in [0, 1] on the same scale: 0 = strongly human,
    1 = strongly AI. The combined score is on the same scale and rounded
    to 3 decimals for cleaner audit-log values.
    """
    combined = STYLOMETRIC_WEIGHT * stylometric_score + LLM_WEIGHT * llm_score
    # Clip defensively in case an upstream signal returns a slightly OOB value.
    combined = max(0.0, min(1.0, combined))
    return round(combined, 3)


def verdict_tier(combined_score: float) -> str:
    """
    Map a combined score to one of the three attribution tiers.

    Returns: 'likely_human' | 'uncertain' | 'likely_ai'
    """
    if combined_score <= THRESHOLD_HUMAN:
        return "likely_human"
    if combined_score > THRESHOLD_AI:
        return "likely_ai"
    return "uncertain"
