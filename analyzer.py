"""
analyzer.py — Standalone post-call analysis replay tool
=========================================================
Re-analyze any saved call without making a new call.
Useful for:
  - Re-running analysis with a different Gemini model
  - Batch analyzing historical calls
  - Testing analysis logic
  - Analyzing a raw .mp3 file directly

Usage:
    python analyzer.py --latest
    python analyzer.py --call-uuid abc123
    python analyzer.py --report reports/CALL_abc123_20260324.json
    python analyzer.py --recording recordings/abc123.mp3
    python analyzer.py --batch --date 2026-03-24
    python analyzer.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("analyzer")

REPORTS_DIR    = Path("reports")
RECORDINGS_DIR = Path("recordings")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Post-call analysis replay tool")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest",           action="store_true",  help="Re-analyze most recent call")
    group.add_argument("--call-uuid",        type=str,             help="Re-analyze by call UUID prefix")
    group.add_argument("--report",           type=str,             help="Path to saved report JSON")
    group.add_argument("--recording",        type=str,             help="Path to .mp3 file (audio only)")
    group.add_argument("--batch",            action="store_true",  help="Batch analyze all calls")
    group.add_argument("--list",             action="store_true",  help="List all saved reports")

    parser.add_argument("--date",            type=str,             help="Filter by date e.g. 2026-03-24")
    parser.add_argument("--model",           type=str,             default="",
                        help="Override Gemini model e.g. gemini-1.5-pro")
    parser.add_argument("--transcript-only", action="store_true",  help="Skip audio analysis")
    parser.add_argument("--audio-only",      action="store_true",  help="Skip Gemini transcript analysis")

    args = parser.parse_args()

    # Override model if specified
    if args.model:
        import os
        os.environ["GEMINI_MODEL"] = args.model
        logger.info("Using model: %s", args.model)

    if args.list:
        _list_reports()
        return

    if args.latest:
        reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not reports:
            print("No reports found. Make a call first.")
            sys.exit(1)
        await _reanalyze_report(reports[0], args)
        return

    if args.call_uuid:
        matches = list(REPORTS_DIR.glob(f"*{args.call_uuid}*"))
        if not matches:
            # Try recordings
            rec_matches = list(RECORDINGS_DIR.glob(f"*{args.call_uuid}*"))
            if rec_matches:
                await _analyze_recording_only(rec_matches[0])
                return
            print(f"No report or recording found for UUID: {args.call_uuid}")
            sys.exit(1)
        await _reanalyze_report(max(matches, key=lambda f: f.stat().st_mtime), args)
        return

    if args.report:
        path = Path(args.report)
        if not path.exists():
            print(f"File not found: {args.report}")
            sys.exit(1)
        await _reanalyze_report(path, args)
        return

    if args.recording:
        path = Path(args.recording)
        if not path.exists():
            print(f"File not found: {args.recording}")
            sys.exit(1)
        await _analyze_recording_only(path)
        return

    if args.batch:
        await _batch_analyze(args.date, args)
        return


# ---------------------------------------------------------------------------
# Re-analyze a saved report
# ---------------------------------------------------------------------------

async def _reanalyze_report(report_path: Path, args: argparse.Namespace):
    logger.info("Re-analyzing: %s", report_path.name)

    try:
        data = json.loads(report_path.read_text())
    except Exception as exc:
        print(f"Failed to read report: {exc}")
        return

    transcript_lines = data.get("transcript", [])
    call_meta        = data.get("call_meta", {})
    recording_info   = data.get("recording", {})
    mp3_path         = recording_info.get("local_path")

    # Audio analysis
    from audio_analyzer import analyze_audio, AudioReport
    audio = AudioReport()
    if not args.transcript_only:
        mp3 = Path(mp3_path) if mp3_path else None
        if not mp3 or not mp3.exists():
            # Try to find by call_uuid
            call_uuid = data.get("call_uuid", "")
            candidates = list(RECORDINGS_DIR.glob(f"{call_uuid}*"))
            mp3 = candidates[0] if candidates else None

        audio = analyze_audio(
            mp3_path=mp3,
            mos=data.get("call_meta", {}).get("mos"),
            jitter=data.get("call_meta", {}).get("jitter"),
            duration=call_meta.get("duration_seconds"),
            billsec=call_meta.get("billsec"),
        )

    # Transcript analysis
    from gemini_analyzer import TranscriptAnalysis, analyze_transcript, compute_gate
    ta = TranscriptAnalysis(**data["transcript_analysis"]) if not args.audio_only else None

    if not args.audio_only:
        ta = await analyze_transcript(
            transcript=transcript_lines,
            from_number=call_meta.get("from", ""),
            to_number=call_meta.get("to", ""),
            duration_seconds=call_meta.get("duration_seconds", 0),
            mos=audio.mos,
            jitter=audio.jitter_ms,
        )

    gate = compute_gate(audio, ta, call_meta.get("duration_seconds", 0))

    # Update and re-save
    data["audio_analysis"]      = audio.model_dump()
    data["transcript_analysis"] = ta.model_dump()
    data["gate"]                = gate
    data["reanalyzed_at"]       = datetime.now(timezone.utc).isoformat()

    # Save as new file with _reanalyzed suffix
    new_path = report_path.with_stem(report_path.stem + "_reanalyzed")
    new_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Saved: %s", new_path)

    # Print
    _print_analysis(data, str(new_path))


# ---------------------------------------------------------------------------
# Analyze recording only (no transcript)
# ---------------------------------------------------------------------------

async def _analyze_recording_only(mp3_path: Path):
    logger.info("Analyzing audio: %s", mp3_path.name)
    from audio_analyzer import analyze_audio
    audio = analyze_audio(mp3_path=mp3_path)
    print(json.dumps(audio.model_dump(), indent=2))


# ---------------------------------------------------------------------------
# Batch analysis
# ---------------------------------------------------------------------------

async def _batch_analyze(date_filter: str | None, args: argparse.Namespace):
    all_reports = sorted(REPORTS_DIR.glob("*.json"),
                         key=lambda f: f.stat().st_mtime, reverse=True)

    if date_filter:
        all_reports = [r for r in all_reports if date_filter in r.name]

    if not all_reports:
        print(f"No reports found{' for ' + date_filter if date_filter else ''}.")
        return

    print(f"Batch analyzing {len(all_reports)} report(s)…\n")
    passed = failed = reviewed = 0

    for rpt in all_reports:
        try:
            data   = json.loads(rpt.read_text())
            verdict = data.get("gate", {}).get("verdict", "?")
            quality = data.get("transcript_analysis", {}).get("quality_scores", {}).get("overall", "?")
            uuid    = data.get("call_uuid", "?")[:8]
            print(f"  [{verdict:6}] Q:{quality:>3}  {uuid}  {rpt.name}")
            if verdict == "PASS":    passed += 1
            elif verdict == "FAIL":  failed += 1
            elif verdict == "REVIEW": reviewed += 1
        except Exception as exc:
            print(f"  [ERROR] {rpt.name}: {exc}")

    total = passed + failed + reviewed
    print(f"\n{'─'*50}")
    print(f"  PASS: {passed}/{total}  REVIEW: {reviewed}/{total}  FAIL: {failed}/{total}")
    if total > 0:
        print(f"  Pass rate: {passed/total*100:.1f}%")


# ---------------------------------------------------------------------------
# List reports
# ---------------------------------------------------------------------------

def _list_reports():
    reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not reports:
        print("No reports yet. Make a call first.")
        return

    print(f"\n{'FILENAME':<45} {'GATE':<8} {'Q':<5} {'DURATION':<10} {'FROM'}")
    print("─" * 90)
    for r in reports[:30]:
        try:
            d       = json.loads(r.read_text())
            gate    = d.get("gate", {}).get("verdict", "?")
            quality = d.get("transcript_analysis", {}).get("quality_scores", {}).get("overall", "?")
            dur     = d.get("call_meta", {}).get("duration_seconds", "?")
            frm     = d.get("call_meta", {}).get("from", "?")
            icon    = {"PASS": "✅", "REVIEW": "🟡", "FAIL": "❌"}.get(gate, "❓")
            print(f"  {r.name:<43} {icon}{gate:<7} {str(quality):<5} {str(dur)+'s':<10} {frm}")
        except Exception:
            print(f"  {r.name:<43} [unreadable]")


# ---------------------------------------------------------------------------
# Print formatted analysis
# ---------------------------------------------------------------------------

def _print_analysis(data: dict, saved_path: str):
    """Print analysis report in the same format as main.py."""
    # Import and reuse main.py's print function
    try:
        import importlib.util, sys as _sys
        spec = importlib.util.spec_from_file_location("main_module", Path(__file__).parent / "main.py")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod._print_report(data, saved_path)
    except Exception:
        # Fallback: simple JSON dump
        print(json.dumps(data.get("gate"), indent=2))
        print(json.dumps(data.get("transcript_analysis"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
