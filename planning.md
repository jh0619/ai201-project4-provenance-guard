# Provenance Guard — Planning

This document captures the design decisions made _before_ writing implementation code, per Milestone 1. The actual code in this repo implements the contracts and choices described here.

---

## 1. Architecture Narrative

### Submission flow (plain English)

A client sends `POST /submit` with the raw text and an optional `creator_id`. The request first passes through **Flask-Limiter**, which rejects callers that have exceeded their per-window quota (returns `429`). Surviving requests reach the **submission controller**, which generates a fresh `submission_id` (UUID) and orchestrates the detection pipeline.

The pipeline runs two distinct signals against the same text:

- **Signal 1 — Stylometric Analyzer** (pure Python, local, ~ms latency). Computes statistical features: sentence-length variance, type-token ratio (TTR), punctuation density, average sentence length. Features are mapped to a normalized score in `[0, 1]` where 0 = looks human, 1 = looks AI.
- **Signal 2 — LLM Classifier** (Groq `llama-3.3-70b-versatile`, network call, ~hundreds of ms). Sends the text with a system prompt asking the model to assess whether the writing reads as AI-generated, and to return a probability plus a one-sentence rationale. We parse the probability into `[0, 1]` on the same scale.

Both scores flow into the **confidence scorer**, which produces a single combined score and assigns one of three verdict tiers based on asymmetric thresholds (see §3). The **label generator** takes the verdict tier plus the confidence number and selects the corresponding pre-written label variant, filling in the percentage.

Before responding, the **audit logger** writes a structured row to SQLite capturing `submission_id`, timestamp, text hash (not the raw text, for privacy), individual signal scores, combined confidence, verdict tier, label text, and a default `status = "decided"`. The HTTP response returns `submission_id`, `verdict`, `confidence`, `label_text`, and the per-signal breakdown so the calling platform can show transparency to readers.

### Appeal flow (plain English)

A creator who disagrees with a verdict sends `POST /appeal` with the `submission_id` and their written reasoning. The **appeal controller** looks up the original decision in the audit log; if it doesn't exist, return `404`. Otherwise the **status updater** flips the submission's status from `"decided"` to `"under_review"`, and the audit logger inserts a new row referencing the original `submission_id`, containing the appeal text, an `appeal_id`, and a timestamp. The response confirms receipt with the new status. There is no automated re-classification — a human reviewer would pick up `under_review` items downstream, which is the appropriate place to put a human in the loop.

---

## 2. Detection Signals

The pipeline uses two genuinely independent signals — one **structural** (statistical features of the text itself) and one **semantic** (a model's holistic read). They fail in different ways, which is exactly why combining them is more informative than either alone.

### Signal 1: Stylometric Heuristics (structural)

**What it measures.** Numeric properties of the text that don't depend on understanding what it says:

- _Sentence-length variance / burstiness_ — standard deviation of sentence lengths in tokens.
- _Type-token ratio (TTR)_ — unique words divided by total words; a vocabulary-diversity measure.
- _Punctuation density_ — non-alphanumeric chars per 100 chars.
- _Average sentence length_ — mean sentence length in tokens.

**Why this differs between human and AI writing.** Current LLMs sample from probability distributions optimized for fluency, which tends to produce _uniform_ prose: sentence lengths cluster around the mean, vocabulary is "safe" (lower TTR), and punctuation patterns are regular. Human writers — especially in creative work — vary unpredictably: a 3-word sentence next to a 40-word one, a sudden rare word, an unexpected dash. Burstiness is the most well-studied of these for AI detection.

**Blind spots.**

- **Short text.** Variance metrics need a sample size. A haiku has 1–3 sentences; sentence-length variance is meaningless.
- **AI text polished by a human.** A human editing AI output reintroduces variance and can completely defeat this signal.
- **Humans who write uniformly by nature.** Technical writers, lawyers, students trained on the 5-paragraph essay, news copy editors — their human writing already looks "AI-uniform."
- **Genre baseline shift.** Sonnets have structurally forced regularity; flash fiction has structurally forced variance. Comparing them against the same threshold is wrong. We do not adjust for genre in v1, which is a known weakness.

### Signal 2: LLM-based Classifier (semantic)

**What it measures.** The "feel" of the text as judged by a capable language model. The model is prompted to consider things a statistical analyzer can't: phrasing tics ("delve into," "tapestry of," excessive hedging), idea progression, the _kind_ of mistakes (typos and idiosyncratic voice vs. fluent-but-generic prose), and overall coherence pattern.

**Why this differs between human and AI writing.** LLMs trained on overlapping corpora share characteristic surface features (favorite words, transition patterns, a hedging cadence), and modern frontier models can usually spot the signature when asked directly. Human writing has idiosyncratic voice — the same word used three different ways, a regional turn of phrase, a half-finished thought.

**Blind spots.**

- **Adversarial prompting of the source.** A user who prompted "write like a human with typos and tangents" can defeat the classifier easily.
- **Newer / different-family AI.** A classifier built on Groq's Llama is least reliable at detecting output from the same model family (and from very new models the patterns haven't been studied for).
- **The classifier is itself an LLM,** with its own biases — most notably, it can mistake fluent non-native-English writing for AI because both share surface uniformity. This is a real harm vector and a reason not to weight this signal at 100%.
- **Short text or very domain-specific text** — the classifier has less signal to work with on a 50-word fragment or a paragraph of dense legal jargon.

### Why these two together

Stylometric is interpretable, deterministic, and free. LLM-classifier is more sophisticated and catches things the stats miss, but has expensive failure modes (especially the non-native-English bias). The signals fail in different ways: short text breaks stylometric reliability but not the LLM read; adversarial "write like a human" prompts break the LLM read but the resulting text often still has tell-tale uniformity at the stats level. The combination is more robust than either alone, and the per-signal breakdown returned to the platform makes the disagreement _visible_ rather than hidden inside an ensemble black box.

---

## 3. Confidence Scoring and the False-Positive Asymmetry

A false positive on a writing platform — calling a human's work AI-generated — is more harmful than a false negative. It's an accusation. It can damage a creator's reputation and chill submission. The system must reflect this asymmetry in two ways:

1. **What `0.5` means.** A combined confidence of 0.5 means "the signals genuinely don't agree, or both were equivocal." It is not "lean AI." It is uncertainty, and the label should communicate that, not paper over it.
2. **Asymmetric verdict thresholds.** We do not split the score evenly into three thirds. Instead:

| Combined score | Verdict tier            | Reasoning                                                                                                    |
| -------------- | ----------------------- | ------------------------------------------------------------------------------------------------------------ |
| `≤ 0.25`       | `high-confidence human` | Cheap to say — the worst case is missing an AI submission, which is recoverable through community reporting. |
| `0.25 – 0.80`  | `uncertain`             | Wide middle band. Default to acknowledging uncertainty rather than making a call.                            |
| `> 0.80`       | `high-confidence AI`    | High bar. We only level this label when _both_ signals agree strongly.                                       |

**Combining the two signals.** Weighted average, with the LLM signal weighted higher because it captures more nuance, but not high enough to dominate (which would let its non-native-English bias slip through):

```
combined = 0.4 * stylometric_score + 0.6 * llm_score
```

The README will document the calibration test: pass a set of known-human and known-AI samples through the pipeline and confirm that (a) known-human samples mostly land in the human or uncertain buckets, never high-confidence AI; (b) the few false positives that happen land in `uncertain`, not in `high-confidence AI`.

---

## 4. False-Positive Walkthrough

Tracing the worst plausible scenario through the system, end-to-end:

> A grad student submits a polished personal essay to a writing platform that uses Provenance Guard. They revised the piece eight times — every sentence is tight, transitions are smooth, vocabulary is consistent across paragraphs.

**Detection.**

- _Stylometric:_ TTR is moderate, sentence-length variance is low (heavy revision flattens variance), punctuation density is normal. Normalized score: `0.62` toward AI.
- _LLM:_ The model reports the text reads as "fluent and consistent, no obvious human idiosyncrasies." Probability: `0.58` toward AI.
- _Combined:_ `0.4 × 0.62 + 0.6 × 0.58 = 0.59`.

**Verdict.** `0.59` falls inside the `0.25–0.80` band → verdict tier is `uncertain`. The label does NOT say "AI-generated." It says something like _"Mixed signals — we couldn't determine confidently."_ This is the asymmetry doing its job: a 0.59 combined score is enough to suspect, but not enough to accuse.

**The creator's experience.** They see a label that flags uncertainty without slandering them. The label includes a visible "Appeal this" affordance.

**Appeal.** Creator sends `POST /appeal` with `submission_id` and reasoning: _"I wrote this myself. I revise heavily, which probably makes my writing look uniform. Happy to share my draft history."_ The endpoint logs the appeal, flips the submission's status to `under_review`, and writes a linked entry in the audit log. The original decision is preserved (we don't overwrite history). A human moderator picks up `under_review` items downstream.

**What this trace tells us about the design.** The asymmetric thresholds are doing real work. The wide `uncertain` band is the explicit place where we say "we don't know" instead of risking a false positive. The appeal flow gives the creator a path to recover even when the signals were genuinely ambiguous.

---

## 5. API Surface

The contract every other piece of code implements. Auth/header details are out of scope for the prototype.

### `POST /submit`

Rate limited (limits documented in README).

Request:

```json
{
  "text": "string, required, the content to analyze",
  "creator_id": "string, optional, for per-creator analytics"
}
```

Response `200`:

```json
{
  "submission_id": "uuid",
  "verdict": "human | uncertain | ai",
  "confidence": 0.0,
  "label_text": "string, the exact transparency label",
  "signals": {
    "stylometric": 0.0,
    "llm": 0.0
  }
}
```

Errors: `400` (missing/empty text), `429` (rate limit), `500` (Groq unavailable — falls back to stylometric-only with the verdict tier forced to `uncertain`).

### `POST /appeal`

Request:

```json
{
  "submission_id": "uuid, required",
  "reasoning": "string, required, the creator's explanation"
}
```

Response `200`:

```json
{
  "appeal_id": "uuid",
  "submission_id": "uuid",
  "status": "under_review"
}
```

Errors: `404` (submission not found), `400` (missing fields).

### `GET /log`

Returns the audit log entries (newest first). Used for transparency and grading. No auth in the prototype; in production this would be admin-only.

Response `200`:

```json
[
  {
    "entry_id": "uuid",
    "entry_type": "decision | appeal",
    "submission_id": "uuid",
    "timestamp": "ISO 8601",
    "...": "type-specific fields"
  }
]
```

### `GET /submission/<id>` (helper)

Returns the full decision record for a single submission. Useful for the appeals UI on the client platform.

---

## Architecture

ASCII diagram of both flows. Arrows are labeled with what passes between components.

```
═══════════════════════════════════════════════════════════════════
                        SUBMISSION FLOW
═══════════════════════════════════════════════════════════════════

  Client
    │
    │  POST /submit { text, creator_id? }
    ▼
  ┌─────────────────────┐
  │   Rate Limiter      │   (Flask-Limiter, per-IP + per-creator)
  └─────────┬───────────┘
            │ raw text  (429 if over quota)
            ▼
  ┌────────────────────────────────────────────┐
  │        Submission Controller               │
  │  - generates submission_id (UUID)          │
  │  - orchestrates the two signals            │
  └─────────┬──────────────────────────────────┘
            │ raw text
            │
            ├──────────────────────────┐
            ▼                          ▼
  ┌─────────────────────┐    ┌──────────────────────┐
  │ Signal 1            │    │ Signal 2             │
  │ Stylometric         │    │ LLM Classifier       │
  │ Analyzer            │    │ (Groq llama-3.3-70b) │
  │                     │    │                      │
  │ - sentence variance │    │ - holistic semantic  │
  │ - type-token ratio  │    │   evaluation         │
  │ - punct. density    │    │ - returns probability│
  │ - avg sent. length  │    │   + rationale        │
  └─────────┬───────────┘    └──────────┬───────────┘
            │ score_styl ∈ [0,1]        │ score_llm ∈ [0,1]
            │                           │
            └─────────────┬─────────────┘
                          ▼
              ┌────────────────────────┐
              │  Confidence Scorer     │
              │                        │
              │  combined =            │
              │   0.4·styl + 0.6·llm   │
              │                        │
              │  → verdict tier via    │
              │  asymmetric thresholds │
              └───────────┬────────────┘
                          │ combined ∈ [0,1] + verdict tier
                          ▼
              ┌────────────────────────┐
              │  Label Generator       │
              │  tier → text variant   │
              │  (3 variants)          │
              └───────────┬────────────┘
                          │ label_text
                          │
          ┌───────────────┴───────────────┐
          ▼                               ▼
  ┌──────────────────┐           ┌──────────────────────┐
  │  Audit Logger    │           │  HTTP Response (200) │
  │  → SQLite        │           │  {                   │
  │                  │           │   submission_id,     │
  │  stores:         │           │   verdict,           │
  │  id, ts, hash,   │           │   confidence,        │
  │  signal scores,  │           │   label_text,        │
  │  combined,       │           │   signals: {styl,llm}│
  │  verdict, label, │           │  }                   │
  │  status=decided  │           └──────────────────────┘
  └──────────────────┘


═══════════════════════════════════════════════════════════════════
                         APPEAL FLOW
═══════════════════════════════════════════════════════════════════

  Creator
    │
    │  POST /appeal { submission_id, reasoning }
    ▼
  ┌─────────────────────────┐
  │   Appeal Controller     │
  └──────────┬──────────────┘
             │ submission_id
             ▼
  ┌─────────────────────────┐
  │   Audit Log (SQLite)    │   ← lookup original decision
  │                         │     (404 if not found)
  └──────────┬──────────────┘
             │ original record + reasoning
             ▼
  ┌─────────────────────────┐
  │   Status Updater        │   status: "decided" → "under_review"
  └──────────┬──────────────┘
             │
             ▼
  ┌─────────────────────────┐         ┌──────────────────────┐
  │   Audit Logger          │ ──────► │  HTTP Response (200) │
  │   inserts appeal row    │         │  {                   │
  │   linked to original    │         │   appeal_id,         │
  │   submission_id         │         │   submission_id,     │
  └─────────────────────────┘         │   status:            │
                                      │     "under_review"   │
                                      │  }                   │
                                      └──────────────────────┘
```

---
