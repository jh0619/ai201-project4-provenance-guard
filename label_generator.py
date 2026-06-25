"""
Label Generator.

Maps a verdict tier + combined confidence score to the user-facing
transparency label text. The three variants are verbatim from
planning.md §3 — they are a UX-as-spec decision, not a code-style choice,
so changes here MUST be matched in planning.md and vice versa.

`{N}` placeholders in §3 become `round(combined_score * 100)` — the
"AI-likelihood score" shown to the reader.
"""


def make_label(verdict_tier: str, combined_score: float) -> str:
    """
    Return the exact transparency label text for the given verdict tier.

    verdict_tier:    'likely_human' | 'uncertain' | 'likely_ai'
    combined_score:  float in [0, 1]; rendered as a percentage in the label.
    """
    pct = round(combined_score * 100)

    if verdict_tier == "likely_human":
        return (
            "Likely written by a human.\n"
            "Our detector found the variation in sentence rhythm and word "
            "choice that is typical of human writing.\n"
            f"AI-likelihood score: {pct}% (low confidence in AI)"
        )

    if verdict_tier == "uncertain":
        return (
            "Inconclusive.\n"
            "Our detector found mixed signals — some patterns look human, "
            "others look AI-generated. We are not confident either way; "
            "treat this result as undetermined.\n"
            f"AI-likelihood score: {pct}%\n"
            "If you are the creator and believe a clearer determination "
            "should have been made, you can appeal."
        )

    if verdict_tier == "likely_ai":
        return (
            "Likely AI-generated.\n"
            "Our detector found strong signals — uniform sentence "
            "structure, generic phrasing — that suggest this content was "
            "produced by an AI model.\n"
            f"AI-likelihood score: {pct}% (high)\n"
            "Detection is not perfect. If you are the creator and wrote "
            "this yourself, you can appeal this label."
        )

    raise ValueError(f"unknown verdict_tier: {verdict_tier!r}")
