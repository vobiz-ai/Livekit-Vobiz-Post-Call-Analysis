"""
gemini_analyzer.py — Gemini 2.0 Flash post-call transcript analysis
=====================================================================
Uses google-genai SDK with native structured output (response_schema)
so Pydantic models come back fully typed — no JSON parsing needed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from audio_analyzer import AudioReport

load_dotenv(".env")

logger = logging.getLogger("post-call.gemini")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_KEY   = os.getenv("GEMINI_API_KEY", "")

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_KEY:
            raise ValueError("GEMINI_API_KEY not set in .env")
        _client = genai.Client(api_key=GEMINI_KEY)
    return _client


# ---------------------------------------------------------------------------
# Pydantic schema — Gemini returns this natively structured
# ---------------------------------------------------------------------------

class QualityScores(BaseModel):
    overall:    int = Field(description="Overall call quality 0-100")
    clarity:    int = Field(description="How clearly both parties communicated 0-100")
    resolution: int = Field(description="How well the caller's issue was resolved 0-100")
    empathy:    int = Field(description="Agent empathy and tone 0-100")
    efficiency: int = Field(description="How efficiently the call was handled 0-100")


class KeyMoment(BaseModel):
    at:    str = Field(description="Approximate position in call e.g. '0:32' or 'early/mid/late'")
    event: str = Field(description="Description of what happened at this moment")


class TranscriptAnalysis(BaseModel):
    summary:              str            = Field(description="2-3 sentence plain-language summary of the call")
    caller_intent:        str            = Field(description="What the caller wanted to achieve")
    caller_sentiment:     str            = Field(description="Caller's emotional arc e.g. 'frustrated → satisfied'")
    agent_sentiment:      str            = Field(description="Agent's tone throughout e.g. 'professional, empathetic'")
    key_moments:          list[KeyMoment]= Field(description="Up to 5 significant moments in the call")
    action_items:         list[str]      = Field(description="Follow-up actions required after this call")
    unanswered_questions: list[str]      = Field(description="Questions the caller asked that were not answered")
    topics_covered:       list[str]      = Field(description="Main topics discussed e.g. ['billing','refund']")
    call_resolution:      str            = Field(description="One of: RESOLVED, UNRESOLVED, ESCALATED, TRANSFERRED, ABANDONED")
    confusion_signals:    list[str]      = Field(description="Signs of confusion e.g. repeated questions, long silences after explanation")
    quality_scores:       QualityScores  = Field(description="Numeric quality breakdown")


# ---------------------------------------------------------------------------
# Gate rules
# ---------------------------------------------------------------------------

GATE_MIN_DURATION   = int(os.getenv("GATE_MIN_DURATION_SECS",   "15"))
GATE_MAX_SILENCE    = int(os.getenv("GATE_MAX_SILENCE_PCT",      "60"))
GATE_MIN_MOS        = float(os.getenv("GATE_MIN_MOS",            "2.0"))
GATE_REVIEW_SILENCE = int(os.getenv("GATE_REVIEW_SILENCE_PCT",   "30"))
GATE_REVIEW_MOS     = float(os.getenv("GATE_REVIEW_MOS",         "3.5"))
GATE_REVIEW_JITTER  = int(os.getenv("GATE_REVIEW_MAX_JITTER_MS", "50"))


def compute_gate(audio: AudioReport, ta: TranscriptAnalysis, duration_seconds: int) -> dict:
    """
    Compute production gate verdict: PASS / REVIEW / FAIL.
    Returns: { verdict, flags, reason }
    """
    flags: list[str] = []

    # ---- FAIL conditions ----
    if duration_seconds < GATE_MIN_DURATION:
        flags.append("call_too_short")

    if audio.silence_pct is not None and audio.silence_pct > GATE_MAX_SILENCE:
        flags.append("mostly_silence")

    if audio.mos is not None and audio.mos < GATE_MIN_MOS:
        flags.append("very_poor_audio_quality")

    intent = (ta.caller_intent or "").strip().lower()
    if not intent or intent in ("unknown", "none", "n/a", ""):
        flags.append("intent_not_captured")

    if ta.call_resolution in ("ABANDONED",):
        flags.append("call_abandoned")

    # ---- REVIEW conditions (only checked if no FAIL) ----
    review_flags: list[str] = []

    if audio.silence_pct is not None and audio.silence_pct > GATE_REVIEW_SILENCE:
        review_flags.append("high_silence")

    if audio.mos is not None and audio.mos < GATE_REVIEW_MOS:
        review_flags.append("poor_audio_quality")

    if audio.jitter_ms is not None and audio.jitter_ms > GATE_REVIEW_JITTER:
        review_flags.append("high_jitter")

    if ta.call_resolution in ("UNRESOLVED", "ESCALATED"):
        review_flags.append(f"call_{ta.call_resolution.lower()}")

    if ta.quality_scores.overall < 50:
        review_flags.append("low_quality_score")

    if len(ta.unanswered_questions) >= 2:
        review_flags.append("multiple_unanswered_questions")

    if len(ta.confusion_signals) >= 2:
        review_flags.append("high_confusion")

    if audio.clipping:
        review_flags.append("audio_clipping")

    # ---- Verdict ----
    if flags:
        verdict = "FAIL"
        all_flags = flags
        reason = f"FAIL: {', '.join(flags)}"
    elif review_flags:
        verdict = "REVIEW"
        all_flags = review_flags
        reason = f"REVIEW: {', '.join(review_flags)}"
    else:
        verdict = "PASS"
        all_flags = []
        reason = f"All checks passed. Quality: {ta.quality_scores.overall}/100"

    return {
        "verdict": verdict,
        "flags":   all_flags,
        "reason":  reason,
    }


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are an expert call quality analyst for a voice AI platform.

Analyze the following call transcript and produce a detailed quality report.
Be objective and specific. Base all scores on what actually happened in the call.

Scoring guide (0-100):
- 90-100: Exceptional
- 75-89:  Good
- 50-74:  Acceptable
- 25-49:  Poor
- 0-24:   Very poor / failed

For call_resolution use exactly one of:
RESOLVED, UNRESOLVED, ESCALATED, TRANSFERRED, ABANDONED

For confusion_signals look for:
- Caller repeating the same question
- Long pauses after agent explanation (check "..." or repeated "hello?" in transcript)
- Caller saying "I don't understand" or "what do you mean"
- Agent failing to directly answer questions

For key_moments identify turning points:
- When the caller stated their main issue
- When the agent offered a solution
- Any escalation or transfer points
- Moments of frustration or satisfaction
""".strip()


async def analyze_transcript(
    transcript: list[dict],
    from_number: str = "",
    to_number:   str = "",
    duration_seconds: int = 0,
    mos:     float | None = None,
    jitter:  int   | None = None,
) -> TranscriptAnalysis:
    """
    Analyze call transcript using Gemini 2.0 Flash.
    Returns a fully typed TranscriptAnalysis Pydantic object.
    """
    if not transcript:
        logger.warning("No transcript available — returning empty analysis")
        return _empty_analysis()

    # Format transcript for the prompt
    formatted = _format_transcript(transcript)

    context = (
        f"Call from {from_number} to {to_number}\n"
        f"Duration: {duration_seconds}s\n"
    )
    if mos is not None:
        context += f"MOS (voice quality): {mos}/5.0\n"
    if jitter is not None:
        context += f"Jitter: {jitter}ms\n"

    prompt = (
        f"{context}\n"
        f"TRANSCRIPT:\n{formatted}\n\n"
        "Produce a complete quality analysis report."
    )

    try:
        client   = _get_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Content(role="user", parts=[
                    types.Part(text=SYSTEM_PROMPT + "\n\n" + prompt)
                ])
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TranscriptAnalysis,
                temperature=0.1,
            ),
        )

        result = response.parsed
        if result and isinstance(result, TranscriptAnalysis):
            logger.info("Gemini analysis complete. Resolution=%s  Overall=%d",
                        result.call_resolution, result.quality_scores.overall)
            return result

        # Fallback: parse text manually
        logger.warning("Gemini response.parsed was None — trying text parse")
        import json as _json
        raw = response.text or "{}"
        data = _json.loads(raw)
        return TranscriptAnalysis(**data)

    except Exception as exc:
        logger.error("Gemini analysis failed: %s", exc)
        return _empty_analysis(error=str(exc))


def _format_transcript(lines: list[dict]) -> str:
    """Format transcript list into readable text for the prompt."""
    parts = []
    for i, line in enumerate(lines):
        speaker = line.get("speaker", "unknown").upper()
        text    = line.get("text", "").strip()
        ts      = line.get("timestamp", "")
        if text:
            ts_short = ts[11:19] if len(ts) >= 19 else ""  # HH:MM:SS from ISO
            parts.append(f"[{ts_short}] {speaker}: {text}")
    return "\n".join(parts) if parts else "(No transcript available)"


def _empty_analysis(error: str = "") -> TranscriptAnalysis:
    return TranscriptAnalysis(
        summary=f"Analysis unavailable.{' Error: ' + error if error else ''}",
        caller_intent="unknown",
        caller_sentiment="unknown",
        agent_sentiment="unknown",
        key_moments=[],
        action_items=[],
        unanswered_questions=[],
        topics_covered=[],
        call_resolution="UNRESOLVED",
        confusion_signals=[],
        quality_scores=QualityScores(
            overall=0, clarity=0, resolution=0, empathy=0, efficiency=0
        ),
    )
