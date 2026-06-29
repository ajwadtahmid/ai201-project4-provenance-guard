# Provenance Guard — Planning Document

## Architecture Narrative (M1)

A single piece of text travels through the system as follows:

1. **Ingestion** — A creator POSTs to `/submit` with a `text` field and a `creator_id`. The endpoint validates that both are present and non-empty, then generates a `content_id` (UUID) to tag this submission for the rest of its life.

2. **Signal 1 — LLM Classification** — The raw text is sent to the Groq API with a structured prompt asking the model to assess whether the writing reads as human-authored or AI-generated, and to return a score between 0.0 and 1.0 (where 1.0 = high confidence AI). This signal captures semantic and stylistic coherence holistically — things like formulaic transitions, perfectly balanced arguments, and unnaturally smooth flow.

3. **Signal 2 — Stylometric Heuristics** — In parallel (conceptually), the text is analyzed using pure Python to compute three statistical properties: sentence-length standard deviation, type-token ratio, and informal-punctuation density. These three sub-metrics are normalized and averaged into a single `stylo_score` between 0.0 and 1.0. This signal captures structural uniformity — properties that are invisible to the LLM but differ reliably between human and AI writing.

4. **Confidence Scoring** — The two signal scores are combined into a single `confidence` value using a weighted average: `confidence = 0.6 * llm_score + 0.4 * stylo_score`. The LLM signal gets more weight because it captures semantic context that statistics alone cannot. The combined score sits on a 0.0–1.0 scale where higher means "more likely AI-generated."

5. **Transparency Label** — The confidence score is passed to a label-generation function that maps score ranges to one of three human-readable label variants (see Label Design section). The label text is included in the API response — it is what a platform would display to a reader.

6. **Audit Log** — Before returning a response, a structured JSON entry is written to a SQLite database recording: `content_id`, `creator_id`, `timestamp`, `attribution` result, `confidence` score, both individual signal scores (`llm_score`, `stylo_score`), and `status = "classified"`.

7. **Response** — The endpoint returns a JSON object containing `content_id`, `attribution`, `confidence`, `label`, `llm_score`, and `stylo_score`.

**Appeal path:** A creator POSTs to `/appeal` with their `content_id` and `creator_reasoning`. The system looks up the record in SQLite, updates its `status` to `"under_review"`, appends an appeal entry to the audit log (with the reasoning and timestamp), and returns a confirmation. No automated re-classification happens — a human reviewer would use `GET /log` to inspect the queue.

---

## False Positive Analysis (M1)

A false positive here means labeling a human writer's work as AI-generated — the more harmful error.

**Scenario:** A non-native English speaker submits a personal essay. They write in formal, grammatically careful prose with consistent paragraph structure. Signal 1 (LLM) scores this 0.72 because it reads "too clean." Signal 2 (stylometrics) scores it 0.58 because sentence lengths are unusually uniform. Combined confidence: ~0.66, which crosses the 0.65 threshold into "Likely AI-generated."

**How the system handles this:**
- The confidence is 0.66 — barely above the threshold. The label will say something like "Our system found indicators suggesting AI generation, but confidence is not high." (see label variants)
- Because the threshold is 0.65 (not 0.5), the system already requires stronger evidence than a coin flip to accuse.
- The creator sees the label and submits an appeal with their reasoning. The system immediately marks the content "under review" and logs the appeal.
- A human reviewer can inspect both signal scores to understand *why* the system flagged it and make a final call.

The wide "uncertain" band (0.35–0.65) is the primary defense — only the clearest cases get a definitive label. The appeals workflow is the safety valve for the rest.

---

## API Surface (M1)

| Method | Endpoint  | Accepts                              | Returns                                                      |
|--------|-----------|--------------------------------------|--------------------------------------------------------------|
| POST   | /submit   | `{text, creator_id}`                 | `{content_id, attribution, confidence, label, llm_score, stylo_score}` |
| POST   | /appeal   | `{content_id, creator_reasoning}`    | `{message, content_id, status}`                              |
| GET    | /log      | query param `?limit=N` (default 20)  | `{entries: [...]}`                                           |

---

## Architecture

### Submission Flow

```
POST /submit
  { text, creator_id }
        |
        v
 [Input Validation]
   - text present & non-empty
   - creator_id present
   - generate content_id (UUID)
        |
        +-----------------------------+
        |                             |
        v                             v
[Signal 1: LLM via Groq]    [Signal 2: Stylometrics]
  Prompt: "Score 0-1         Metrics:
  how AI-generated           - sentence length std dev
  is this text?"             - type-token ratio
  Returns: llm_score         - informal punctuation density
  (float 0.0-1.0)            Combined -> stylo_score
                             (float 0.0-1.0)
        |                             |
        +-----------------------------+
                    |
                    v
         [Confidence Scoring]
         confidence = 0.6 * llm_score
                    + 0.4 * stylo_score
                    |
                    v
        [Transparency Label Generator]
         confidence < 0.35  -> "Likely human-written"
         confidence 0.35-0.65 -> "Uncertain"
         confidence > 0.65  -> "Likely AI-generated"
                    |
                    v
           [Audit Log (SQLite)]
           writes: content_id, creator_id, timestamp,
                   attribution, confidence, llm_score,
                   stylo_score, status="classified"
                    |
                    v
           [JSON Response]
           { content_id, attribution, confidence,
             label, llm_score, stylo_score }
```

### Appeal Flow

```
POST /appeal
  { content_id, creator_reasoning }
        |
        v
  [Lookup content_id in SQLite]
   - 404 if not found
        |
        v
  [Update status -> "under_review"]
        |
        v
  [Audit Log: append appeal entry]
   writes: content_id, creator_reasoning,
           timestamp, event="appeal_filed",
           status="under_review"
        |
        v
  [JSON Response]
  { message: "Appeal received", content_id,
    status: "under_review" }
```

### Data passed between components

- `POST /submit` → Validation: raw text string + creator_id string
- Validation → Signal 1: raw text string
- Validation → Signal 2: raw text string
- Signal 1 → Scoring: llm_score (float 0.0–1.0)
- Signal 2 → Scoring: stylo_score (float 0.0–1.0)
- Scoring → Label generator: combined confidence (float 0.0–1.0)
- Label generator → Audit log + Response: label text string + attribution enum
- Audit log: persists all of the above under a single content_id

---

## Detection Signals (M2)

### Signal 1: LLM Classification via Groq

**What it measures:** Holistic semantic and stylistic coherence. The model is asked to assess whether the writing exhibits patterns characteristic of AI generation — formulaic transitions ("Furthermore," "It is important to note"), perfectly structured paragraphs, unnaturally balanced arguments, and absence of the tangents, hedges, and digressions that characterize human thought.

**Output:** A float between 0.0 and 1.0 returned as structured JSON from the Groq API. 1.0 means the model is highly confident the text is AI-generated. The prompt will instruct the model to return `{"ai_score": <float>}` and nothing else, so the output is reliably parseable.

**Blind spots:**
- Can be fooled by AI text that has been lightly edited to introduce casual phrasing or deliberate "imperfections."
- Formal human writing (academic papers, legal documents) may score high because it resembles AI's default register.
- The model's training data includes AI-generated text, so its internal "AI vs human" boundary may shift as AI writing styles evolve.
- Short texts (< 50 words) give the model too little signal — scores will be unreliable.

### Signal 2: Stylometric Heuristics

**What it measures:** Three statistical properties of text structure that differ reliably between human and AI writing:

1. **Sentence length standard deviation** — AI writing tends to produce uniformly structured sentences. Human writing has more variance — mixing short punchy sentences with long, winding ones. A low std dev suggests AI; a high std dev suggests human. Normalized: `stylo_sld = max(0, 1 - (std_dev / 15))` where 15 words of std dev is treated as the "clearly human" threshold.

2. **Type-token ratio (TTR)** — `unique_words / total_words`. AI tends toward moderate-to-high TTR because it draws from a broad vocabulary consistently. Very low TTR (repetitive casual writing) is human; very high TTR in a formal, smooth text suggests AI. Normalized: `stylo_ttr = min(1.0, max(0, (ttr - 0.5) / 0.4))` — maps the range 0.5–0.9 onto 0.0–1.0.

3. **Informal punctuation density** — count of `!`, `?` (beyond sentence-ending), `...`, `—`, `(`, `)` divided by word count. Human writing uses more of these; AI writing favors clean, formal punctuation. Normalized: `stylo_ipd = max(0, 1 - (informal_punct_count / words * 20))`.

**Combined stylo_score:** unweighted average of the three normalized sub-scores.

**M4 spec divergence — TTR replaced with average word length:** During implementation, dry-run testing on the four guide sample texts showed that type-token ratio is not a reliable discriminator: casual human text naturally has high lexical diversity (TTR ≈ 0.875), producing a false AI signal indistinguishable from formal AI text. Average word length proved a stronger structural discriminator — casual human writing averages ≈ 4.2 chars/word while AI-generated text averages ≈ 6.2 chars/word. The normalized range [3, 8] chars maps to [0.0, 1.0] and drops the clearly-human stylometric score from 0.65 to 0.42, preserving its correct classification after combining with the LLM signal.

**Output:** A float between 0.0 and 1.0.

**Blind spots:**
- Academic and professional human writing is structurally similar to AI output — uniform sentences, formal vocabulary, minimal informal punctuation. This signal will over-flag it.
- Very short texts (< 3 sentences) produce unreliable sentence-length variance.
- AI outputs generated with a "casual tone" prompt can have high stylometric variance.
- Non-English text or text with heavy code blocks will distort all three metrics.

### Combining the signals

```
confidence = 0.6 * llm_score + 0.4 * stylo_score
```

The LLM gets 60% weight because semantic context is more informative than structural statistics for most text types. The stylometric signal acts as a structural cross-check — if the LLM says "human" but the text has near-zero sentence variance, the combined score will still be elevated.

---

## Uncertainty Representation (M2)

**What a confidence score means:**

- `0.0` — system is maximally confident the content is human-written
- `0.5` — system genuinely cannot tell; signals are ambiguous or contradictory
- `1.0` — system is maximally confident the content is AI-generated

A score of `0.6` means: the system leans toward AI-generated, but not strongly. Both signals are pointing in the same direction but neither is definitive. The label at this score will acknowledge the lean without issuing a verdict.

**Thresholds:**

| Range        | Attribution     | Label variant          |
|--------------|-----------------|------------------------|
| < 0.35       | `likely_human`  | High-confidence human  |
| 0.35 – 0.65  | `uncertain`     | Uncertain              |
| > 0.65       | `likely_ai`     | High-confidence AI     |

**Why these thresholds?**

The thresholds are asymmetric in favor of human classification: the system needs a 0.65 score to call something AI-generated, but only a 0.35 score to call it human-written. This is a deliberate design decision reflecting the false-positive asymmetry — being wrong about AI is worse than being wrong about human. The uncertain band (0.35–0.65) is wide by design, covering 30 points of the scale. This pushes borderline cases into transparency rather than a potentially wrong verdict.

**Calibration approach:** After implementation, I'll test the scoring on the four sample texts provided in the project guide (clearly AI, clearly human, two borderlines) and verify that:
- Clearly AI text scores > 0.70
- Clearly human (casual) text scores < 0.25
- Formal human writing scores in the 0.40–0.65 range (uncertain)
- Lightly edited AI output scores in the 0.50–0.70 range

If clearly AI text scores below 0.65, I'll adjust the LLM prompt to be more discriminating. If casual human text scores above 0.35, I'll check the stylometric normalization constants.

---

## Transparency Label Design (M2)

All three label variants are written below. The `{confidence_pct}` placeholder will be filled with the score rendered as a percentage (e.g., `78%`).

### Variant 1: High-confidence AI (`confidence > 0.65`)

```
AI-Generated Content ({confidence_pct} confidence)

Our system's analysis found strong indicators that this content was likely
generated by an AI tool rather than written by the author. This label is
shown transparently so readers can weigh the content accordingly.

If you are the creator and believe this classification is wrong, you have
the right to appeal. Appeals are reviewed by a human moderator.
```

### Variant 2: High-confidence Human (`confidence < 0.35`)

```
Human-Created Content ({confidence_pct} confidence)

Our system's analysis found strong indicators that this content was written
by a human author. Thank you for sharing original work.

Note: No automated system is perfect. This label reflects our best
assessment, not a guarantee.
```

### Variant 3: Uncertain (`confidence 0.35–0.65`)

```
Origin Unclear ({confidence_pct} confidence)

Our system could not confidently determine whether this content was written
by a human or generated by an AI tool. It may contain a mix of both, or
it may fall outside the patterns our system was trained to recognize.

This content is published as-is. If you are the creator, you may submit
a statement of origin to clarify the record.
```

**Design rationale:** The label names the score as a percentage so non-technical readers can see that "78% confidence" is different from "52% confidence." The uncertain label avoids accusation — it explains the ambiguity rather than implying wrongdoing. The AI label explicitly mentions appeal rights because that is the most important information a falsely-flagged creator needs.

---

## Appeals Workflow (M2)

**Who can appeal:** Any creator whose `creator_id` is associated with a `content_id`. In the current implementation there is no authentication, so any caller who knows the `content_id` can file an appeal — this is acceptable for the project scope.

**What information they provide:**
- `content_id` (required) — identifies the submission being contested
- `creator_reasoning` (required) — free-text explanation of why the classification is wrong (e.g., "I wrote this myself as a non-native English speaker; formal style reflects my background")

**What the system does when an appeal is received:**
1. Validates that `content_id` exists in the database; returns 404 if not.
2. Updates the submission record's `status` from `"classified"` to `"under_review"`.
3. Appends a new audit log entry with: `content_id`, `creator_reasoning`, `timestamp`, `event = "appeal_filed"`, `status = "under_review"`.
4. Returns a JSON confirmation: `{"message": "Appeal received and is under review", "content_id": "...", "status": "under_review"}`.

**What a human reviewer sees when they open the appeal queue (`GET /log`):**
- The original classification entry: timestamp, attribution, confidence, both signal scores, status before appeal
- The appeal entry: timestamp, creator reasoning, updated status
- They can compare signal scores to understand why the system flagged it and use the creator's reasoning to make a judgment call

Automated re-classification is not implemented — re-classification could compound the error and would require a secondary review mechanism not in scope.

---

## Anticipated Edge Cases (M2)

### Edge Case 1: Formal human writing misclassified as AI

A law professor submits a blog post analyzing recent case law. The prose is structurally consistent — each paragraph makes a point, provides evidence, and concludes — with formal vocabulary and almost no informal punctuation. Sentence lengths are uniform because academic training encourages that style.

**Why the system will struggle:** Signal 2 (stylometrics) will score this high — low sentence variance, moderate-to-high TTR, minimal informal punctuation. Signal 1 (LLM) may also score it high because the semantic register matches AI's default output style. Combined score could easily land at 0.60–0.72, putting it in the "uncertain" or "likely AI" category.

**Mitigation:** The wide uncertain band (0.35–0.65) catches many of these cases before they become false positives. For the ones that cross 0.65, the appeal workflow is the intended resolution path.

### Edge Case 2: Short text (fewer than 3 sentences)

A creator submits a haiku or a two-sentence caption. With fewer than 3 sentences, sentence-length standard deviation is mathematically unreliable (std dev of 1–2 values is not meaningful). The LLM signal will also have very little text to analyze and may output a score near 0.5 by default.

**Why the system will struggle:** Both signals are starved of input. The stylometric signal's sentence-length sub-metric will be noise; the LLM prompt may explicitly hedge. The combined score will likely land near the center of the scale, triggering the "Uncertain" label regardless of actual origin.

**Mitigation:** The stylometric signal returns 0.5 (neutral) when text has fewer than 3 sentences or 10 words. Document this limitation in the README. The uncertain label is honest in this case — the system genuinely cannot tell.

### Edge Case 3: AI output with a deliberate "casual voice" prompt

An AI-generated piece produced by prompting the model to "write like a tired college student — use slang, typos, sentence fragments." The output may have high sentence-length variance and informal punctuation, fooling Signal 2. Signal 1 (the LLM) may also struggle because the semantic patterns of casual AI output are close to casual human output.

**Why the system will struggle:** Both signals trained on "standard" AI output patterns will underperform on adversarially styled AI content. The combined score may land in the uncertain range or even below 0.35.

**Mitigation:** Acknowledge in the README that the system is not adversarially robust. The transparency label for uncertain cases does not accuse — the worst outcome is a missed detection, which is the less harmful error direction.

---

## Stretch Feature: Analytics Dashboard

**What it shows:**
- **Detection patterns** — count and percentage of submissions classified as `likely_ai`, `likely_human`, and `uncertain`.
- **Appeal rate** — number of appeals filed divided by total submissions, expressed as a percentage.
- **Calibration metric (additional)** — average confidence score, LLM score, and stylometric score broken down by attribution category. This metric shows whether the scoring system is internally consistent: `likely_ai` submissions should average higher confidence than `uncertain` ones, which should average higher than `likely_human` ones. A well-calibrated system will show this ordering.

**Implementation plan:**
- Add `get_analytics()` to `db.py` — three SQL queries against the existing `submissions` and `audit_log` tables. No schema changes required.
- Add `GET /analytics` route to `app.py` — one line calling `get_analytics()`.
- Document in README under a stretch features section.

**Verification:**
- Run server, submit at least one of each attribution type, file an appeal, then call `GET /analytics`.
- Confirm the distribution counts match the known submissions.
- Confirm appeal rate is non-zero after filing an appeal.
- Confirm calibration averages show the expected ordering (AI > uncertain > human).

---

## AI Tool Plan (M2)

### M3: Submission endpoint + first detection signal

**Spec sections to provide:** Detection Signals (Signal 1 only), Architecture Diagram (submission flow), API Surface table.

**What to ask the AI to generate:**
1. Flask app skeleton with `POST /submit` route stub that validates input and returns a hardcoded response
2. A standalone `classify_with_llm(text)` function that calls the Groq API with a structured prompt and returns `llm_score` as a float

**How to verify the output:**
- Call `classify_with_llm()` directly with the four sample texts from the project guide and inspect the raw scores before wiring into the endpoint
- Confirm the function returns a float, not a string or dict
- Test the `/submit` route with `curl` and verify the JSON shape matches the API contract

### M4: Second signal + confidence scoring

**Spec sections to provide:** Detection Signals (Signal 2 + combination formula), Uncertainty Representation (thresholds), Architecture Diagram.

**What to ask the AI to generate:**
1. A standalone `compute_stylometrics(text)` function that computes the three sub-metrics and returns `stylo_score` as a float
2. A `compute_confidence(llm_score, stylo_score)` function and an `attribution_from_confidence(confidence)` function that applies the threshold logic

**What to check:**
- Run all four sample texts through both signals separately and print individual scores — verify they diverge in the expected direction
- Confirm the combined score for "clearly AI" text is > 0.65 and for "clearly casual human" text is < 0.35
- Check that the borderline texts land in the 0.35–0.65 uncertain range

### M5: Production layer

**Spec sections to provide:** Transparency Label Design (all three variants with exact text), Appeals Workflow, Architecture Diagram (both flows), Rate Limiting setup note from project guide.

**What to ask the AI to generate:**
1. A `generate_label(attribution, confidence)` function that returns the correct label text for each of the three variants
2. The `POST /appeal` endpoint and the SQLite update + audit-log append logic
3. Flask-Limiter configuration applied to `/submit`

**How to verify:**
- Submit inputs that produce each of the three confidence ranges and confirm the label text matches the spec exactly
- Test the appeal endpoint with a known `content_id` and verify `GET /log` shows `"status": "under_review"` and the `creator_reasoning` field
- Run the 12-request loop from the project guide and confirm the first 10 return 200 and the remaining return 429
