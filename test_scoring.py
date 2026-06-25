"""
Test the confidence scorer in isolation.

Spec (M4): "Verify that the generated scoring function actually matches the
thresholds you defined in your planning document."

Thresholds: human <= 0.25, AI > 0.70, uncertain in between (after M4
calibration; originally 0.80 but lowered — see planning.md §2).
"""
from confidence_scorer import combine, verdict_tier


CASES = [
    # (styl, llm, expected_combined, expected_tier, label)
    (0.00, 0.00, 0.000, "likely_human", "both signals strongly human"),
    (0.20, 0.20, 0.200, "likely_human", "both mildly human"),
    (0.30, 0.30, 0.300, "uncertain",    "both mildly AI, just above human threshold"),
    (0.50, 0.50, 0.500, "uncertain",    "both at 0.5 - genuine middle"),
    (0.70, 0.70, 0.700, "uncertain",    "exactly at AI threshold - NOT likely_ai"),
    (0.71, 0.71, 0.710, "likely_ai",    "just past AI threshold"),
    (0.80, 0.80, 0.800, "likely_ai",    "well into AI region"),
    (1.00, 1.00, 1.000, "likely_ai",    "both signals max"),
    # Signal disagreement → uncertain (LLM bias dampened by stylometric)
    (0.10, 0.90, 0.580, "uncertain",    "LLM strong AI, stylometric strong human"),
    (0.90, 0.10, 0.420, "uncertain",    "stylometric strong AI, LLM strong human"),
    # Realistic M4 calibration target
    (0.394, 0.95, 0.728, "likely_ai",   "extreme_AI_marketing-like (M4 calibration)"),
]


def main():
    passed = 0
    failed = 0
    for styl, llm, exp_combined, exp_tier, label in CASES:
        got_combined = combine(styl, llm)
        got_tier = verdict_tier(got_combined)
        ok = (abs(got_combined - exp_combined) < 0.005) and (got_tier == exp_tier)
        marker = "  OK " if ok else "FAIL "
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"{marker} styl={styl:.3f} llm={llm:.2f}  -> "
              f"combined={got_combined:.3f} ({got_tier:<13})  "
              f"expected {exp_combined:.3f} ({exp_tier})  | {label}")
    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
