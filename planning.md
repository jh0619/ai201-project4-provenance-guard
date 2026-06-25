# Provenance Guard — Planning

This document is the implementation spec. The five required questions are answered in §1–§5. The diagram and brief narrative live in `## Architecture`. The AI prompting strategy for M3–M5 lives in `## AI Tool Plan`. A code generator should be able to read this document and produce an implementation that matches.

---

## 1. Detection Signals

The pipeline uses two genuinely independent signals — one **structural** (statistics of the text itself) and one **semantic** (a model's holistic read). They fail in different ways, which is exactly why combining them is more informative than either alone.

### Signal 1: Stylometric Heuristics (structural)

**What it measures.** Four numeric properties of the text that don't depend on understanding what it says:

| Feature                                 | Definition                                       | Direction                              |
| --------------------------------------- | ------------------------------------------------ | -------------------------------------- |
| Sentence-length variance (`burstiness`) | Standard deviation of sentence lengths in tokens | Higher → more human                    |
| Type-token ratio (`TTR`)                | Unique words ÷ total words                       | Higher (within range) → more human     |
| Punctuation density                     | Non-alphanumeric chars ÷ 100 chars               | More uniform → more AI                 |
| Average sentence length                 | Mean sentence length in tokens                   | Used as context, not weighted directly |

**Output shape.** A single float in `[0, 1]`, where `0` = strongly human-looking, `1` = strongly AI-looking. Each feature is normalized against a reference range (described in the implementation comments), the normalized values are averaged, and the result is clipped to `[0, 1]`. The function also returns the raw feature values for transparency.

```python
# Output contract
{
  "score": 0.62,                    # float in [0, 1], higher = more AI-like
  "features": {
    "burstiness": 4.2,
    "ttr": 0.51,
    "punct_density": 0.08,
    "avg_sent_len": 14.3
  }
}
```

**Why human and AI differ here.** Current LLMs sample from probability distributions optimized for fluency, which produces _uniform_ prose: sentence lengths cluster around the mean, vocabulary stays "safe" (lower TTR), punctuation is regular. Human writing — especially creative work — varies unpredictably.

**Blind spots.** Short text (variance metrics need sample size; a haiku has too few sentences); AI text polished by a human (the human reintroduces variance); humans who naturally write uniformly (technical writers, lawyers, students trained on the 5-paragraph essay); genre baseline shift (sonnets force regularity, flash fiction forces variance).

### Signal 2: LLM-based Classifier (semantic)

**What it measures.** The "feel" of the text as judged by Groq's `llama-3.3-70b-versatile`. The model is prompted with a clear system instruction to evaluate whether the writing reads as AI-generated, considering phrasing tics, idea progression, kind of mistakes, and overall coherence — things stylometry can't see.

**Output shape.** A single float in `[0, 1]` parsed out of a structured JSON response from Groq. Same scale as signal 1: `0` = strongly human, `1` = strongly AI. The model is also asked for a short rationale, which we capture for the audit log (not shown in the user label).

```python
# Output contract
{
  "score": 0.58,                    # float in [0, 1], higher = more AI-like
  "rationale": "Fluent but uniformly polished; no idiosyncratic voice markers."
}
```

**Prompt strategy (for implementation).** System prompt asks the model to (a) return JSON with `score` and `rationale` keys only, (b) score on `[0, 1]` with explicit anchors at 0.0 ("clearly human"), 0.5 ("genuinely undetermined"), and 1.0 ("clearly AI-generated"), (c) refuse to refuse — if the text is short or ambiguous, return 0.5 rather than declining.

**Why human and AI differ here.** LLMs share characteristic surface features across model families (favorite words, transition patterns, hedging cadence). A capable LLM can usually spot the signature when asked directly. Human writing has idiosyncratic voice.

**Blind spots.** Adversarial source prompting ("write like a human with typos"); newer / same-family AI output (a Llama classifier is least reliable at detecting Llama output); LLM-on-LLM bias — most importantly, the classifier can mistake fluent non-native-English writing for AI because both share surface uniformity; very short or domain-specific text gives the model little to read.

### Combining the two signals

Weighted average. The LLM signal gets more weight because it captures more nuance, but not enough to dominate (which would let its non-native-English bias slip through):

```
combined_score = 0.4 * stylometric_score + 0.6 * llm_score
```

`combined_score` is in `[0, 1]`. This single number, plus the asymmetric thresholds in §2, produces the verdict tier.

**Failure mode handling.** If Groq is unavailable, the system falls back to stylometric-only and forces the verdict tier to `uncertain` regardless of the score. This is documented in the API spec and is the right default because a single signal isn't trustworthy enough to make a confident call.

---

## 2. Uncertainty Representation

A confidence score is a design decision before it is a technical one. The decision here:

### What `combined_score = 0.6` means

It means **"the signals are leaning AI but not strongly, and we are not confident."** It is _not_ "60% probability of AI" — that would imply a calibrated probability, which we don't have. It is a weighted aggregate of two scores on the same `[0, 1]` scale, where:

- `0.0` = both signals strongly indicate human
- `0.5` = signals genuinely disagree, or both are equivocal — **we don't know**
- `1.0` = both signals strongly indicate AI

A user reading the label should see `0.6` and conclude "the system is not making a confident call here." The verdict tier and label text enforce that interpretation (see §3).

### Mapping signal outputs to a calibrated score

Both signals already emit on `[0, 1]` on the same scale (higher = AI). The weighted average is the mapping. We do **not** apply additional calibration (Platt scaling, isotonic regression, etc.) in v1 — calibration would require a labeled dataset we don't have. This is a known limitation, documented in the README. The asymmetric thresholds below partially compensate.

### Asymmetric verdict thresholds

A false positive on a writing platform — calling a human's work AI-generated — is worse than a false negative. The thresholds reflect this:

| `combined_score` range | Verdict tier | Label slot                  |
| ---------------------- | ------------ | --------------------------- |
| `≤ 0.25`               | `human`      | "Likely written by a human" |
| `0.25 – 0.70`          | `uncertain`  | "Inconclusive"              |
| `> 0.70`               | `ai`         | "Likely AI-generated"       |

The `uncertain` band is deliberately wide (0.45 of the total range). The `ai` band requires a high bar — both signals must agree strongly before we make an accusation. The `human` band is more permissive (≤0.25) because mislabeling AI as human is less harmful than the reverse.

**Calibration note (M4).** The AI threshold was originally `>0.80`. After running the 5-sample calibration test in M4, that bar proved unreachable for realistic two-signal outputs — even a strongly AI-looking text with `stylometric=0.39` and a confident `llm=0.95` only reached `combined=0.728`. Lowering to `>0.70` keeps the asymmetry (the AI side is still nearly 3× the human side) while making the AI verdict actually attainable on clearly-AI content.

### Verifying the score is meaningful

The README will document a calibration check: pass a fixed set of known-human and known-AI samples through the pipeline and confirm (a) scores vary across the range (not all clustered at 0.5), (b) clearly-AI samples score higher than clearly-human samples, and (c) any misclassifications among known-human samples land in `uncertain`, never in `ai`.

---

## 3. Transparency Label Design

The label is what readers on the platform see. It needs to be plain-language, communicate confidence honestly, and treat creators fairly — especially in the AI verdict, where the harm of a false positive is real.

Below are the **verbatim** label texts for all three variants. `{N}` is the AI-likelihood percentage (i.e. `combined_score * 100`, rounded).

### Variant 1 — `human` (combined_score ≤ 0.25)

> **Likely written by a human.**
> Our detector found the variation in sentence rhythm and word choice that is typical of human writing.
> AI-likelihood score: {N}% (low confidence in AI)

### Variant 2 — `uncertain` (0.25 < combined_score ≤ 0.70)

> **Inconclusive.**
> Our detector found mixed signals — some patterns look human, others look AI-generated. We are not confident either way; treat this result as undetermined.
> AI-likelihood score: {N}%
> If you are the creator and believe a clearer determination should have been made, you can appeal.

### Variant 3 — `ai` (combined_score > 0.70)

> **Likely AI-generated.**
> Our detector found strong signals — uniform sentence structure, generic phrasing — that suggest this content was produced by an AI model.
> AI-likelihood score: {N}% (high)
> Detection is not perfect. If you are the creator and wrote this yourself, you can appeal this label.

### Design rationale

- All three variants surface the same numeric score, so a curious reader can interpret it consistently — the score doesn't change meaning between labels.
- The `uncertain` label is the default when in doubt. It is the longest of the three because acknowledging uncertainty honestly takes more words than a clean verdict.
- The `ai` variant explicitly says _"Detection is not perfect"_ and surfaces the appeal path. The `human` variant has no appeal CTA because there's no harm to recover from.
- "AI-likelihood score" is preferred over "confidence" in the user-facing text because it's less ambiguous — `confidence` could be read as "confidence in the verdict" or "confidence the content is real," both wrong.

---

## 4. Appeals Workflow

### Who can submit an appeal

The creator of the content. In the prototype, we don't authenticate — anyone with a `submission_id` can file an appeal. On a real platform, the appeal endpoint would be authenticated and the platform would verify the appellant is the same account that submitted the content.

### What information they provide

A `submission_id` (to identify the contested decision) and `reasoning` (free text — the creator's explanation of why they believe the verdict is wrong). Optional in v2: a link to draft history or prior work. v1 keeps the surface minimal.

### What the system does on receipt

1. Look up the original decision by `submission_id`. If not found, return `404`.
2. Update the submission's `status` field from `"decided"` to `"under_review"`. **The original verdict, scores, and label are not modified** — preserving them is the entire point of an audit log.
3. Insert a new row into the audit log of `entry_type = "appeal"`, containing a new `appeal_id`, the original `submission_id`, the reasoning text, and a timestamp.
4. Return a confirmation with `appeal_id`, `submission_id`, and `status = "under_review"`.

No automated re-classification — a human reviewer is the appropriate place to make a final call, and v1 does not implement that reviewer interface.

### What a human reviewer would see when opening the appeal queue

For each appeal in the queue, the reviewer interface (out of scope for v1, but specified here so the data model supports it) should show:

- The original text (or hash + a way to retrieve it from the platform)
- The original verdict, combined score, and per-signal breakdown
- The exact transparency label the reader saw
- The creator's appeal reasoning
- A timeline: submission timestamp, appeal timestamp
- Two actions for the reviewer: `Uphold original verdict` or `Overturn`. (Both actions would write new audit log entries; the schema supports this.)

The audit log schema makes all of the above queryable. v1 implements only the data — the UI is a stretch goal under "Analytics dashboard."

---

## 5. Anticipated Edge Cases

The system will handle the following content types poorly. We name them up front rather than discovering them at grading time.

### Edge case 1 — Short content (haiku, tweet, micro-fiction under ~50 words)

**Why it breaks.** The stylometric signal needs sample size. Sentence-length variance over 1–3 sentences is statistical noise, not signal. The LLM classifier also has less to work with.

**What the system does today.** Both signals will return scores, but those scores are unreliable. The combined score is likely to fall in the `uncertain` band — which is the least harmful outcome.

**Mitigation in v1.** Document the minimum-length limitation in the API response. In v2, the `/submit` endpoint would short-circuit on text shorter than a threshold (e.g. 50 words) and return verdict = `uncertain` with a specific reason code (`"too_short_for_reliable_detection"`).

### Edge case 2 — Heavily-revised human writing with uniform style

**Why it breaks.** A grad student who has revised an essay eight times has flattened the natural variance in their writing — sentence lengths cluster, vocabulary is consistent, transitions are smooth. The stylometric signal cannot distinguish "polished" from "machine-uniform." The LLM classifier can be similarly fooled.

**What the system does today.** This is exactly the false-positive scenario the asymmetric thresholds were designed for. A score in the high-`0.5`s or low-`0.6`s falls into `uncertain`, not `ai`. The label says "Inconclusive" and surfaces the appeal path. The creator is not falsely accused.

### Edge case 3 — Fluent non-native English writing

**Why it breaks.** L2 English writing often has uniform sentence structure (a survival strategy in a second language) and a vocabulary range that overlaps with AI output. The LLM classifier in particular has a documented bias here — it can rate fluent-but-stilted writing as AI-like. This is a real fairness problem.

**What the system does today.** The 0.4-stylometric / 0.6-LLM weighting was set partly to dampen this — stylometric is bias-neutral on this axis. The wide `uncertain` band catches the rest. But the failure mode is genuine, and the README will name it explicitly.

### Edge case 4 — AI-assisted but human-edited writing

**Why it breaks.** The premise of the system is that content is either human or AI. Real creative practice increasingly mixes the two. Neither verdict is "correct" for a piece that was drafted by AI and substantially rewritten by a human (or vice versa).

**What the system does today.** Combined score lands in `uncertain` for genuinely mixed authorship — which, again, is the least harmful outcome. The label says "Inconclusive" honestly. The system is not designed to identify partial authorship in v1.

---

## Architecture

The system has two flows. The **submission flow** routes content through rate limiting, two parallel detection signals (stylometric + LLM), a weighted confidence scorer, and a label generator; every step writes to a SQLite audit log before the response goes back to the client. The **appeal flow** looks up the original decision, flips its status to `under_review`, and inserts a linked audit entry — the original verdict and scores are preserved unmodified.

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

## AI Tool Plan

For each implementation milestone, this section specifies which planning sections to paste into the AI prompt, what to ask for, and how to verify the output before moving on. The verification step matters most — code that "looks right" is the failure mode this section is designed to prevent.

### M3 — Submission endpoint + first signal (stylometric)

**Sections to provide to the AI tool:**

- §1 Detection Signals → "Signal 1: Stylometric Heuristics" subsection (entire)
- §1 Detection Signals → "Combining the two signals" (so the AI knows the output contract that the scorer will later consume)
- `## Architecture` section (the submission flow diagram)
- API Surface for `POST /submit` (in §6 below)

**What to ask the AI to generate:**

1. A Flask app skeleton in `app.py` with a `POST /submit` route. The route accepts `{ "text": str, "creator_id": str? }`, calls a stub `analyze(text)` function, and returns the response shape specified in §6.
2. A `stylometric_analyzer.py` module with a single function `analyze_stylometric(text) -> dict` that returns the `{ score, features }` shape specified in §1.
3. The `analyze(text)` stub in `app.py` should call `analyze_stylometric` and return a placeholder shape — the LLM signal and scoring come in M4.

**How to verify before wiring further:**

- Unit-test `analyze_stylometric` directly with three inputs:
  1. A 200+ word generic AI paragraph (e.g. a ChatGPT response on a generic topic) — expect `score > 0.5`.
  2. A 200+ word casual human text (e.g. a Reddit comment with typos and varied rhythm) — expect `score < 0.5`.
  3. A short snippet (~20 words) — expect a score returned (not a crash), and document that the score is unreliable.
- Hit `POST /submit` with `curl` or Postman using one of the above texts. Confirm the response has the right shape.

**Stop condition.** Only move to M4 once stylometric scores reliably differentiate the two sample texts. If they don't, the normalization in `analyze_stylometric` needs work before adding more code.

### M4 — Second signal (LLM) + confidence scoring

**Sections to provide:**

- §1 Detection Signals → "Signal 2: LLM-based Classifier" subsection (entire), including the prompt strategy
- §1 Detection Signals → "Combining the two signals" (the weighted-average formula and the Groq-failure fallback)
- §2 Uncertainty Representation (entire — including the threshold table)
- `## Architecture` section

**What to ask the AI to generate:**

1. An `llm_classifier.py` module with `analyze_llm(text) -> dict` returning `{ score, rationale }`. It must build the system prompt described in §1, call Groq, parse the JSON response defensively (the model sometimes returns prose around the JSON), and raise a defined exception on Groq failure.
2. A `confidence_scorer.py` module with two functions: `combine(styl_score, llm_score) -> float` (the weighted average) and `verdict_tier(combined) -> str` returning one of `"human" | "uncertain" | "ai"` using the §2 thresholds.
3. Update `app.py`'s `/submit` route to call both signals, the scorer, and the tier function — and to apply the Groq-failure fallback (stylometric-only + force verdict to `uncertain`).

**How to verify:**

- Run **10 calibration samples** through the full pipeline: 5 known-AI (ChatGPT/Claude outputs on different topics), 5 known-human (mix of casual and polished human writing). Record the combined score for each.
- Check three things:
  1. **Spread.** Scores span more than 0.3 of the range — they aren't all clustered around 0.5.
  2. **Direction.** The mean known-AI score is meaningfully higher than the mean known-human score.
  3. **No false-positive AI.** Zero known-human samples score `> 0.80`. If any do, either the thresholds need tightening or the signals need rebalancing.
- Test the Groq-failure path by temporarily setting an invalid `GROQ_API_KEY` and confirming the fallback returns verdict `uncertain` instead of crashing.

**Stop condition.** All three checks pass. Document the 10 samples and their scores in a `tests/calibration_samples.md` file for the README to reference.

### M5 — Label generation, appeals, audit log, rate limiting

**Sections to provide:**

- §3 Transparency Label Design (entire — all three verbatim variants)
- §4 Appeals Workflow (entire)
- `## Architecture` section (the appeal flow diagram especially)
- API Surface for `POST /appeal` and `GET /log`

**What to ask the AI to generate:**

1. `label_generator.py` with `make_label(verdict_tier, combined_score) -> str` that returns the exact verbatim text from §3 with `{N}` substituted as `round(combined_score * 100)`.
2. `audit_log.py` with a SQLite schema (one table or two — implementer's call, but the schema must support the reviewer view described in §4) and helper functions: `log_decision(...)`, `log_appeal(...)`, `get_log()`, `get_submission(id)`, `update_status(id, new_status)`.
3. A `POST /appeal` route in `app.py` implementing the 4-step process in §4 exactly.
4. A `GET /log` route returning audit log entries newest-first.
5. Rate limiting on `/submit`. Use Flask-Limiter with the limits documented in the README — start with `10 per minute, 100 per hour` per IP. Justification will live in the README.

**How to verify:**

- **All three label variants are reachable.** Submit three test texts engineered to land in each tier (a clean human Reddit comment → `human`; a polished generic paragraph → `uncertain`; an obvious ChatGPT response → `ai`). Confirm each returns the exact label text from §3.
- **Appeal flow works end-to-end.** `POST /submit` with any text, capture the `submission_id`, then `POST /appeal` with that ID + reasoning. Then `GET /log` and confirm: (a) the original decision row is unchanged, (b) a new appeal row exists with `entry_type = "appeal"` linked to the same `submission_id`, (c) the original submission's `status` is now `"under_review"`.
- **Audit log shows at least 3 entries.** After all the above, `GET /log` returns ≥3 entries. Copy a sample into the README per the deliverable requirement.
- **Rate limiting fires.** Send 11 rapid `POST /submit` requests; the 11th should return `429`.

**Stop condition.** All four checks pass and a representative `GET /log` output is captured in the README.

---

## 6. API Surface

The contract every other piece of code implements. No auth in v1.

### `POST /submit`

Rate limited (limits in README).

Request:

```json
{ "text": "string, required", "creator_id": "string, optional" }
```

Response `200`:

```json
{
  "submission_id": "uuid",
  "verdict": "human | uncertain | ai",
  "confidence": 0.0,
  "label_text": "string, the exact transparency label per §3",
  "signals": { "stylometric": 0.0, "llm": 0.0 }
}
```

Errors: `400` (missing/empty text), `429` (rate limit), `500` (Groq unavailable triggers fallback, not 500).

### `POST /appeal`

Request:

```json
{ "submission_id": "uuid, required", "reasoning": "string, required" }
```

Response `200`:

```json
{ "appeal_id": "uuid", "submission_id": "uuid", "status": "under_review" }
```

Errors: `404` (submission not found), `400` (missing fields).

### `GET /log`

No request body. Response is an array of audit log entries, newest-first.

### `GET /submission/<id>`

No request body. Returns the full decision record for one submission, or `404`.

---

## Checkpoint (Milestone 2)

- [x] §1 answers what each signal measures, output shape, and how they combine.
- [x] §2 defines what `0.6` means, how scores map, and the three threshold ranges.
- [x] §3 contains verbatim text for all three label variants.
- [x] §4 defines who can appeal, what they provide, what the system does, and what a reviewer would see.
- [x] §5 names four specific edge cases (spec required two).
- [x] `## Architecture` has the M1 diagram and a 2–3 sentence narrative.
- [x] `## AI Tool Plan` covers M3/M4/M5 with sections, requests, and verification steps.

Ready to move to Milestone 3 (implementation).
