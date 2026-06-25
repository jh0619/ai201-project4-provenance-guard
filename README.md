# Provenance Guard

A backend system that classifies text as human-written or AI-generated, returns a calibrated confidence score, displays a transparency label to readers, and lets creators appeal the result. Built for AI 201, Project 4.

The full design rationale lives in [`planning.md`](./planning.md). This README is the operator-facing record: how to run it, what the labels look like, what the rate limits are, and what the audit log captures.

---

## Setup

```bash
git clone <this repo>
cd ai201-project4-provenance-guard

python -m venv .venv
source .venv/bin/activate         # Mac/Linux
# .venv\Scripts\activate          # Windows
pip install -r requirements.txt

cp .env.example .env              # then edit .env and paste your GROQ_API_KEY

python app.py                     # starts on http://127.0.0.1:5000
```

---

## How detection works (one paragraph)

A submitted piece of text runs through two independent signals — **stylometric heuristics** (a pure-Python statistical reading of sentence-length variance, type-token ratio, and punctuation density) and an **LLM classifier** (Groq's `llama-3.3-70b-versatile` asked for a holistic 0–1 verdict). Each signal returns a score in `[0, 1]` where 0 means "looks human" and 1 means "looks AI." The two scores are combined with a fixed weighting — `combined = 0.4·stylometric + 0.6·llm` — and the result is mapped to one of three verdict tiers using asymmetric thresholds (see "Confidence scoring" below). Every step is written to a SQLite audit log. If Groq is unavailable, the system falls back to stylometric-only and forces the verdict to `uncertain` — a single signal isn't trustworthy enough to make a confident call.

---

## API endpoints

| Method | Path                       | Purpose                                                              |
| ------ | -------------------------- | -------------------------------------------------------------------- |
| `POST` | `/submit`                  | Run the detection pipeline on a piece of text. Rate-limited.         |
| `POST` | `/appeal`                  | Creator contests a verdict; logs reasoning, status → `under_review`. |
| `GET`  | `/log`                     | Audit log, newest first.                                             |
| `GET`  | `/submission/<content_id>` | Fetch one decision record (helper for reviewer UIs).                 |

### `POST /submit`

Request:

```json
{ "text": "string, required", "creator_id": "string, optional" }
```

Response `200`:

```json
{
  "content_id": "uuid",
  "creator_id": "diner-1",
  "timestamp": "ISO-8601 UTC",
  "attribution": "likely_human | uncertain | likely_ai",
  "confidence": 0.102,
  "label": "verbatim multi-line text — see Transparency Label section",
  "signals": {
    "stylometric": { "score": 0.104, "features": { ... }, "warning": null },
    "llm":         { "score": 0.10,  "rationale": "...", "error": null }
  },
  "status": "classified"
}
```

Errors: `400` (missing/empty text), `429` (rate limit), `404` (only used by `/submission/<id>`).

### `POST /appeal`

Request:

```json
{ "content_id": "uuid from a prior /submit", "creator_reasoning": "free text" }
```

Response `200`:

```json
{
  "appeal_id": "uuid",
  "content_id": "uuid",
  "status": "under_review",
  "timestamp": "ISO-8601 UTC",
  "message": "Appeal received and logged. A human reviewer will follow up."
}
```

The original decision row is **not modified** — only its `status` field flips from `classified` to `under_review`, and a separate `appeal` row is inserted into the audit log carrying `appeal_reasoning`. Original verdict, scores, and label are preserved.

---

## Confidence scoring

Two signals are combined with a fixed weighting, then mapped to a verdict tier using asymmetric thresholds. The asymmetry is intentional: a false positive (calling a human's writing AI) is more harmful on a writing platform than a false negative, so the AI threshold is much higher than the human threshold.

```
combined = 0.4 * stylometric_score + 0.6 * llm_score
```

| `combined` range | Verdict tier   |
| ---------------- | -------------- |
| `≤ 0.25`         | `likely_human` |
| `0.25 – 0.70`    | `uncertain`    |
| `> 0.70`         | `likely_ai`    |

**What `combined = 0.6` means.** _Not_ "60% probability of AI." It is a weighted aggregate score on the same `[0, 1]` scale as the two signals. `0.6` means the signals lean toward AI but not strongly — and importantly, it falls in the `uncertain` band, so the user-facing label communicates that uncertainty honestly.

**Calibration note.** The AI threshold was originally `>0.80` in `planning.md`. During Milestone 4 calibration testing on five deliberately chosen inputs, that bar proved unreachable for realistic two-signal outputs — even strongly AI-looking text with `stylometric=0.39` and a confident `llm=0.95` only reached `combined=0.728`. Lowering to `>0.70` keeps the asymmetry intact (the AI side is still ~3× the human side at 0.25) while making the `likely_ai` verdict attainable on clearly-AI content. The full calibration log is in [`tests/calibration_samples.md`](./tests/calibration_samples.md).

### Worked examples — two submissions, different confidence levels

The combined score is designed to vary meaningfully across inputs, not flip at a single threshold. Two real cases from M4 calibration:

**Case A — high-confidence `likely_human`** (a casual, varied-rhythm restaurant review):

> Input (37 words, 4 sentences): _"ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium and i was thirsty for like three hours after. my friend got the spicy version. probably wont go back unless someone drags me there"_
>
> - `stylometric_score`: **0.038** _(burstiness=7.4 — high variance, very human-typical)_
> - `llm_score`: **0.10** _("LOVE IT"-style emotional all-caps + idiosyncratic punctuation read as human")_
> - `combined`: **0.075** → `likely_human`
> - Label: _"Likely written by a human. ... AI-likelihood score: 8% (low confidence in AI)"_

This sits well inside the human band (`≤ 0.25`). Both signals agreed strongly; nothing is borderline.

**Case B — lower-confidence `uncertain`** (formal but plausibly-human academic prose):

> Input (43 words, 3 sentences): _"Furthermore, the relationship between monetary policy and asset price inflation has been extensively studied. Central banks face a leverage tension between price stability and the unintended consequences of prolonged low interest rates on equity valuations. Innovative central bank tools have emerged in response."_
>
> - `stylometric_score`: **0.243** _(burstiness 5.31; uniform structure but TTR still high)_
> - `llm_score`: **0.55** _(words like "Furthermore" and "leverage" triggered partial AI signal)_
> - `combined`: **0.427** → `uncertain`
> - Label: _"Inconclusive. Our detector found mixed signals... AI-likelihood score: 43%"_

This sits squarely in the middle of the `uncertain` band. The system is honestly saying "we don't know" rather than risking a false-positive AI accusation against a human writing in a formal register.

**What this demonstrates.** The spread between `0.075` and `0.427` is ~0.35 of the total range, and the two cases land in different verdict tiers with materially different labels. The confidence score is doing real work — not flipping at 0.5, and not constant across inputs.

---

## Transparency label — three variants

All three are produced by `label_generator.make_label()`. `{N}` is `round(combined_score * 100)`.

### Variant 1 — `likely_human` (combined ≤ 0.25)

> Likely written by a human.
> Our detector found the variation in sentence rhythm and word choice that is typical of human writing.
> AI-likelihood score: {N}% (low confidence in AI)

### Variant 2 — `uncertain` (0.25 < combined ≤ 0.70)

> Inconclusive.
> Our detector found mixed signals — some patterns look human, others look AI-generated. We are not confident either way; treat this result as undetermined.
> AI-likelihood score: {N}%
> If you are the creator and believe a clearer determination should have been made, you can appeal.

### Variant 3 — `likely_ai` (combined > 0.70)

> Likely AI-generated.
> Our detector found strong signals — uniform sentence structure, generic phrasing — that suggest this content was produced by an AI model.
> AI-likelihood score: {N}% (high)
> Detection is not perfect. If you are the creator and wrote this yourself, you can appeal this label.

### Design choices

- **"AI-likelihood score"** rather than "confidence" — `confidence` could be misread as "confidence in the verdict" or "confidence that the content is real." `AI-likelihood` is unambiguous.
- **All three variants show the same numeric score**, so a curious reader can interpret the score consistently across labels.
- **Only `uncertain` and `likely_ai` mention appeal.** The `likely_human` variant has no appeal CTA because there's no harm to recover from.
- **`likely_ai` explicitly says "Detection is not perfect."** This bakes the spec's false-positive-asymmetry hint directly into the user-facing text.

---

## Appeals workflow

The creator of contested content can submit an appeal via `POST /appeal` with the original `content_id` and free-text `creator_reasoning`. On receipt the system:

1. Looks up the original decision; returns `404` if the `content_id` is unknown.
2. Updates the decision row's `status` field from `classified` to `under_review`.
3. Inserts a new audit log row of `entry_type = "appeal"` linked to the same `content_id`, carrying the appeal reasoning, a new `appeal_id`, and a UTC timestamp.
4. Returns `200` with the appeal ID and the new status.

**Automated re-classification is intentionally not implemented.** Per `planning.md` §4, the right place for a human reviewer is at the `under_review` queue — a downstream interface (not built in v1) would let a reviewer see the original text, the verdict, both signal scores, the rationale, and the appeal reasoning side-by-side, and either uphold or overturn.

---

## Rate limiting

The `/submit` endpoint is rate-limited by Flask-Limiter with `storage_uri="memory://"`:

```python
@limiter.limit("10 per minute; 100 per day")
def submit():
    ...
```

### Justification of the chosen limits

- **10 per minute.** A real human creator submitting their own work submits _occasionally_ — a poem, a draft, a finished essay. Not 10 times per minute. The minute-window limit exists to defeat scripted abuse, not legitimate usage. A creator pasting a piece, getting a result, reading the label, and (if needed) drafting an appeal naturally takes well over 60 seconds per cycle.
- **100 per day.** Even a heavy power user is unlikely to clear 100 distinct submissions in a day. This is the slower-burn ceiling that catches sustained low-volume scraping ("submit one piece per minute for an hour to map the model's behavior").
- **Per-IP keying.** `get_remote_address` is the default. In a production deployment the key would be the authenticated creator ID; in this prototype IP is the only identifier available.
- **In-memory storage.** Limits reset on server restart. That's appropriate for a dev/grader prototype; a real deployment would use Redis.

### Evidence of rate-limiting in action

The spec asks for proof. The block below is captured output from sending 12 rapid `POST /submit` requests; the 11th and 12th return `429` as expected:

```
Sending 12 rapid requests (limit is 10/min)...
  request # 1: HTTP 200
  request # 2: HTTP 200
  request # 3: HTTP 200
  request # 4: HTTP 200
  request # 5: HTTP 200
  request # 6: HTTP 200
  request # 7: HTTP 200
  request # 8: HTTP 200
  request # 9: HTTP 200
  request #10: HTTP 200
  request #11: HTTP 429
  request #12: HTTP 429

Summary: 10x 200, 2x 429
```

A `429` body looks like:

```json
{
  "error": "rate limit exceeded",
  "detail": "10 per 1 minute"
}
```

---

## Audit log

SQLite-backed, single table `audit_log`. Every `/submit` writes a row of `entry_type = "decision"`; every `/appeal` writes a row of `entry_type = "appeal"` and updates the matching decision row's `status`.

### Columns (per decision row)

| Column              | Purpose                                                       |
| ------------------- | ------------------------------------------------------------- |
| `id`                | Auto-incrementing primary key                                 |
| `entry_type`        | `"decision"` or `"appeal"`                                    |
| `content_id`        | UUID; ties appeal rows back to the original decision          |
| `creator_id`        | Caller-supplied identifier (defaults to `"anonymous"`)        |
| `timestamp`         | ISO-8601 UTC                                                  |
| `attribution`       | `likely_human` / `uncertain` / `likely_ai`                    |
| `confidence`        | Combined score in `[0, 1]`                                    |
| `stylometric_score` | Signal 1 raw output                                           |
| `llm_score`         | Signal 2 raw output (`NULL` if Groq was unavailable)          |
| `llm_rationale`     | LLM's one-sentence justification                              |
| `features_json`     | Raw stylometric features as JSON                              |
| `label`             | The exact transparency label text returned to the client      |
| `status`            | `"classified"` initially; flips to `"under_review"` on appeal |
| `appeal_id`         | Set only on appeal rows                                       |
| `appeal_reasoning`  | Set only on appeal rows                                       |

### Sample output from `GET /log` after 3 submissions and 1 appeal

```
count: 4

  [appeal]   15b8a6e5.. status=under_review  reasoning=I wrote this myself. English is my second language...
  [decision] 0e113090.. status=classified    attr=uncertain     conf=0.427 styl=0.243 llm=0.55
  [decision] 6ca60dd3.. status=classified    attr=likely_ai     conf=0.719 styl=0.372 llm=0.95
  [decision] 15b8a6e5.. status=under_review  attr=likely_human  conf=0.102 styl=0.104 llm=0.1
```

Note that the bottom row (`15b8a6e5..`) has `status=under_review` because the appeal at the top references the same `content_id` — exactly the link the M5 spec asks the grader to verify.

A full entry in raw JSON looks like:

```json
{
  "id": 1,
  "entry_type": "decision",
  "content_id": "15b8a6e5-858b-4e94-9e01-da337e8e2ae0",
  "creator_id": "diner-1",
  "timestamp": "2026-06-25T21:38:10.860414+00:00",
  "attribution": "likely_human",
  "confidence": 0.102,
  "stylometric_score": 0.104,
  "llm_score": 0.1,
  "llm_rationale": "...",
  "features": {
    "burstiness": 7.4,
    "ttr": 0.919,
    "punct_density": 0.015,
    "avg_sent_len": 9.2,
    "word_count": 37,
    "sentence_count": 4
  },
  "label": "Likely written by a human.\nOur detector found the variation in sentence rhythm and word choice that is typical of human writing.\nAI-likelihood score: 10% (low confidence in AI)",
  "status": "under_review"
}
```

---

## Known limitations

### The specific failure case: fluent non-native English writing

This is the most important content type to call out because the failure mode is in _both_ detection signals at the same time, which means signal weighting cannot fully neutralize it.

- **Stylometric weakness.** L2 English writers often produce uniformly-structured prose. Uniform syntax is the survival strategy of a second language — you stay with what you know works. Result: low burstiness, regular sentence length, consistent vocabulary. The analyzer reads this as "AI-uniform" because that's exactly the surface pattern it was designed to detect.
- **LLM-classifier weakness.** This is the more documented and more serious bias. Llama-family classifiers (and most other AI detectors) consistently rate fluent-but-stilted L2 writing higher on AI-likelihood than equivalent native writing. Both share the same surface features the classifier was trained to associate with AI output.

Because **both signals are biased in the same direction** on this content type, no weighting tweak fixes the underlying problem. The mitigations the system has:

- The 0.4 stylometric weight is partly there to dampen the LLM bias — stylometric measures _structure_, not _feel_, so its bias is at least independent in mechanism even if directionally similar.
- The asymmetric `> 0.70` AI threshold catches most L2 borderline cases in `uncertain` rather than `likely_ai`. The user-facing harm is "your work was marked inconclusive" rather than "your work was marked AI" — a much smaller insult.
- The appeal workflow is the explicit recovery path. The `uncertain` and `likely_ai` labels both surface "you can appeal" as a CTA so the L2 writer has a documented way to push back.

**What I'd change for a real deployment.** Calibrate the LLM signal against a labeled L2 English corpus and apply a per-language-background scaling factor. This requires data we don't have in v1, so the appeal flow is the v1 answer. Naming this limitation honestly is itself part of the design: a system that pretends to be neutral when it isn't is more harmful than one that documents its biases.

### Other content the system handles poorly

- **Short content** (< 30 words, < 3 sentences). Stylometric burstiness is noise on small samples; the analyzer falls back to TTR-only and attaches a `warning` field to the response. Verdict usually defaults to `uncertain` because the signal is too weak to claim either side.
- **Heavily-revised human writing.** Polished prose flattens variance. Lands in `uncertain` rather than `likely_ai` by design — the asymmetric threshold catches it.
- **AI-assisted but human-edited writing.** Genuinely mixed authorship; neither verdict is "correct." The `uncertain` label is the honest answer; v1 is not designed to identify partial authorship.

---

## Spec reflection

**One way the spec helped my implementation.** The requirement to write all three transparency-label variants _verbatim_ in `planning.md` before any coding caught a real UX problem early. My first drafts used "confidence: 87%" in the label text — but "confidence" was ambiguous (confidence in the verdict? confidence the content is real?). Writing out the actual reader-facing string forced me to feel the ambiguity. I switched to "AI-likelihood score" in the spec; the implementation followed without rework. Without the spec's "write the literal text" requirement, I would have caught this only in M5 when I was already wiring up `label_generator.py` — much more expensive to fix.

**One way my implementation diverged from the spec.** The original `planning.md` had `THRESHOLD_AI > 0.80` as the bar for the `likely_ai` verdict. During M4 calibration on five deliberately chosen inputs, that threshold proved unreachable for realistic two-signal outputs — the most aggressively AI-style sample (long, uniform marketing-speak) only reached `combined = 0.728` even with a maximally confident LLM. I lowered `THRESHOLD_AI` to `>0.70`, updated `planning.md` and `confidence_scorer.py` in lockstep, and recorded the rationale in `tests/calibration_samples.md`. The asymmetry is preserved — the AI side is still ~3× the human side at 0.25 — and `likely_ai` is now actually attainable on clearly-AI content. This was a deliberate spec-driven recalibration off real test data, not a fudge to make tests pass.

---

## AI usage

Claude (Anthropic) was my primary AI tool. Three specific instances:

**1. Architecture and planning (M1–M2).** I directed Claude to walk through the spec and produce a `planning.md` answering the five required questions before writing any code. Claude generated the two-signal architecture, the asymmetric threshold design, the verbatim label variants, and the API surface. I pushed back on the threshold values — Claude's first pass had `> 0.80` for the AI verdict, which I accepted because it sounded reasonable. That decision came back to bite us in M4 when calibration showed it was unreachable. Lesson: AI-generated design decisions need to be validated against real data, not just inspected for plausibility.

**2. Stylometric debugging (M3).** I asked Claude to generate the stylometric module with burstiness + TTR. The initial implementation set `MIN_WORDS_FOR_FULL_SIGNAL = 50`, which meant most of the spec's 4 test inputs (39–55 words) fell back to TTR-only — producing scores clustered around 0.0 with no useful differentiation. I asked Claude to debug; we identified together that on short text, TTR is naturally high (less word repetition is possible) so it clips to zero contribution under our anchor of 0.65, leaving the score with no signal. I had Claude lower the word threshold to 30 while keeping `MIN_SENTENCES_FOR_VARIANCE = 3` as the real safety check — a fix that matched the actual statistical behavior, not arbitrary parameter tuning.

**3. LLM classifier defensive parsing (M4).** When generating `llm_classifier.py`, Claude proposed a three-stage JSON extractor: try a markdown code fence first, fall back to the outermost `{ ... }`, raise a typed `GroqUnavailable` exception on failure. I kept all three stages rather than simplifying because (a) the cost of an extra regex is negligible compared to a Groq round-trip and (b) LLMs are inconsistent enough that paranoid parsing actually earns its keep. I also kept Claude's suggested `temperature=0.0` setting for audit-log reproducibility — without it the same input could produce different scores on different runs, which would make the audit log misleading.

**What I overrode or didn't take.** Claude flagged an `f"..."` print statement with no placeholders as a lint warning; I left it as-is because the cleanup cost outweighed the benefit on a test file. Claude also suggested per-language calibration for the L2 English bias problem (see Known Limitations); I did not implement it because it requires labeled L2 data I don't have, and I documented the limitation honestly instead.

---

## Walkthrough

Recorded portfolio walkthrough: **[https://www.loom.com/share/54dd2eabda0d4b9c9e2365aaee6c4e16]**

The video covers, in roughly two minutes:

1. The two-signal architecture and why stylometric + LLM specifically — independent failure modes.
2. Live demo: submit clearly-human text → `likely_human` label; submit AI marketing-speak → `likely_ai` label.
3. Appeal flow: submit appeal on the previous `content_id`, then `GET /log` shows the original decision with `status=under_review` AND a new appeal row linked by `content_id`.
4. Three design decisions worth highlighting: false-positive asymmetry in the thresholds, audit-log immutability on appeal, LLM-failure fallback to `uncertain`.

---

## Testing

| Script                       | What it verifies                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------- |
| `python test_stylometric.py` | Signal 1 in isolation on 4 sample texts                                            |
| `python test_scoring.py`     | The scorer + threshold logic (11 cases)                                            |
| `python test_4_inputs.py`    | Full pipeline on the spec's 4 inputs + 1 calibration sample (needs `GROQ_API_KEY`) |
| `python test_m5_e2e.py`      | M5 end-to-end against a running server (3 submits + 1 appeal + log inspection)     |

---

## Stretch features

None completed for the v1 submission. The schema and architecture are designed to accept them without rework — for example, the audit log's `appeal_id` / `appeal_reasoning` columns and the `/submission/<id>` endpoint together give a reviewer dashboard most of its data already.
