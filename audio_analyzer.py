"""
audio_analyzer.py — pydub-based audio quality analysis
=======================================================
Analyzes the downloaded .mp3 recording for:
  - Silence percentage
  - Long silence gaps (> 3s)
  - Volume (dBFS)
  - Audio clipping detection
  - One-sided audio detection

Also incorporates MOS and Jitter from the Vobiz Hangup CDR payload.

Requires: pydub + ffmpeg
Install ffmpeg: brew install ffmpeg  (macOS)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger("post-call.audio")

# Silence detection thresholds
SILENCE_THRESH_DBF  = -40    # dBFS below which a frame is "silent"
MIN_SILENCE_LEN_MS  = 500    # minimum silence duration to count (ms)
LONG_SILENCE_MS     = 3000   # gaps >= this are "notable" (3 seconds)


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class SilenceGap(BaseModel):
    at:          str  # approximate timestamp e.g. "0:32"
    duration_ms: int  # gap duration in milliseconds


class AudioReport(BaseModel):
    analyzed:        bool  = False
    error:           Optional[str] = None

    # From .mp3 analysis (pydub)
    file_duration_ms:  Optional[int]   = None
    silence_pct:       Optional[float] = None   # % of audio that is silent
    silence_gaps:      list[SilenceGap] = []    # gaps > LONG_SILENCE_MS
    volume_dBFS:       Optional[float] = None   # mean amplitude
    max_dBFS:          Optional[float] = None   # peak amplitude
    clipping:          bool = False             # True if max_dBFS > -0.5
    low_volume:        bool = False             # True if mean dBFS < -30
    one_sided:         bool = False             # True if only one half has audio

    # From Vobiz CDR (always populated when available)
    mos:               Optional[float] = None   # Mean Opinion Score 1-5
    jitter_ms:         Optional[int]   = None   # Network jitter in ms
    duration_seconds:  Optional[int]   = None   # Total call duration
    billsec:           Optional[int]   = None   # Billable seconds


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def analyze_audio(
    mp3_path: Path | str | None,
    mos:      float | None = None,
    jitter:   int   | None = None,
    duration: int   | None = None,
    billsec:  int   | None = None,
) -> AudioReport:
    """
    Analyze audio file and combine with Vobiz CDR quality metrics.
    If mp3_path is None or unavailable, still returns CDR metrics.
    """
    report = AudioReport(
        mos=mos,
        jitter_ms=jitter,
        duration_seconds=duration,
        billsec=billsec,
    )

    if not mp3_path or not Path(mp3_path).exists():
        logger.warning("No audio file available — CDR metrics only")
        return report

    try:
        from pydub import AudioSegment
        from pydub.silence import detect_silence

        audio = AudioSegment.from_file(str(mp3_path))
        total_ms = len(audio)

        report.file_duration_ms = total_ms
        report.volume_dBFS      = round(audio.dBFS, 1)
        report.max_dBFS         = round(audio.max_dBFS, 1)
        report.clipping         = audio.max_dBFS > -0.5
        report.low_volume       = audio.dBFS < -30

        # Silence detection
        silent_ranges = detect_silence(
            audio,
            min_silence_len=MIN_SILENCE_LEN_MS,
            silence_thresh=SILENCE_THRESH_DBF,
        )

        total_silent_ms = sum(end - start for start, end in silent_ranges)
        report.silence_pct = round((total_silent_ms / total_ms) * 100, 1) if total_ms > 0 else 0.0

        # Notable gaps (> 3s)
        notable = []
        for start_ms, end_ms in silent_ranges:
            gap_ms = end_ms - start_ms
            if gap_ms >= LONG_SILENCE_MS:
                secs   = start_ms // 1000
                at_str = f"{secs // 60}:{secs % 60:02d}"
                notable.append(SilenceGap(at=at_str, duration_ms=gap_ms))

        report.silence_gaps = notable[:10]  # cap at 10 for report size

        # One-sided audio detection
        # Compare energy in first half vs second half
        # If one half is nearly silent, likely one-sided
        if total_ms > 4000:
            half     = total_ms // 2
            first_h  = audio[:half]
            second_h = audio[half:]
            diff     = abs(first_h.dBFS - second_h.dBFS)
            if diff > 20:   # more than 20dBFS difference between halves
                report.one_sided = True

        report.analyzed = True
        logger.info(
            "Audio analysis: duration=%dms silence=%.1f%% gaps=%d volume=%.1fdBFS mos=%s",
            total_ms, report.silence_pct, len(notable),
            report.volume_dBFS, mos or "N/A"
        )

    except ImportError:
        report.error = "pydub not installed. Run: pip install pydub"
        logger.error("pydub not available: %s", report.error)
    except Exception as exc:
        report.error = str(exc)
        logger.error("Audio analysis failed: %s", exc)

    return report
