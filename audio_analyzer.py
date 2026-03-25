"""
audio_analyzer.py — Robust pydub-based call recording analysis
===============================================================

Robustness features:
  - Handles corrupt/truncated audio files gracefully
  - Auto-calibrates silence threshold based on file's noise floor
    (avoids false positives in noisy telecom recordings)
  - Stereo → mono normalization before analysis
  - Separate per-channel analysis for stereo (agent vs caller separation)
  - Speech activity estimation (% of call with actual speech)
  - Background noise level classification (quiet/moderate/noisy/very noisy)
  - Ring tone detection (call may not have been answered)
  - Audio format validation before processing
  - Always returns valid AudioReport even on complete failure
  - Incorporates MOS + Jitter from Vobiz Hangup CDR payload

Requires: pydub + ffmpeg
Install:  pip install pydub && brew install ffmpeg
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator

logger = logging.getLogger("post-call.audio")

# ---------------------------------------------------------------------------
# Configurable thresholds (can override via env vars)
# ---------------------------------------------------------------------------

SILENCE_THRESH_OFFSET = int(os.getenv("AUDIO_SILENCE_OFFSET_DB", "14"))
# Auto-threshold = noise_floor + SILENCE_THRESH_OFFSET dB
# Fallback if we can't measure noise floor:
SILENCE_THRESH_FALLBACK = int(os.getenv("AUDIO_SILENCE_THRESH_DB", "-40"))

MIN_SILENCE_LEN_MS = int(os.getenv("AUDIO_MIN_SILENCE_MS",  "400"))   # min gap to count
LONG_SILENCE_MS    = int(os.getenv("AUDIO_LONG_SILENCE_MS", "3000"))  # notable gap
CLIPPING_THRESH    = float(os.getenv("AUDIO_CLIPPING_THRESH", "-0.5"))
LOW_VOLUME_THRESH  = float(os.getenv("AUDIO_LOW_VOLUME_THRESH", "-35.0"))
ONE_SIDED_DIFF_DB  = float(os.getenv("AUDIO_ONE_SIDED_DIFF_DB", "18.0"))

# Ring tone heuristic: if first 6 seconds are periodic silence → ringing
RING_DETECT_WINDOW_MS = 6000


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class SilenceGap(BaseModel):
    at:          str  # e.g. "0:32"
    duration_ms: int

    @field_validator("at", mode="before")
    @classmethod
    def coerce(cls, v): return str(v) if v else ""


class AudioReport(BaseModel):
    analyzed:          bool = False
    error:             Optional[str] = None
    format:            Optional[str] = None   # "mp3", "ogg", etc.
    channels:          Optional[int] = None   # 1=mono, 2=stereo

    # Timing
    file_duration_ms:  Optional[int]   = None
    speech_pct:        Optional[float] = None   # % with speech activity
    silence_pct:       Optional[float] = None   # % of silent frames
    silence_gaps:      list[SilenceGap] = []
    ring_detected:     bool = False             # sounds like unanswered call

    # Volume
    volume_dBFS:       Optional[float] = None   # mean amplitude (full mix)
    max_dBFS:          Optional[float] = None   # peak amplitude
    noise_floor_dBFS:  Optional[float] = None   # estimated background noise
    clipping:          bool = False
    low_volume:        bool = False

    # Per-channel (stereo only — ch0=agent, ch1=caller for telephony)
    ch0_volume_dBFS:   Optional[float] = None
    ch1_volume_dBFS:   Optional[float] = None
    one_sided:         bool = False
    dominant_speaker:  Optional[str]   = None   # "agent" | "caller" | "balanced"

    # Background noise
    noise_level:       str = "unknown"   # quiet/moderate/noisy/very_noisy

    # Silence threshold used
    silence_threshold_used: Optional[float] = None

    # From Vobiz CDR
    mos:               Optional[float] = None
    jitter_ms:         Optional[int]   = None
    duration_seconds:  Optional[int]   = None
    billsec:           Optional[int]   = None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_audio(
    mp3_path: Path | str | None,
    mos:      Optional[float] = None,
    jitter:   Optional[int]   = None,
    duration: Optional[int]   = None,
    billsec:  Optional[int]   = None,
) -> AudioReport:
    """
    Analyze audio file and combine with Vobiz CDR metrics.
    Always returns a valid AudioReport — never raises.
    """
    report = AudioReport(
        mos=mos,
        jitter_ms=jitter,
        duration_seconds=duration,
        billsec=billsec,
    )

    if not mp3_path:
        logger.warning("No audio path provided — CDR metrics only")
        return report

    path = Path(mp3_path)
    if not path.exists():
        logger.warning("Audio file not found: %s", path)
        return report

    if path.stat().st_size < 1024:
        report.error = f"Audio file too small ({path.stat().st_size} bytes) — likely empty"
        logger.warning(report.error)
        return report

    report.format = path.suffix.lstrip(".").lower()

    try:
        from pydub import AudioSegment
        from pydub.silence import detect_silence

        # ── Load audio ────────────────────────────────────────────────────
        audio = _safe_load(path)
        if audio is None:
            report.error = "Could not decode audio file (corrupt or unsupported format)"
            return report

        total_ms       = len(audio)
        report.file_duration_ms = total_ms
        report.channels         = audio.channels

        if total_ms < 500:
            report.error = "Audio file too short to analyze (< 0.5s)"
            return report

        # ── Normalize to mono for global analysis ──────────────────────────
        mono = audio.set_channels(1)

        report.volume_dBFS = _safe_dbfs(mono)
        report.max_dBFS    = _safe_max_dbfs(mono)
        report.clipping    = (report.max_dBFS or -100) > CLIPPING_THRESH
        report.low_volume  = (report.volume_dBFS or 0) < LOW_VOLUME_THRESH

        # ── Noise floor estimation ─────────────────────────────────────────
        report.noise_floor_dBFS = _estimate_noise_floor(mono)
        report.noise_level      = _classify_noise(report.noise_floor_dBFS)

        # ── Auto-calibrate silence threshold ──────────────────────────────
        if report.noise_floor_dBFS is not None:
            silence_thresh = report.noise_floor_dBFS + SILENCE_THRESH_OFFSET
        else:
            silence_thresh = SILENCE_THRESH_FALLBACK
        report.silence_threshold_used = round(silence_thresh, 1)

        # ── Silence detection ──────────────────────────────────────────────
        silent_ranges = detect_silence(
            mono,
            min_silence_len=MIN_SILENCE_LEN_MS,
            silence_thresh=silence_thresh,
        )

        total_silent_ms      = sum(e - s for s, e in silent_ranges)
        report.silence_pct   = round((total_silent_ms / total_ms) * 100, 1)
        report.speech_pct    = round(100.0 - report.silence_pct, 1)

        # Notable long gaps
        notable = []
        for start_ms, end_ms in silent_ranges:
            gap_ms = end_ms - start_ms
            if gap_ms >= LONG_SILENCE_MS:
                secs = start_ms // 1000
                notable.append(SilenceGap(
                    at=f"{secs // 60}:{secs % 60:02d}",
                    duration_ms=gap_ms,
                ))
        report.silence_gaps = notable[:15]

        # ── Ring tone detection ────────────────────────────────────────────
        if total_ms >= RING_DETECT_WINDOW_MS:
            report.ring_detected = _detect_ring_tone(mono[:RING_DETECT_WINDOW_MS])

        # ── Per-channel analysis (stereo) ──────────────────────────────────
        if audio.channels == 2:
            ch0 = audio.split_to_mono()[0]
            ch1 = audio.split_to_mono()[1]
            ch0_vol = _safe_dbfs(ch0)
            ch1_vol = _safe_dbfs(ch1)
            report.ch0_volume_dBFS = ch0_vol
            report.ch1_volume_dBFS = ch1_vol

            if ch0_vol is not None and ch1_vol is not None:
                diff = abs(ch0_vol - ch1_vol)
                report.one_sided = diff > ONE_SIDED_DIFF_DB
                if diff < 5:
                    report.dominant_speaker = "balanced"
                elif ch0_vol > ch1_vol:
                    report.dominant_speaker = "agent"   # ch0 = agent in telephony
                else:
                    report.dominant_speaker = "caller"
        else:
            # Mono — use energy distribution across time to detect one-sided
            if total_ms > 4000:
                first_h  = mono[:total_ms // 2]
                second_h = mono[total_ms // 2:]
                v1 = _safe_dbfs(first_h)
                v2 = _safe_dbfs(second_h)
                if v1 is not None and v2 is not None:
                    diff = abs(v1 - v2)
                    report.one_sided = diff > ONE_SIDED_DIFF_DB

        report.analyzed = True
        logger.info(
            "Audio OK: dur=%dms silence=%.1f%% speech=%.1f%% gaps=%d "
            "vol=%.1fdBFS noise=%s mos=%s clips=%s",
            total_ms, report.silence_pct, report.speech_pct,
            len(notable), report.volume_dBFS or 0,
            report.noise_level, mos or "N/A", report.clipping,
        )

    except ImportError:
        report.error = "pydub not installed. Run: pip install pydub && brew install ffmpeg"
        logger.error(report.error)
    except MemoryError:
        report.error = "Audio file too large to process in memory"
        logger.error(report.error)
    except Exception as exc:
        report.error = f"Audio analysis error: {type(exc).__name__}: {str(exc)[:200]}"
        logger.error("Audio analysis failed: %s", exc)

    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_load(path: Path):
    """Load audio file — try multiple formats if extension is wrong."""
    from pydub import AudioSegment

    try:
        return AudioSegment.from_file(str(path))
    except Exception as e1:
        # Try explicit formats
        for fmt in ("mp3", "ogg", "wav", "flac", "m4a"):
            try:
                return AudioSegment.from_file(str(path), format=fmt)
            except Exception:
                pass
        logger.error("Could not load audio in any format: %s", e1)
        return None


def _safe_dbfs(audio) -> Optional[float]:
    """Return dBFS mean, handling -inf (complete silence)."""
    try:
        v = audio.dBFS
        if v == float("-inf"):
            return None
        return round(v, 1)
    except Exception:
        return None


def _safe_max_dbfs(audio) -> Optional[float]:
    try:
        v = audio.max_dBFS
        if v == float("-inf"):
            return None
        return round(v, 1)
    except Exception:
        return None


def _estimate_noise_floor(mono) -> Optional[float]:
    """
    Estimate background noise floor by analyzing the quietest 10% of frames.
    Splits audio into 100ms chunks, sorts by volume, takes quietest 10%.
    """
    try:
        chunk_ms   = 100
        total_ms   = len(mono)
        chunks     = [mono[i:i+chunk_ms] for i in range(0, total_ms - chunk_ms, chunk_ms)]
        if not chunks:
            return None
        vols       = sorted([c.dBFS for c in chunks if c.dBFS != float("-inf")])
        if not vols:
            return None
        quiet_ct   = max(1, len(vols) // 10)
        noise_floor = sum(vols[:quiet_ct]) / quiet_ct
        return round(noise_floor, 1)
    except Exception:
        return None


def _classify_noise(noise_floor: Optional[float]) -> str:
    """Classify background noise level from noise floor dBFS."""
    if noise_floor is None:
        return "unknown"
    if noise_floor < -60:
        return "quiet"
    if noise_floor < -45:
        return "moderate"
    if noise_floor < -30:
        return "noisy"
    return "very_noisy"


def _detect_ring_tone(segment) -> bool:
    """
    Heuristic: detect if audio starts with a ring tone pattern.
    Ring tones are periodic: ~1s on, ~2s off, repeating.
    Check if the first 6s has alternating loud/silent pattern.
    """
    try:
        from pydub.silence import detect_silence
        chunk_ms = 500
        chunks = [segment[i:i+chunk_ms] for i in range(0, len(segment) - chunk_ms, chunk_ms)]
        if len(chunks) < 6:
            return False

        # Count transitions between loud and quiet
        threshold = -35
        states = ["loud" if (c.dBFS if c.dBFS != float("-inf") else -100) > threshold else "silent"
                  for c in chunks]
        transitions = sum(1 for i in range(1, len(states)) if states[i] != states[i-1])

        # Ring tone: expects ~2-4 transitions in 6 seconds
        return 2 <= transitions <= 6 and "loud" in states and "silent" in states
    except Exception:
        return False
