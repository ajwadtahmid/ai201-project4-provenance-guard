import json
import math
import os
import re

from groq import Groq

_client = None


def _groq():
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


def classify_with_llm(text: str) -> float:
    """
    Ask Groq to score how AI-generated the text appears.
    Returns a float 0.0 (human) to 1.0 (AI).
    Falls back to 0.5 on parse failure.
    """
    prompt = f"""You are an AI content detection system. Analyze the text below and return
ONLY a JSON object with this exact format (no explanation, no markdown):

{{"ai_score": <float between 0.0 and 1.0>}}

0.0 = definitely human-written, 1.0 = definitely AI-generated.

Signals to consider:
- Formulaic transitions: "Furthermore", "It is important to note", "In conclusion"
- Unnaturally balanced, perfectly structured arguments
- Absence of personal voice, genuine uncertainty, or digressions
- Suspiciously smooth, consistent sentence flow with no irregularities
- Generic phrasing that avoids specificity or anecdote

Text to analyze:
\"\"\"
{text}
\"\"\"
"""
    response = _groq().chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=64,
    )
    raw = response.choices[0].message.content.strip()
    try:
        score = json.loads(raw)["ai_score"]
        return float(max(0.0, min(1.0, score)))
    except Exception:
        # Try to extract a float from the raw string as a last resort
        match = re.search(r"(\d+(?:\.\d+)?)", raw)
        if match:
            return float(max(0.0, min(1.0, float(match.group(1)))))
        return 0.5


def _split_sentences(text: str) -> list:
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in parts if s.strip()]


def _tokenize_words(text: str) -> list:
    return re.findall(r'[a-zA-Z]+', text)


def compute_stylometrics(text: str) -> float:
    """
    Structural heuristics: 0.0 = human, 1.0 = AI.

    Three equally-weighted sub-metrics:
      1. Sentence-length standard deviation — AI writing is more uniform;
         low std dev maps to a high (AI) score.
      2. Average word length — AI writing favors longer, formal vocabulary;
         short average word length maps to a low (human) score.
         Range [3, 8] chars maps to [0.0, 1.0].
      3. Informal expression density — counts !, ?, ellipsis, em-dash,
         parentheses, and all-caps words. More informal markers → lower
         (human) score.

    Note: the planning spec listed type-token ratio as sub-metric 2, but
    TTR is unreliable on short texts because casual human writing naturally
    exhibits high lexical diversity. Average word length proved a stronger
    structural discriminator without this bias.

    Returns 0.5 when text has fewer than 3 sentences or 10 words.
    """
    sentences = _split_sentences(text)
    words = _tokenize_words(text)

    if len(sentences) < 3 or len(words) < 10:
        return 0.5

    # 1. Sentence-length standard deviation
    sent_lengths = [len(_tokenize_words(s)) for s in sentences if _tokenize_words(s)]
    if len(sent_lengths) < 2:
        sld_score = 0.5
    else:
        mean_l = sum(sent_lengths) / len(sent_lengths)
        variance = sum((l - mean_l) ** 2 for l in sent_lengths) / len(sent_lengths)
        std_dev = math.sqrt(variance)
        sld_score = max(0.0, 1.0 - std_dev / 15.0)

    # 2. Average word length  (3 chars → 0.0, 8 chars → 1.0)
    avg_word_len = sum(len(w) for w in words) / len(words)
    wl_score = min(1.0, max(0.0, (avg_word_len - 3.0) / 5.0))

    # 3. Informal expression density
    informal_count = (
        text.count('!')
        + text.count('?')
        + text.count('...')
        + text.count('…')
        + text.count('—')
        + text.count('--')
        + text.count('(')
        + text.count(')')
        + len(re.findall(r'\b[A-Z]{2,}\b', text))  # all-caps words
    )
    ipd_score = max(0.0, 1.0 - (informal_count / len(words)) * 15.0)

    return round((sld_score + wl_score + ipd_score) / 3.0, 4)
