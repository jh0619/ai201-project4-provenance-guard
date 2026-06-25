"""
Standalone test for stylometric_analyzer. Run this BEFORE wiring into Flask.

Spec (M3): "Test it independently before wiring it into the endpoint —
call the function directly with a few test inputs and inspect the output."

Expected behavior:
  - ai_like sample (uniform, polished) should score > 0.5
  - human_like sample (varied, conversational) should score < 0.5
  - short sample returns a score but with a warning
"""
import json
from stylometric_analyzer import analyze_stylometric

SAMPLES = {
    "ai_like": (
        "The journey of self-discovery is a deeply transformative process that "
        "requires patience and persistence. Many people find that journaling "
        "helps them understand their emotions and thoughts. Mindfulness "
        "practices can also contribute significantly to personal growth and "
        "well-being. It is important to be kind to yourself throughout this "
        "process. Remember that growth takes time and consistent effort. "
        "By embracing these practices, individuals can develop a deeper "
        "understanding of themselves and their place in the world."
    ),
    "human_like": (
        "OK so yesterday I tried that ramen place on 3rd. Insane. Like, "
        "the broth was kind of just OK? Decent, not blow-your-mind. But "
        "the noodles. Springy. Fresh. Perfect texture. Got the spicy miso. "
        "Burned my tongue twice. Worth it. The lady at the counter "
        "remembered me from last time which honestly weirded me out a "
        "little. Going back tomorrow probably. Maybe Friday."
    ),
    "short": "I love coffee in the morning.",
    "empty": "",
}


def main():
    for name, text in SAMPLES.items():
        result = analyze_stylometric(text)
        print(f"\n=== {name} ===")
        print(f"  text: {text[:80]}{'...' if len(text) > 80 else ''}")
        print(f"  score: {result['score']}  (higher = more AI-like)")
        print(f"  features: {json.dumps(result['features'], indent=4)}")
        if result.get("warning"):
            print(f"  warning: {result['warning']}")


if __name__ == "__main__":
    main()
