# Provenance Guard

A backend API that classifies submitted text as human-written or AI-generated, scores confidence in that classification, surfaces a transparency label, and handles creator appeals. Built as a drop-in attribution layer for creative sharing platforms.

---

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# create .env with GROQ_API_KEY=your_key_here
python app.py
```

---

## Architecture

The full architecture diagram and narrative live in [planning.md](planning.md) under `## Architecture`. Brief summary of both flows:

**Submission flow:** `POST /submit` → input validation + UUID generation → LLM signal (Groq) + stylometric signal (pure Python) run in sequence → weighted confidence score → transparency label → SQLite audit log → JSON response.

**Appeal flow:** `POST /appeal` → content_id lookup → status update to `under_review` → audit log append → JSON confirmation.

Data passed between components: raw text goes into both signals independently; each returns a float 0–1; the scoring layer combines them; the label generator maps the combined score to one of three text variants; everything is persisted under a single `content_id`.

---

## API

### `POST /submit`

Accepts a piece of text for attribution analysis.

**Request:**
```json
{
  "text": "The text to classify",
  "creator_id": "creator-handle"
}
```

**Response:**
```json
{
  "content_id": "uuid",
  "attribution": "likely_ai | likely_human | uncertain",
  "confidence": 0.738,
  "label": "... full label text ...",
  "llm_score": 0.8,
  "stylo_score": 0.645
}
```

Rate limit: **10 requests per minute, 100 per day** per IP.

### `POST /appeal`

Lets a creator contest a classification.

**Request:**
```json
{
  "content_id": "uuid-from-submit",
  "creator_reasoning": "I wrote this myself..."
}
```

**Response:**
```json
{
  "message": "Appeal received and is under review",
  "content_id": "uuid",
  "status": "under_review"
}
```

### `GET /log?limit=N`

Returns the most recent N audit log entries (default 20).

---

## Detection Signals

### Signal 1 — LLM Classification (Groq)

A call to `llama-3.3-70b-versatile` via the Groq API with a structured prompt asking the model to assess whether the text reads as human-authored or AI-generated. The model returns `{"ai_score": <float>}` where 1.0 = high confidence AI. Temperature is set to 0.1 for consistency across repeated calls on the same text.

**What it captures:** Semantic and stylistic coherence holistically — formulaic transitions ("Furthermore," "It is important to note"), unnaturally balanced arguments, absence of personal voice or genuine digressions, generic phrasing that avoids specificity. These are patterns that are real but hard to quantify statistically.

**Why it's the dominant signal (60% weight):** Semantic context is more informative than structural statistics for most text types. A statistically-regular text can still be obviously human (formal academic writing); a statistically-irregular text can still read like AI with a casual-tone prompt. The LLM captures what the statistics miss.

**Blind spots:** Can be fooled by AI text that has been lightly edited to introduce casual phrasing. Formal human writing (academic papers, legal documents) may score high because it resembles AI's default register. Unreliable on very short texts (< 3 sentences).

### Signal 2 — Stylometric Heuristics (pure Python)

Three structural sub-metrics averaged equally into a single `stylo_score` (0–1, where 1 = likely AI):

**Sub-metric 1 — Sentence-length standard deviation:**
AI writing tends to produce sentences of consistent length; human writing mixes short and long. Std dev is normalized: `sld_score = max(0, 1 - std_dev / 15)` where 15 words of std dev maps to "clearly human." Low variance → high (AI) score.

**Sub-metric 2 — Average word length:**
AI writing defaults to longer, more formal vocabulary. Casual human writing uses shorter words. Normalized over the range [3, 8] chars: `wl_score = min(1, max(0, (avg_len - 3) / 5))`. A word average of 4.2 chars (casual human) scores 0.24; 6.2 chars (formal AI) scores 0.64.

**Sub-metric 3 — Informal expression density:**
Counts `!`, `?`, `...`, `—`, `--`, `(`, `)`, and all-caps words (e.g. `WAY`, `BREAKING`) divided by word count. AI writing avoids these; human writing uses them naturally. Normalized: `ipd_score = max(0, 1 - (informal_count / words) * 15)`. Zero informal markers → 1.0 (AI).

**Why these three sub-metrics are independent:** Sentence variance is structural rhythm. Average word length is lexical register. Informal expression density is punctuation and typographic style. They can agree (clear AI text scores high on all three) or disagree (a human writing in academic style has low sentence variance and long words, but the LLM signal corrects for this).

**Blind spots:** Academic human writing looks AI-like on all three metrics — this is the documented edge case. Very short texts (fewer than 3 sentences) make sentence-variance unreliable.

**Why TTR was replaced with average word length:** The spec originally listed type-token ratio as sub-metric 2. During dry-run testing, casual human text produced TTR ≈ 0.875 — indistinguishable from formal AI text (TTR ≈ 0.884). Average word length separates these clearly and is documented in `planning.md` as a spec divergence.

---

## Confidence Scoring

```
confidence = 0.6 × llm_score + 0.4 × stylo_score
```

| Range | Attribution | Meaning |
|---|---|---|
| confidence > 0.65 | `likely_ai` | Strong indicators of AI generation |
| 0.35 ≤ confidence ≤ 0.65 | `uncertain` | System cannot confidently classify |
| confidence < 0.35 | `likely_human` | Strong indicators of human authorship |

**Why asymmetric thresholds?** A false positive (calling a human's work AI) is worse than a false negative on a writing platform. The system requires a score above 0.65 — not merely above 0.5 — before issuing an AI verdict. The uncertain band is deliberately wide (30 points) to push borderline cases into transparency rather than a wrong verdict.

**What a score of 0.5 means:** Both signals point in conflicting directions, or neither has enough evidence to lean either way. The system genuinely cannot tell, and the label says so without making an accusation.

### Example submissions showing meaningful score variation

**High-confidence example — formulaic AI prose:**

> *"Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."*

```
llm_score:   0.80   (formulaic transitions, perfectly balanced structure)
stylo_score: 0.645  (uniform sentence lengths, long formal words, no informal punct)
confidence:  0.738  → likely_ai
```

**Lower-confidence example — casual human writing:**

> *"ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after..."*

```
llm_score:   0.00   (personal voice, specific anecdote, genuine frustration)
stylo_score: 0.418  (high sentence variance, short words, informal markers)
confidence:  0.167  → likely_human
```

**Uncertain example — mixed-register writing (signals disagree):**

> *"Remote work has genuinely improved my quality of life — fewer commutes, more flexibility, and the ability to structure my day around when I am most productive. On the other hand, the boundaries between work and home have blurred in ways I did not anticipate. Whether this trade-off is worth it probably depends on your living situation and role."*

```
llm_score:   0.20   (personal voice present, but structured and hedged conclusion)
stylo_score: 0.594  (moderate sentence uniformity, formal vocabulary, minimal informal punct)
confidence:  0.358  → uncertain
```

The two signals disagree here — the LLM reads this as mostly human (0.20) while the stylometrics see formal structure (0.594). The weighted average lands in the uncertain band (0.35–0.65), producing the "Origin Unclear" label rather than a potentially wrong verdict. This is the intended behavior: when signals conflict, the system acknowledges ambiguity instead of guessing.

The spread across all three examples — 0.738, 0.358, 0.167 — shows the scoring produces meaningful variation across the full range, not a binary flip near 0.5.

---

## Transparency Labels

All three label variants are written below exactly as they appear in API responses. The `{X% confidence}` placeholder is filled with the actual score.

### Variant 1 — High-confidence AI (`confidence > 0.65`)

```
AI-Generated Content (74% confidence)

Our system's analysis found strong indicators that this content was likely
generated by an AI tool rather than written by the author. This label is
shown transparently so readers can weigh the content accordingly.

If you are the creator and believe this classification is wrong, you have
the right to appeal. Appeals are reviewed by a human moderator.
```

### Variant 2 — High-confidence Human (`confidence < 0.35`)

```
Human-Created Content (17% confidence)

Our system's analysis found strong indicators that this content was written
by a human author. Thank you for sharing original work.

Note: No automated system is perfect. This label reflects our best
assessment, not a guarantee.
```

### Variant 3 — Uncertain (`0.35 ≤ confidence ≤ 0.65`)

```
Origin Unclear (36% confidence)

Our system could not confidently determine whether this content was written
by a human or generated by an AI tool. It may contain a mix of both, or
it may fall outside the patterns our system was trained to recognize.

This content is published as-is. If you are the creator, you may submit
a statement of origin to clarify the record.
```

**Design rationale:** The score is shown as a percentage so readers can see that 74% confidence is qualitatively different from 36%. The uncertain label avoids accusation — it names the ambiguity without implying wrongdoing. The AI label leads with appeal rights because that is the most important information for a falsely-flagged creator.

---

## Appeals Workflow

A creator submits `POST /appeal` with their `content_id` and a `creator_reasoning` string explaining why the classification is wrong. The system:

1. Validates the `content_id` exists; returns 404 if not
2. Updates the submission's `status` from `"classified"` to `"under_review"` in SQLite
3. Appends an `appeal_filed` event to the audit log with the reasoning and timestamp
4. Returns a confirmation response

A human reviewer uses `GET /log` to inspect the queue — they see the original classification entry (with both signal scores) and the appeal entry (with the creator's reasoning) side by side, giving them everything needed to make a judgment call. Automated re-classification is not implemented.

**Test appeal flow:**
```bash
# Submit content, note the content_id in the response
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "...", "creator_id": "alice"}'

# File an appeal with that content_id
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-ID-HERE", "creator_reasoning": "I wrote this myself..."}'

# Verify the appeal appears in the log
curl -s http://localhost:5000/log | python -m json.tool
```

---

## Rate Limiting

**Limits:** 10 requests per minute, 100 requests per day (per IP address).

**Reasoning:** A legitimate creator submitting their own work would rarely need to classify more than a handful of pieces in a single session — 10/minute is generous even for a writer iterating on drafts. The 100/day ceiling covers heavy but plausible use (e.g., a platform ingesting a day's backlog for one user) while blocking automated flooding. Together, these limits make it expensive to probe the system for adversarial bypasses — an attacker who wants to reverse-engineer the detection threshold by submitting thousands of variations would hit the daily cap after ~10 minutes of testing.

**Evidence (12 rapid requests, limit at 10/minute):**

```
200
200
200
200
200
200
200
200
200
200
429
429
```

---

## Audit Log

Every attribution decision and appeal is written to SQLite as a structured JSON event. Sample from `GET /log` (3 representative entries):

**Entry 1 — AI classification:**
```json
{
  "id": 1,
  "content_id": "5e5f8680-36eb-4a32-b42e-c4a17d2d932b",
  "event": "classification",
  "timestamp": "2026-06-28T22:41:10.757135+00:00",
  "data": {
    "creator_id": "m5-demo",
    "attribution": "likely_ai",
    "confidence": 0.738,
    "llm_score": 0.8,
    "stylo_score": 0.6451,
    "status": "classified"
  }
}
```

**Entry 2 — Human classification:**
```json
{
  "id": 2,
  "content_id": "c4441f58-e3e8-494d-8120-810b95a5928e",
  "event": "classification",
  "timestamp": "2026-06-28T22:41:18.855225+00:00",
  "data": {
    "creator_id": "m5-demo",
    "attribution": "likely_human",
    "confidence": 0.1673,
    "llm_score": 0.0,
    "stylo_score": 0.4183,
    "status": "classified"
  }
}
```

**Entry 3 — Appeal filed:**
```json
{
  "id": 4,
  "content_id": "5e5f8680-36eb-4a32-b42e-c4a17d2d932b",
  "event": "appeal_filed",
  "timestamp": "2026-06-28T22:42:41.761135+00:00",
  "data": {
    "creator_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
    "status": "under_review"
  }
}
```

---

## Stretch Feature: Analytics Dashboard

`GET /analytics` returns three categories of metrics over all submissions in the database.

**Detection patterns** — attribution distribution showing what fraction of submissions fall into each category:

```json
"attribution_distribution": {
  "likely_ai":    { "count": 1, "percentage": 33.3 },
  "likely_human": { "count": 1, "percentage": 33.3 },
  "uncertain":    { "count": 1, "percentage": 33.3 }
}
```

**Appeal rate** — raw count and percentage of submissions that received an appeal:

```json
"appeal_rate": {
  "total_appeals":   1,
  "rate_percentage": 33.3
}
```

**Average confidence by attribution (additional metric)** — shows whether the scoring system is internally calibrated. A well-behaved system will show `likely_ai` averaging higher confidence than `uncertain`, which averages higher than `likely_human`. This is a sanity check on the scoring, not just a summary:

```json
"avg_confidence_by_attribution": {
  "likely_ai":    { "avg_confidence": 0.738, "avg_llm_score": 0.8,  "avg_stylo_score": 0.645 },
  "uncertain":    { "avg_confidence": 0.358, "avg_llm_score": 0.2,  "avg_stylo_score": 0.594 },
  "likely_human": { "avg_confidence": 0.167, "avg_llm_score": 0.0,  "avg_stylo_score": 0.418 }
}
```

The ordering holds: 0.738 → 0.358 → 0.167, confirming the confidence score is meaningful across categories and not arbitrarily assigned.

**Implementation:** Three SQL queries against the existing `submissions` and `audit_log` tables — no schema changes were required. Full spec for this feature is in `planning.md` under `## Stretch Feature: Analytics Dashboard`.

---

## Known Limitations

**Formal human writing is the system's most predictable failure mode.** Academic papers, legal briefs, professional blog posts, and business memos tend to score high on both signals — the LLM scores them high because they read like AI's default register, and the stylometric signal scores them high because uniform sentence lengths, formal vocabulary, and minimal informal punctuation all point the same direction. In testing, a two-sentence excerpt on monetary policy scored `likely_ai` at 0.776 confidence despite being clearly human-authored. The wide uncertain band catches some of these cases (0.35–0.65), but anything above 0.65 will receive the AI label and require an appeal to correct. This is a structural limitation of using AI's typical output style as the detection target — when humans write like AI, the system cannot distinguish them.

**Short texts are unreliable regardless of origin.** Texts with fewer than 3 sentences or 10 words cause the stylometric signal to return 0.5 (neutral), and give the LLM too little context to assess stylistic patterns. The fallback pulls the combined score toward the center of the range, making an uncertain label more likely regardless of actual origin. This is an honest acknowledgment of the signal's limits rather than a false verdict.

---

## Spec Reflection

**One way the spec guided implementation:** Writing the three label variants in `planning.md` before writing any code forced a concrete design decision early: the uncertain label must not accuse. The phrase "Our system could not confidently determine" came directly from drafting the spec, and it shaped the entire tone of the label system — non-accusatory, transparent about the system's limits, actionable for the creator. Without the spec forcing me to write the exact text before building, I would likely have written a vaguer label and tried to refine it later.

**One way the implementation diverged from the spec:** The spec listed type-token ratio (TTR) as the second stylometric sub-metric. Dry-run testing before writing any signal code showed that casual human text (ramen review) had TTR ≈ 0.875 — nearly identical to formal AI text (TTR ≈ 0.884). The signal was not discriminating at all. Average word length was substituted, which separated the two categories cleanly (4.2 vs 6.2 chars average). The divergence is documented in `planning.md` under the Detection Signals section.

---

## AI Usage

**Instance 1 — Generating the Flask app skeleton and LLM signal function (M3):**
I provided the Detection Signals section from `planning.md` and the architecture diagram, and asked Claude to generate: (1) a Flask app with a `POST /submit` stub, and (2) a `classify_with_llm()` function that calls Groq and returns a float. The generated function used `response.json()` to parse the output, which would have failed on the raw string the Groq SDK returns. I replaced it with `json.loads(response.choices[0].message.content.strip())` and added a regex fallback for when the model returns text around the JSON. I also changed the model from `mixtral-8x7b-32768` to `llama-3.3-70b-versatile` for better accuracy, and set `temperature=0.1` to reduce score variance across repeated calls.

**Instance 2 — Generating the stylometric signal function (M4):**
I provided the Detection Signals section (stylometrics sub-metrics) and the architecture diagram, and asked Claude to generate the `compute_stylometrics()` function implementing all three sub-metrics. The generated function implemented TTR as specified. I ran a dry-run analysis on all four guide sample texts before integrating it and discovered that TTR did not discriminate between casual human writing and AI text. I replaced TTR with average word length after verifying the new metric produced the correct ordering across all four texts, and updated `planning.md` to document the divergence. The normalization constants (`/ 15.0` for sentence variance, `/ 5.0` for word length, `* 15.0` for informal density) were also tuned by me based on the dry-run results rather than taken from generated code.
