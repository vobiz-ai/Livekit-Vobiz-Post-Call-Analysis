"""
gemini_analyzer.py — Robust Gemini 2.0 Flash post-call transcript analysis
============================================================================

Robustness features:
  - Automatic retry with exponential backoff (up to 3 attempts)
  - Fallback model chain: gemini-2.0-flash → gemini-1.5-flash → gemini-1.5-pro
  - Response validation: clamp scores 0-100, enforce valid resolution values,
    strip empty strings, deduplicate lists
  - Partial response recovery: if parsed is None, extract from raw JSON text
  - Language detection: auto-detects Hindi/Hinglish/English and adjusts prompt
  - Indian telecom context: understands OLX, Telegram, UPI, Paytm scam patterns
  - Short call handling: special prompt for calls < 30s
  - Token budget: truncates very long transcripts to fit context window
  - Structured logging with timing metrics
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator

from audio_analyzer import AudioReport

load_dotenv(".env")

logger = logging.getLogger("post-call.gemini")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_KEY   = (
    os.getenv("GEMINI_API_KEY")
    or os.getenv("GOOGLE_GENERATIVE_AI_API_KEY")
    or ""
)

# Fallback model chain — tried in order if primary fails
FALLBACK_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-flash-latest",
]

MAX_RETRIES      = int(os.getenv("GEMINI_MAX_RETRIES",      "3"))
RETRY_BASE_DELAY = float(os.getenv("GEMINI_RETRY_BASE_DELAY", "1.5"))
MAX_TRANSCRIPT_LINES = int(os.getenv("MAX_TRANSCRIPT_LINES", "200"))  # token budget

VALID_RESOLUTIONS = {"RESOLVED", "UNRESOLVED", "ESCALATED", "TRANSFERRED", "ABANDONED"}

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not GEMINI_KEY:
            raise ValueError(
                "Gemini API key not set. Add GEMINI_API_KEY or "
                "GOOGLE_GENERATIVE_AI_API_KEY to .env"
            )
        _client = genai.Client(api_key=GEMINI_KEY)
    return _client


# ---------------------------------------------------------------------------
# Pydantic schema with validators
# ---------------------------------------------------------------------------

class QualityScores(BaseModel):
    overall:    int = Field(default=0, description="Overall call quality 0-100")
    clarity:    int = Field(default=0, description="Communication clarity 0-100")
    resolution: int = Field(default=0, description="Issue resolution effectiveness 0-100")
    empathy:    int = Field(default=0, description="Agent empathy and warmth 0-100")
    efficiency: int = Field(default=0, description="Call efficiency 0-100")

    @field_validator("overall", "clarity", "resolution", "empathy", "efficiency", mode="before")
    @classmethod
    def clamp(cls, v):
        try:
            return max(0, min(100, int(v)))
        except (TypeError, ValueError):
            return 0


class KeyMoment(BaseModel):
    at:    str = Field(default="", description="Approximate time e.g. '0:32' or 'early/mid/late'")
    event: str = Field(default="", description="What happened")

    @field_validator("at", "event", mode="before")
    @classmethod
    def coerce_str(cls, v):
        return str(v).strip() if v is not None else ""

    @classmethod
    def from_any(cls, v) -> "KeyMoment":
        """Accept dict, string repr, or already-constructed instance."""
        if isinstance(v, cls):
            return v
        if isinstance(v, dict):
            return cls(**{k: str(val) for k, val in v.items() if k in ("at", "event")})
        if isinstance(v, str):
            # Try eval as dict (safe: only string literals)
            import ast
            try:
                d = ast.literal_eval(v)
                if isinstance(d, dict):
                    return cls(**{k: str(val) for k, val in d.items() if k in ("at", "event")})
            except Exception:
                pass
            # Treat whole string as event description
            return cls(at="", event=v[:200])
        return cls(at="", event=str(v)[:200])


class TranscriptAnalysis(BaseModel):
    summary:              str             = Field(default="", description="2-3 sentence summary")
    caller_intent:        str             = Field(default="unknown", description="What the caller wanted")
    caller_sentiment:     str             = Field(default="neutral", description="Caller emotional arc")
    agent_sentiment:      str             = Field(default="neutral", description="Agent tone")
    language_detected:    str             = Field(default="English", description="Primary language: English, Hindi, Hinglish")
    key_moments:          list[KeyMoment] = Field(default_factory=list, description="Up to 5 turning points")
    action_items:         list[str]       = Field(default_factory=list, description="Follow-up actions needed")
    unanswered_questions: list[str]       = Field(default_factory=list, description="Caller questions not addressed")
    topics_covered:       list[str]       = Field(default_factory=list, description="Topics discussed")
    call_resolution:      str             = Field(default="UNRESOLVED", description="RESOLVED/UNRESOLVED/ESCALATED/TRANSFERRED/ABANDONED")
    confusion_signals:    list[str]       = Field(default_factory=list, description="Signs of confusion or frustration")
    scam_indicators:      list[str]       = Field(default_factory=list, description="Fraud/scam signals detected (OLX, Telegram, advance payment, etc.)")
    quality_scores:       QualityScores   = Field(default_factory=QualityScores, description="Numeric quality breakdown")
    analysis_confidence:  int             = Field(default=50, description="Analyzer confidence 0-100 (lower if transcript is short/unclear)")

    @field_validator("call_resolution", mode="before")
    @classmethod
    def validate_resolution(cls, v):
        v = str(v).strip().upper()
        return v if v in VALID_RESOLUTIONS else "UNRESOLVED"

    @field_validator("summary", "caller_intent", "caller_sentiment",
                     "agent_sentiment", "language_detected", mode="before")
    @classmethod
    def strip_str(cls, v):
        return str(v).strip() if v else ""

    @field_validator("key_moments", mode="before")
    @classmethod
    def coerce_key_moments(cls, v):
        if not isinstance(v, list):
            return []
        result = []
        for item in v:
            try:
                result.append(KeyMoment.from_any(item))
            except Exception:
                pass
        return result

    @field_validator("action_items", "unanswered_questions",
                     "topics_covered", "confusion_signals", "scam_indicators", mode="before")
    @classmethod
    def deduplicate_list(cls, v):
        if not isinstance(v, list):
            return []
        seen, out = set(), []
        for item in v:
            s = str(item).strip() if item else ""
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    @field_validator("analysis_confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, v):
        try:
            return max(0, min(100, int(v)))
        except (TypeError, ValueError):
            return 50


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
    Returns: { verdict, flags, reason, score_breakdown }
    """
    fail_flags:   list[str] = []
    review_flags: list[str] = []

    # ── FAIL conditions ────────────────────────────────────────────────────
    if duration_seconds < GATE_MIN_DURATION:
        fail_flags.append("call_too_short")

    if audio.silence_pct is not None and audio.silence_pct > GATE_MAX_SILENCE:
        fail_flags.append("mostly_silence")

    if audio.mos is not None and audio.mos < GATE_MIN_MOS:
        fail_flags.append("very_poor_audio_quality")

    intent = (ta.caller_intent or "").strip().lower()
    if not intent or intent in ("unknown", "none", "n/a", "unclear", ""):
        fail_flags.append("intent_not_captured")

    if ta.call_resolution == "ABANDONED":
        fail_flags.append("call_abandoned")

    if ta.quality_scores.overall < 25:
        fail_flags.append("critically_low_quality")

    if ta.scam_indicators and len(ta.scam_indicators) >= 3:
        fail_flags.append("multiple_scam_indicators")

    # ── REVIEW conditions ──────────────────────────────────────────────────
    if audio.silence_pct is not None and audio.silence_pct > GATE_REVIEW_SILENCE:
        review_flags.append("high_silence")

    if audio.mos is not None and audio.mos < GATE_REVIEW_MOS:
        review_flags.append("poor_audio_quality")

    if audio.jitter_ms is not None and audio.jitter_ms > GATE_REVIEW_JITTER:
        review_flags.append("high_jitter")

    if audio.one_sided:
        review_flags.append("one_sided_audio")

    if ta.call_resolution in ("UNRESOLVED", "ESCALATED"):
        review_flags.append(f"call_{ta.call_resolution.lower()}")

    if 25 <= ta.quality_scores.overall < 50:
        review_flags.append("low_quality_score")

    if len(ta.unanswered_questions) >= 2:
        review_flags.append("multiple_unanswered_questions")

    if len(ta.confusion_signals) >= 2:
        review_flags.append("high_confusion")

    if audio.clipping:
        review_flags.append("audio_clipping")

    if audio.low_volume:
        review_flags.append("low_audio_volume")

    if ta.scam_indicators and len(ta.scam_indicators) >= 1:
        review_flags.append(f"scam_indicators_detected({len(ta.scam_indicators)})")

    if ta.analysis_confidence < 40:
        review_flags.append("low_analysis_confidence")

    # ── Verdict ────────────────────────────────────────────────────────────
    if fail_flags:
        verdict   = "FAIL"
        all_flags = fail_flags + [f for f in review_flags if f not in fail_flags]
        reason    = f"FAIL due to: {', '.join(fail_flags)}"
    elif review_flags:
        verdict   = "REVIEW"
        all_flags = review_flags
        reason    = f"REVIEW: {', '.join(review_flags)}"
    else:
        verdict   = "PASS"
        all_flags = []
        reason    = (
            f"All quality checks passed. "
            f"Quality: {ta.quality_scores.overall}/100, "
            f"Resolution: {ta.call_resolution}"
        )

    return {
        "verdict":    verdict,
        "flags":      all_flags,
        "reason":     reason,
        "score_breakdown": {
            "overall":    ta.quality_scores.overall,
            "clarity":    ta.quality_scores.clarity,
            "resolution": ta.quality_scores.resolution,
            "empathy":    ta.quality_scores.empathy,
            "efficiency": ta.quality_scores.efficiency,
            "confidence": ta.analysis_confidence,
        },
    }


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are an expert call quality analyst specializing in Indian voice AI platforms and UPI payment fraud detection.

Your job: analyze a call transcript and produce a detailed, accurate quality report.

IMPORTANT CONTEXT — Indian telecom & fraud patterns:
- Calls may be in English, Hindi, or Hinglish (mixed Hindi-English)
- Common scam patterns to flag in scam_indicators:
    • OLX/Quikr/Telegram/WhatsApp deal (fake sellers)
    • Advance payment requests ("pehle paisa bhejo")
    • Lottery/prize scams ("aapne jeet liya")
    • Cheap electronics (iPhone/MacBook "bahut sasta")
    • Crypto/investment returns ("double money")
    • Tech support fraud ("aapka computer virus hai")
    • KYC update scams ("aapka account band ho jayega")
- "Haan" = yes, "Nahi" = no, "Theek hai" = okay
- Caller may switch between languages mid-sentence

SCORING GUIDE (0-100):
  90-100: Exceptional
  75-89:  Good — minor issues only
  50-74:  Acceptable — clear room for improvement
  25-49:  Poor — significant problems
  0-24:   Failed / critical issues

RESOLUTION VALUES (use exactly one):
  RESOLVED    — caller's issue fully addressed
  UNRESOLVED  — issue raised but not solved
  ESCALATED   — transferred to specialist or higher tier
  TRANSFERRED — SIP/call transferred to human agent
  ABANDONED   — caller hung up without completing interaction

ANALYSIS CONFIDENCE:
  Set lower (< 50) when:
  - Transcript is very short (< 5 lines)
  - Caller gave one-word answers only
  - Heavy background noise mentioned in context
  - Language is mostly unclear

Be specific. Do NOT use generic phrases like "the call was good".
Reference actual things said in the transcript.
""".strip()

_SHORT_CALL_ADDENDUM = """
NOTE: This was a SHORT call (< 30 seconds). Many fields may have limited data.
Score conservatively. If the caller hung up immediately, set call_resolution=ABANDONED
and analysis_confidence < 40.
""".strip()


def _build_prompt(
    transcript: list[dict],
    from_number: str,
    to_number:   str,
    duration_seconds: int,
    mos:     Optional[float],
    jitter:  Optional[int],
    language_hint: str,
) -> str:
    ctx_lines = [
        f"Call: {from_number} → {to_number}",
        f"Duration: {duration_seconds}s",
    ]
    if mos     is not None: ctx_lines.append(f"Audio MOS: {mos}/5.0")
    if jitter  is not None: ctx_lines.append(f"Network jitter: {jitter}ms")
    if language_hint:       ctx_lines.append(f"Detected language: {language_hint}")

    context = "\n".join(ctx_lines)
    formatted = _format_transcript(transcript)

    addendum = _SHORT_CALL_ADDENDUM if duration_seconds < 30 else ""

    return (
        f"{context}\n\n"
        f"{addendum}\n\n" if addendum else f"{context}\n\n"
    ) + f"TRANSCRIPT:\n{formatted}\n\nProduce the complete quality analysis report."


def _format_transcript(lines: list[dict]) -> str:
    """Format transcript with relative timestamps and line numbers."""
    if not lines:
        return "(No transcript available)"

    # Truncate if too long
    if len(lines) > MAX_TRANSCRIPT_LINES:
        logger.warning("Truncating transcript from %d to %d lines (token budget)",
                       len(lines), MAX_TRANSCRIPT_LINES)
        # Keep first 20 + last N lines to preserve opening and closing context
        head = lines[:20]
        tail = lines[-(MAX_TRANSCRIPT_LINES - 20):]
        lines = head + [{"speaker": "...", "text": f"[{len(lines)-MAX_TRANSCRIPT_LINES} lines omitted]"}] + tail

    parts = []
    first_ts = None

    for i, line in enumerate(lines, 1):
        speaker = (line.get("speaker") or "unknown").upper()
        text    = (line.get("text") or "").strip()
        if not text:
            continue

        # Build relative timestamp
        ts = line.get("timestamp", "")
        rel = ""
        try:
            if ts and len(ts) >= 19:
                from datetime import datetime
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if first_ts is None:
                    first_ts = t
                delta = int((t - first_ts).total_seconds())
                rel = f"+{delta:>3}s"
        except Exception:
            pass

        parts.append(f"[{i:>3}] {rel:>5} {speaker:<7}: {text}")

    return "\n".join(parts) if parts else "(Empty transcript)"


def _detect_language(transcript: list[dict]) -> str:
    """Quick heuristic to detect if transcript is Hinglish/Hindi/English."""
    hindi_markers = {
        "haan", "nahi", "aap", "kya", "hai", "hoon", "main",
        "mera", "paisa", "rupee", "theek", "achha", "bhai",
        "namaste", "shukriya", "ji", "raha", "tha", "karo",
    }
    all_text = " ".join(
        (line.get("text") or "").lower() for line in transcript
    )
    words     = re.findall(r"\b\w+\b", all_text)
    hindi_ct  = sum(1 for w in words if w in hindi_markers)
    ratio     = hindi_ct / max(len(words), 1)

    if ratio > 0.15:
        return "Hinglish (Hindi-English mix)"
    if ratio > 0.05:
        return "Primarily English with some Hindi"
    return "English"


# ---------------------------------------------------------------------------
# Core analysis with retry + fallback
# ---------------------------------------------------------------------------

async def analyze_transcript(
    transcript:       list[dict],
    from_number:      str   = "",
    to_number:        str   = "",
    duration_seconds: int   = 0,
    mos:              Optional[float] = None,
    jitter:           Optional[int]   = None,
) -> TranscriptAnalysis:
    """
    Analyze call transcript using Gemini 2.0 Flash.

    Robustness:
    - Retries up to MAX_RETRIES times with exponential backoff
    - Falls back through model chain on persistent failure
    - Validates and sanitizes all returned fields
    - Never raises — always returns a TranscriptAnalysis (may be empty on total failure)
    """
    if not transcript:
        logger.warning("No transcript lines — returning empty analysis")
        return _empty_analysis(reason="no_transcript")

    # Filter out empty lines
    transcript = [l for l in transcript if (l.get("text") or "").strip()]
    if not transcript:
        logger.warning("All transcript lines were empty")
        return _empty_analysis(reason="empty_transcript_lines")

    lang = _detect_language(transcript)

    prompt = _build_prompt(
        transcript=transcript,
        from_number=from_number,
        to_number=to_number,
        duration_seconds=duration_seconds,
        mos=mos,
        jitter=jitter,
        language_hint=lang,
    )

    full_prompt = _SYSTEM_PROMPT + "\n\n" + prompt

    # Try primary model first, then fallbacks
    models_to_try = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]

    for model_name in models_to_try:
        result = await _attempt_analysis(full_prompt, model_name, transcript, duration_seconds)
        if result is not None:
            return result
        logger.warning("Model %s failed — trying next fallback", model_name)

    logger.error("All Gemini models failed — returning empty analysis")
    return _empty_analysis(reason="all_models_failed")


async def _attempt_analysis(
    prompt:           str,
    model_name:       str,
    transcript:       list[dict],
    duration_seconds: int,
) -> Optional[TranscriptAnalysis]:
    """
    Attempt analysis with one model, retrying on transient errors.
    Returns None if all retries fail.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            t0 = time.time()
            client = _get_client()

            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Content(role="user", parts=[types.Part(text=prompt)])
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TranscriptAnalysis,
                    temperature=0.1,
                    max_output_tokens=2048,
                ),
            )

            elapsed = round(time.time() - t0, 2)

            # Try .parsed first (native structured output)
            result = response.parsed
            if result and isinstance(result, TranscriptAnalysis):
                result = _validate_and_fix(result, transcript, duration_seconds)
                logger.info(
                    "Gemini analysis OK — model=%s attempt=%d elapsed=%.2fs "
                    "resolution=%s overall=%d confidence=%d",
                    model_name, attempt, elapsed,
                    result.call_resolution,
                    result.quality_scores.overall,
                    result.analysis_confidence,
                )
                return result

            # Fallback: parse raw JSON text
            raw_text = response.text or ""
            if raw_text.strip():
                result = _parse_raw_json(raw_text, transcript, duration_seconds)
                if result:
                    logger.info("Gemini raw-parse OK — model=%s attempt=%d", model_name, attempt)
                    return result

            logger.warning("Gemini returned empty/unparseable response (attempt %d/%d)",
                           attempt, MAX_RETRIES)

        except Exception as exc:
            err_str = str(exc)
            is_transient = any(code in err_str for code in [
                "429", "500", "503", "RESOURCE_EXHAUSTED",
                "UNAVAILABLE", "INTERNAL", "timeout", "rate"
            ])

            if is_transient and attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY ** attempt
                logger.warning("Transient error (attempt %d/%d) — retrying in %.1fs: %s",
                               attempt, MAX_RETRIES, delay, err_str[:100])
                await asyncio.sleep(delay)
                continue

            # Non-transient or final attempt
            logger.error("Gemini error (model=%s attempt=%d): %s",
                         model_name, attempt, err_str[:200])
            if not is_transient:
                return None  # Don't retry non-transient errors

    return None


def _parse_raw_json(
    raw: str,
    transcript: list[dict],
    duration_seconds: int,
) -> Optional[TranscriptAnalysis]:
    """Extract JSON from Gemini's raw text response and parse into schema."""
    # Strip markdown code fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw.strip(), flags=re.MULTILINE)

    # Find first complete JSON object
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON object via regex
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except Exception:
            return None

    try:
        result = TranscriptAnalysis(**data)
        return _validate_and_fix(result, transcript, duration_seconds)
    except Exception as exc:
        # Try fixing key_moments format issue
        if "key_moments" in data and isinstance(data.get("key_moments"), list):
            data["key_moments"] = [
                KeyMoment.from_any(item).model_dump()
                for item in data["key_moments"]
            ]
            try:
                result = TranscriptAnalysis(**data)
                return _validate_and_fix(result, transcript, duration_seconds)
            except Exception:
                pass
        logger.warning("JSON parse failed: %s", exc)
        return None


def _validate_and_fix(
    ta: TranscriptAnalysis,
    transcript: list[dict],
    duration_seconds: int,
) -> TranscriptAnalysis:
    """
    Post-process: fix common Gemini output issues.
    """
    # Fix intent — Gemini sometimes returns very verbose intent strings
    if len(ta.caller_intent) > 200:
        ta.caller_intent = ta.caller_intent[:197] + "..."

    # If call was very short and resolution isn't ABANDONED, consider overriding
    if duration_seconds < 15 and ta.call_resolution not in ("ABANDONED", "TRANSFERRED"):
        ta.call_resolution = "ABANDONED"
        ta.analysis_confidence = min(ta.analysis_confidence, 40)

    # If transcript only has agent lines (caller never spoke), flag it
    caller_lines = [l for l in transcript if l.get("speaker") == "caller"]
    if not caller_lines:
        ta.confusion_signals = list(set(ta.confusion_signals + ["caller_never_spoke"]))
        ta.analysis_confidence = min(ta.analysis_confidence, 30)

    # Ensure quality_scores.overall matches the tone of call_resolution
    if ta.call_resolution == "ABANDONED" and ta.quality_scores.overall > 60:
        # Cap abandoned calls at 60
        ta.quality_scores = QualityScores(
            overall=min(ta.quality_scores.overall, 60),
            clarity=ta.quality_scores.clarity,
            resolution=min(ta.quality_scores.resolution, 20),
            empathy=ta.quality_scores.empathy,
            efficiency=ta.quality_scores.efficiency,
        )

    if ta.call_resolution == "RESOLVED" and ta.quality_scores.resolution < 60:
        ta.quality_scores = QualityScores(
            overall=ta.quality_scores.overall,
            clarity=ta.quality_scores.clarity,
            resolution=max(ta.quality_scores.resolution, 60),
            empathy=ta.quality_scores.empathy,
            efficiency=ta.quality_scores.efficiency,
        )

    # Clean up empty strings from lists
    ta.action_items         = [x for x in ta.action_items if x.strip()]
    ta.unanswered_questions = [x for x in ta.unanswered_questions if x.strip()]
    ta.topics_covered       = [x for x in ta.topics_covered if x.strip()]
    ta.confusion_signals    = [x for x in ta.confusion_signals if x.strip()]
    ta.scam_indicators      = [x for x in ta.scam_indicators if x.strip()]

    return ta


def _empty_analysis(reason: str = "") -> TranscriptAnalysis:
    confidence = {
        "no_transcript":          0,
        "empty_transcript_lines": 5,
        "all_models_failed":      0,
    }.get(reason, 0)

    return TranscriptAnalysis(
        summary=f"Analysis could not be completed ({reason})." if reason else "Analysis unavailable.",
        caller_intent="unknown",
        caller_sentiment="unknown",
        agent_sentiment="unknown",
        language_detected="unknown",
        key_moments=[],
        action_items=[],
        unanswered_questions=[],
        topics_covered=[],
        call_resolution="UNRESOLVED",
        confusion_signals=[],
        scam_indicators=[],
        quality_scores=QualityScores(overall=0, clarity=0, resolution=0, empathy=0, efficiency=0),
        analysis_confidence=confidence,
    )
