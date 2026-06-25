"""
M4 verification: test the full pipeline on 4 deliberately chosen inputs.

Spec: "Test your scoring with at least 4 deliberately chosen inputs:
something you're confident is AI-generated, something you're confident
is human-written, and two borderline cases."

Run this AFTER setting GROQ_API_KEY in .env.

If the LLM signal fails, the script still prints stylometric scores so
you can debug which signal is misbehaving (spec hint: "print both signal
scores separately to find which one is misbehaving").
"""
import json

from stylometric_analyzer import analyze_stylometric
from confidence_scorer import combine, verdict_tier

try:
    from llm_classifier import analyze_llm, GroqUnavailable
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False


SAMPLES = [
    (
        "clearly_AI",
        "Artificial intelligence represents a transformative paradigm shift in modern "
        "society. It is important to note that while the benefits of AI are numerous, "
        "it is equally essential to consider the ethical implications. Furthermore, "
        "stakeholders across various sectors must collaborate to ensure responsible "
        "deployment.",
        "expect: likely_ai (high combined score)",
    ),
    (
        "clearly_human",
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium in it "
        "and i was thirsty for like three hours after. my friend got the spicy "
        "version and said it was better. probably won't go back unless someone "
        "drags me there",
        "expect: likely_human (low combined score)",
    ),
    (
        "borderline_formal_human",
        "The relationship between monetary policy and asset price inflation has "
        "been extensively studied in the literature. Central banks face a "
        "fundamental tension between their mandate for price stability and the "
        "unintended consequences of prolonged low interest rates on equity and "
        "real estate valuations.",
        "expect: uncertain (formal human writing looks AI-uniform)",
    ),
    (
        "borderline_edited_AI",
        "I've been thinking a lot about remote work lately. There are genuine "
        "tradeoffs — flexibility and no commute on one side, isolation and "
        "blurred work-life boundaries on the other. Studies show productivity "
        "varies widely by individual and role type.",
        "expect: uncertain (lightly edited AI; mixed signals)",
    ),
    (
        "extreme_AI_marketing",
        # 5th sample: deliberately long + repetitive + formulaic to demonstrate
        # that likely_ai IS reachable when BOTH signals strongly agree.
        # The spec set's 4 inputs are too short to push stylometric high enough
        # to cross the strict >0.80 AI threshold even with a confident LLM.
        "In today's competitive landscape, businesses must leverage cutting-edge "
        "technology to achieve sustainable growth. Companies that embrace digital "
        "transformation gain significant advantages over their competitors. By "
        "implementing innovative strategies, organizations can streamline "
        "operations and enhance productivity. Furthermore, adopting data-driven "
        "approaches enables companies to make informed decisions and drive better "
        "outcomes. Additionally, businesses must continuously evolve and adapt to "
        "stay ahead in this dynamic environment. Ultimately, success in the "
        "modern era requires a commitment to continuous improvement and strategic "
        "innovation.",
        "expect: likely_ai (long, uniform, repetitive marketing-speak)",
    ),
]


def run_one(name, text, expectation):
    print(f"\n{'='*70}")
    print(f"  {name}")
    print(f"  {expectation}")
    print('='*70)

    styl = analyze_stylometric(text)
    styl_score = styl["score"]
    print(f"  stylometric score: {styl_score:.3f}  "
          f"(burstiness={styl['features']['burstiness']:.2f}, "
          f"ttr={styl['features']['ttr']:.3f}, "
          f"words={styl['features']['word_count']})")

    if not LLM_AVAILABLE:
        print("  llm signal:        SKIPPED (groq not importable)")
        return

    try:
        llm = analyze_llm(text)
        llm_score = llm["score"]
        print(f"  llm score:         {llm_score:.3f}")
        print(f"  llm rationale:     {llm['rationale']}")

        combined = combine(styl_score, llm_score)
        tier = verdict_tier(combined)
        print(f"  combined:          {combined:.3f}  -> {tier}")
    except GroqUnavailable as e:
        print(f"  llm signal:        UNAVAILABLE ({e})")
        print("  -> would fall back to stylometric-only, verdict forced to 'uncertain'")


def main():
    for name, text, expectation in SAMPLES:
        run_one(name, text, expectation)
    print(f"\n{'='*70}")
    print("  Done. Review the scores: do the directions match the expectations?")
    print("  If clearly_AI doesn't score higher than clearly_human, investigate.")
    print('='*70)


if __name__ == "__main__":
    main()
