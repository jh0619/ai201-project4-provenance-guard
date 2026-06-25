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

## Edge cases the system handles poorly

These are spelled out fully in [`planning.md`](./planning.md) §5. Brief summary:

1. **Short content** (< 30 words, < 3 sentences). Stylometric burstiness is statistical noise on small samples; the signal returns a `warning` field and falls back to TTR-only.
2. **Heavily-revised human writing with uniform style.** A polished human essay can look "AI-uniform" to stylometry; the asymmetric `likely_ai` threshold (`>0.70`) catches most of these in `uncertain` rather than mislabeling them as AI.
3. **Fluent non-native English writing.** The LLM classifier has a documented bias here — L2 English often has uniform structure that resembles AI output. The 0.4 weight on stylometric (which is L2-neutral) partly dampens this.
4. **AI-assisted but human-edited writing.** Neither verdict is "correct" for genuinely mixed authorship; the `uncertain` band is the honest answer.

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
