"""
analyzer.py — Robust standalone post-call analysis replay tool
===============================================================

Usage:
    python analyzer.py --latest
    python analyzer.py --call-uuid abc123
    python analyzer.py --report reports/CALL_abc123_20260324.json
    python analyzer.py --recording recordings/abc123.mp3
    python analyzer.py --batch [--date 2026-03-24]
    python analyzer.py --list
    python analyzer.py --stats
    python analyzer.py --compare CALL_aaa CALL_bbb
    python analyzer.py --watch          (live mode — re-runs on new reports)
    python analyzer.py --export csv     (export all reports to CSV)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(".env")

logging.basicConfig(
    level=logging.WARNING,    # quiet by default — only show errors + final output
    format="%(levelname)s  %(message)s",
)
logger = logging.getLogger("analyzer")

REPORTS_DIR    = Path(os.getenv("REPORTS_DIR",    "reports"))
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "recordings"))

REPORTS_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Post-call analysis replay tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python analyzer.py --latest
  python analyzer.py --call-uuid a6d9b22e
  python analyzer.py --report reports/CALL_abc.json
  python analyzer.py --recording recordings/abc.mp3
  python analyzer.py --batch --date 2026-03-25
  python analyzer.py --list
  python analyzer.py --stats
  python analyzer.py --compare CALL_aaa CALL_bbb
  python analyzer.py --watch
  python analyzer.py --export csv
        """,
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--latest",    action="store_true", help="Re-analyze most recent call")
    mode.add_argument("--call-uuid", metavar="UUID",      help="Re-analyze by UUID prefix")
    mode.add_argument("--report",    metavar="PATH",      help="Path to saved report JSON")
    mode.add_argument("--recording", metavar="PATH",      help="Analyze .mp3 file directly")
    mode.add_argument("--batch",     action="store_true", help="Batch process all calls")
    mode.add_argument("--list",      action="store_true", help="List all saved reports")
    mode.add_argument("--stats",     action="store_true", help="Aggregate statistics across all reports")
    mode.add_argument("--compare",   nargs=2, metavar=("A", "B"), help="Compare two calls side-by-side")
    mode.add_argument("--watch",     action="store_true", help="Watch for new reports and print them")
    mode.add_argument("--export",    choices=["csv", "json"], help="Export all reports to csv or json")

    p.add_argument("--date",            metavar="YYYY-MM-DD", help="Filter by date")
    p.add_argument("--model",           metavar="MODEL",      help="Override Gemini model")
    p.add_argument("--transcript-only", action="store_true",  help="Skip audio re-analysis")
    p.add_argument("--audio-only",      action="store_true",  help="Skip Gemini transcript analysis")
    p.add_argument("--no-save",         action="store_true",  help="Print result but don't save")
    p.add_argument("--verbose", "-v",   action="store_true",  help="Verbose logging")

    return p


async def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    if args.model:
        os.environ["GEMINI_MODEL"] = args.model
        print(f"Using model: {args.model}")

    try:
        if args.list:
            _cmd_list()
        elif args.stats:
            _cmd_stats()
        elif args.export:
            _cmd_export(args.export)
        elif args.watch:
            await _cmd_watch()
        elif args.compare:
            _cmd_compare(args.compare[0], args.compare[1])
        elif args.latest:
            await _cmd_single(_find_latest_report(), args)
        elif args.call_uuid:
            await _cmd_by_uuid(args.call_uuid, args)
        elif args.report:
            await _cmd_single(Path(args.report), args)
        elif args.recording:
            await _cmd_recording(Path(args.recording))
        elif args.batch:
            await _cmd_batch(args.date, args)
    except KeyboardInterrupt:
        print("\nInterrupted.")


# ---------------------------------------------------------------------------
# Single report analysis
# ---------------------------------------------------------------------------

async def _cmd_single(report_path: Optional[Path], args: argparse.Namespace):
    if not report_path:
        print("No reports found.")
        sys.exit(1)
    if not report_path.exists():
        print(f"File not found: {report_path}")
        sys.exit(1)

    print(f"\nAnalyzing: {report_path.name}")

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in {report_path}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR reading file: {e}")
        sys.exit(1)

    transcript = data.get("transcript", [])
    call_meta  = data.get("call_meta", {})

    # ── Audio ────────────────────────────────────────────────────────────
    from audio_analyzer import analyze_audio, AudioReport

    audio: AudioReport
    if args.transcript_only:
        # Reuse existing audio analysis
        try:
            audio = AudioReport(**data.get("audio_analysis", {}))
        except Exception:
            audio = AudioReport()
    else:
        mp3 = _find_recording(data)
        audio = analyze_audio(
            mp3_path=mp3,
            mos=call_meta.get("cost") and data.get("audio_analysis", {}).get("mos"),
            jitter=data.get("audio_analysis", {}).get("jitter_ms"),
            duration=call_meta.get("duration_seconds"),
            billsec=call_meta.get("billsec"),
        )
        # Preserve CDR metrics from original report
        orig_audio = data.get("audio_analysis", {})
        if not audio.mos:     audio.mos      = orig_audio.get("mos")
        if not audio.jitter_ms: audio.jitter_ms = orig_audio.get("jitter_ms")

    # ── Transcript (Gemini) ──────────────────────────────────────────────
    from gemini_analyzer import TranscriptAnalysis, analyze_transcript, compute_gate

    if args.audio_only:
        try:
            ta = TranscriptAnalysis(**data["transcript_analysis"])
        except Exception:
            from gemini_analyzer import _empty_analysis
            ta = _empty_analysis("audio_only_mode")
    else:
        print("Running Gemini analysis… ", end="", flush=True)
        t0 = time.time()
        ta = await analyze_transcript(
            transcript=transcript,
            from_number=call_meta.get("from", ""),
            to_number=call_meta.get("to", ""),
            duration_seconds=call_meta.get("duration_seconds", 0),
            mos=audio.mos,
            jitter=audio.jitter_ms,
        )
        print(f"done ({time.time()-t0:.1f}s)")

    # ── Gate ─────────────────────────────────────────────────────────────
    gate = compute_gate(audio, ta, call_meta.get("duration_seconds", 0))

    # ── Update data ───────────────────────────────────────────────────────
    data["audio_analysis"]      = audio.model_dump()
    data["transcript_analysis"] = ta.model_dump()
    data["gate"]                = gate
    data["reanalyzed_at"]       = datetime.now(timezone.utc).isoformat()
    data["analyzer_model"]      = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    # ── Save ──────────────────────────────────────────────────────────────
    if not args.no_save:
        stem     = report_path.stem.replace("_reanalyzed", "")
        new_path = report_path.parent / f"{stem}_reanalyzed.json"
        _safe_write(new_path, data)
        saved_path = str(new_path)
    else:
        saved_path = str(report_path) + " (not saved)"

    # ── Print ─────────────────────────────────────────────────────────────
    _print_report(data, saved_path)


async def _cmd_by_uuid(uuid_prefix: str, args: argparse.Namespace):
    # Search reports first
    matches = sorted(
        REPORTS_DIR.glob(f"*{uuid_prefix}*"),
        key=lambda f: f.stat().st_mtime, reverse=True,
    )
    if matches:
        await _cmd_single(matches[0], args)
        return

    # Try recordings
    rec = list(RECORDINGS_DIR.glob(f"*{uuid_prefix}*"))
    if rec:
        await _cmd_recording(rec[0])
        return

    print(f"No report or recording found matching: {uuid_prefix}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Recording-only analysis
# ---------------------------------------------------------------------------

async def _cmd_recording(mp3_path: Path):
    if not mp3_path.exists():
        print(f"File not found: {mp3_path}")
        sys.exit(1)

    print(f"\nAnalyzing audio: {mp3_path.name}")
    from audio_analyzer import analyze_audio
    audio = analyze_audio(mp3_path=mp3_path)

    if audio.error:
        print(f"Audio error: {audio.error}")
    else:
        print(_format_audio_section(audio))

    print(json.dumps(audio.model_dump(), indent=2, default=str))


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

async def _cmd_batch(date_filter: Optional[str], args: argparse.Namespace):
    reports = _get_all_reports(date_filter)
    if not reports:
        msg = f"No reports found"
        if date_filter:
            msg += f" for {date_filter}"
        print(msg)
        return

    print(f"\nBatch analyzing {len(reports)} report(s)…\n")
    results = []
    errors  = 0

    for i, rpt in enumerate(reports, 1):
        try:
            data    = json.loads(rpt.read_text(encoding="utf-8"))
            uuid    = (data.get("call_uuid") or "?")[:8]
            verdict = data.get("gate", {}).get("verdict", "?")
            quality = data.get("transcript_analysis", {}).get(
                "quality_scores", {}).get("overall", "?")
            dur     = data.get("call_meta", {}).get("duration_seconds", "?")
            frm     = data.get("call_meta", {}).get("from", "?")
            scam_ct = len(data.get("transcript_analysis", {}).get("scam_indicators", []))
            icon    = {"PASS": "✅", "REVIEW": "🟡", "FAIL": "❌"}.get(verdict, "❓")
            scam_str = f" ⚠️{scam_ct}scam" if scam_ct else ""
            print(f"  {i:>3}. [{icon}{verdict:<6}] Q:{str(quality):>3}  {str(dur):>4}s  "
                  f"{uuid}  {frm}{scam_str}  {rpt.name}")
            results.append({"verdict": verdict, "quality": quality, "scam_ct": scam_ct})
        except Exception as exc:
            print(f"  {i:>3}. [ERROR] {rpt.name}: {exc}")
            errors += 1

    _print_batch_summary(results, errors)


def _print_batch_summary(results: list, errors: int):
    total    = len(results)
    passed   = sum(1 for r in results if r["verdict"] == "PASS")
    reviewed = sum(1 for r in results if r["verdict"] == "REVIEW")
    failed   = sum(1 for r in results if r["verdict"] == "FAIL")
    with_scam= sum(1 for r in results if r.get("scam_ct", 0) > 0)

    qualities = [r["quality"] for r in results
                 if isinstance(r.get("quality"), int)]
    avg_q = round(sum(qualities) / len(qualities), 1) if qualities else "N/A"

    print(f"\n{'─'*60}")
    print(f"  Total: {total}  ✅PASS: {passed}  🟡REVIEW: {reviewed}  "
          f"❌FAIL: {failed}  💥Errors: {errors}")
    print(f"  Avg quality: {avg_q}/100   Scam calls: {with_scam}")
    if total > 0:
        print(f"  Pass rate: {passed/total*100:.1f}%   "
              f"Flag rate: {(reviewed+failed)/total*100:.1f}%")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def _cmd_list():
    reports = _get_all_reports()
    if not reports:
        print("No reports yet. Make a call first.")
        return

    print(f"\n{'#':<4} {'UUID':>8}  {'GATE':<7} {'Q':>3}  {'DUR':>5}  "
          f"{'SCAM':>4}  {'FROM':<17}  {'DATE'}")
    print("─" * 90)

    for i, r in enumerate(reports[:50], 1):
        try:
            d       = json.loads(r.read_text(encoding="utf-8"))
            uuid    = (d.get("call_uuid") or "?")[:8]
            gate    = d.get("gate", {}).get("verdict", "?")
            quality = d.get("transcript_analysis", {}).get("quality_scores", {}).get("overall", "?")
            dur     = d.get("call_meta", {}).get("duration_seconds", "?")
            frm     = (d.get("call_meta", {}).get("from") or "?")[:17]
            scam_ct = len(d.get("transcript_analysis", {}).get("scam_indicators", []))
            icon    = {"PASS": "✅", "REVIEW": "🟡", "FAIL": "❌"}.get(gate, "❓")
            ts      = (d.get("generated_at") or "")[:10]
            scam_s  = f"  ⚠️{scam_ct}" if scam_ct else "    "
            print(f"  {i:>2}. {uuid}  {icon}{gate:<6} {str(quality):>3}  "
                  f"{str(dur):>4}s{scam_s}  {frm:<17}  {ts}")
        except Exception:
            print(f"  {i:>2}. [unreadable] {r.name}")

    if len(reports) > 50:
        print(f"\n  … {len(reports) - 50} more reports not shown")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _cmd_stats():
    reports = _get_all_reports()
    if not reports:
        print("No reports yet.")
        return

    all_data = []
    for r in reports:
        try:
            all_data.append(json.loads(r.read_text(encoding="utf-8")))
        except Exception:
            pass

    if not all_data:
        print("No readable reports.")
        return

    verdicts  = [d.get("gate", {}).get("verdict", "?") for d in all_data]
    qualities = [d.get("transcript_analysis", {}).get("quality_scores", {}).get("overall")
                 for d in all_data if isinstance(
                     d.get("transcript_analysis", {}).get("quality_scores", {}).get("overall"), int)]
    durations = [d.get("call_meta", {}).get("duration_seconds")
                 for d in all_data if isinstance(d.get("call_meta", {}).get("duration_seconds"), (int, float))]
    mos_vals  = [d.get("audio_analysis", {}).get("mos")
                 for d in all_data if isinstance(d.get("audio_analysis", {}).get("mos"), (int, float))]
    scam_calls = [d for d in all_data
                  if len(d.get("transcript_analysis", {}).get("scam_indicators", [])) > 0]
    resolutions = [d.get("transcript_analysis", {}).get("call_resolution", "?")
                   for d in all_data]

    total = len(all_data)
    w = 50

    print(f"\n╔{'═'*w}╗")
    print(f"║{'AGGREGATE STATISTICS':^{w}}║")
    print(f"╠{'═'*w}╣")
    print(f"║  {'Total reports:':<24} {total:<{w-27}}║")
    print(f"╠{'═'*w}╣")

    # Verdicts
    for v, icon in [("PASS","✅"),("REVIEW","🟡"),("FAIL","❌")]:
        ct  = verdicts.count(v)
        pct = round(ct/total*100, 1) if total else 0
        bar = "█" * int(pct // 4)
        print(f"║  {icon} {v:<8} {ct:>4} ({pct:>5.1f}%)  {bar:<{w-28}}║")

    print(f"╠{'═'*w}╣")

    # Quality
    if qualities:
        print(f"║  {'Quality (avg):':<24} {sum(qualities)/len(qualities):.1f}/100{'':<{w-34}}║")
        print(f"║  {'Quality (min):':<24} {min(qualities)}/100{'':<{w-30}}║")
        print(f"║  {'Quality (max):':<24} {max(qualities)}/100{'':<{w-30}}║")

    # Duration
    if durations:
        avg_dur = int(sum(durations)/len(durations))
        print(f"║  {'Avg duration:':<24} {avg_dur//60}m {avg_dur%60}s{'':<{w-32}}║")

    # MOS
    if mos_vals:
        print(f"║  {'Avg MOS:':<24} {sum(mos_vals)/len(mos_vals):.2f}/5.0{'':<{w-32}}║")

    # Scam
    print(f"║  {'Scam indicators:':<24} {len(scam_calls)} calls ({round(len(scam_calls)/total*100,1)}%){'':<{w-40}}║")

    # Resolutions
    print(f"╠{'═'*w}╣")
    print(f"║  {'RESOLUTIONS':<{w-4}}║")
    for res in ["RESOLVED","UNRESOLVED","ABANDONED","ESCALATED","TRANSFERRED"]:
        ct = resolutions.count(res)
        if ct > 0:
            print(f"║    {res:<20} {ct:>4} ({round(ct/total*100,1):>5.1f}%){'':<{w-38}}║")

    # Top scam patterns
    all_scam = []
    for d in all_data:
        all_scam.extend(d.get("transcript_analysis", {}).get("scam_indicators", []))
    if all_scam:
        from collections import Counter
        top = Counter(all_scam).most_common(5)
        print(f"╠{'═'*w}╣")
        print(f"║  {'TOP SCAM PATTERNS':<{w-4}}║")
        for pattern, count in top:
            short = pattern[:35]
            print(f"║    {short:<35} x{count:<{w-43}}║")

    print(f"╚{'═'*w}╝\n")


# ---------------------------------------------------------------------------
# Compare two calls
# ---------------------------------------------------------------------------

def _cmd_compare(uuid_a: str, uuid_b: str):
    def load(uuid):
        matches = sorted(REPORTS_DIR.glob(f"*{uuid}*"),
                         key=lambda f: f.stat().st_mtime, reverse=True)
        if not matches:
            print(f"No report found for: {uuid}")
            return None
        try:
            return json.loads(matches[0].read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Error reading {matches[0]}: {e}")
            return None

    a = load(uuid_a)
    b = load(uuid_b)
    if not a or not b:
        return

    w = 28
    print(f"\n{'COMPARISON':^{w*2+7}}")
    print(f"{'─'*(w*2+7)}")
    print(f"{'Field':<20} {'Call A':^{w}} {'Call B':^{w}}")
    print(f"{'─'*20} {'─'*w} {'─'*w}")

    def cmp(label, va, vb):
        print(f"  {label:<18} {str(va)[:w]:<{w}} {str(vb)[:w]:<{w}}")

    ta = a.get("transcript_analysis", {})
    tb = b.get("transcript_analysis", {})
    aa = a.get("audio_analysis", {})
    ab = b.get("audio_analysis", {})
    ga = a.get("gate", {})
    gb = b.get("gate", {})
    ma = a.get("call_meta", {})
    mb = b.get("call_meta", {})

    cmp("UUID",       (a.get("call_uuid") or "?")[:8], (b.get("call_uuid") or "?")[:8])
    cmp("From",       ma.get("from","?"),  mb.get("from","?"))
    cmp("Duration",   f"{ma.get('duration_seconds','?')}s", f"{mb.get('duration_seconds','?')}s")
    cmp("Gate",       ga.get("verdict","?"), gb.get("verdict","?"))
    cmp("Quality",    f"{ta.get('quality_scores',{}).get('overall','?')}/100",
                      f"{tb.get('quality_scores',{}).get('overall','?')}/100")
    cmp("Resolution", ta.get("call_resolution","?"), tb.get("call_resolution","?"))
    cmp("MOS",        f"{aa.get('mos','N/A')}/5.0",  f"{ab.get('mos','N/A')}/5.0")
    cmp("Jitter",     f"{aa.get('jitter_ms','N/A')}ms", f"{ab.get('jitter_ms','N/A')}ms")
    cmp("Silence",    f"{aa.get('silence_pct','N/A')}%", f"{ab.get('silence_pct','N/A')}%")
    cmp("Intent",     (ta.get("caller_intent") or "?")[:w], (tb.get("caller_intent") or "?")[:w])
    cmp("Sentiment",  (ta.get("caller_sentiment") or "?")[:w], (tb.get("caller_sentiment") or "?")[:w])
    cmp("Scam flags", len(ta.get("scam_indicators",[])), len(tb.get("scam_indicators",[])))

    print(f"{'─'*(w*2+7)}")
    # Winner
    qa = ta.get("quality_scores",{}).get("overall") or 0
    qb = tb.get("quality_scores",{}).get("overall") or 0
    if qa > qb:
        print(f"  Higher quality: Call A  ({qa} vs {qb})")
    elif qb > qa:
        print(f"  Higher quality: Call B  ({qb} vs {qa})")
    else:
        print(f"  Quality tied: {qa}/100")


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

async def _cmd_watch():
    print("Watching for new reports… (Ctrl+C to stop)\n")
    seen = {r.name for r in REPORTS_DIR.glob("*.json")}

    while True:
        await asyncio.sleep(3)
        current = {r.name for r in REPORTS_DIR.glob("*.json")}
        new     = current - seen
        for name in sorted(new):
            path = REPORTS_DIR / name
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _print_report(data, str(path))
            except Exception as exc:
                print(f"Error reading {name}: {exc}")
        seen = current


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _cmd_export(fmt: str):
    reports = _get_all_reports()
    if not reports:
        print("No reports to export.")
        return

    all_data = []
    for r in reports:
        try:
            all_data.append(json.loads(r.read_text(encoding="utf-8")))
        except Exception:
            pass

    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out  = REPORTS_DIR / f"export_{ts}.{fmt}"

    if fmt == "json":
        _safe_write(out, all_data)
        print(f"Exported {len(all_data)} reports → {out}")

    elif fmt == "csv":
        fields = [
            "call_uuid", "generated_at", "from", "to", "duration_seconds",
            "billsec", "cost", "currency", "mos", "jitter_ms", "silence_pct",
            "speech_pct", "noise_level", "clipping", "ring_detected",
            "gate_verdict", "gate_flags",
            "overall", "clarity", "resolution", "empathy", "efficiency",
            "confidence", "caller_intent", "call_resolution",
            "caller_sentiment", "language_detected",
            "action_items_count", "unanswered_count", "scam_count",
            "scam_indicators",
        ]
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for d in all_data:
                meta  = d.get("call_meta", {})
                audio = d.get("audio_analysis", {})
                ta    = d.get("transcript_analysis", {})
                gate  = d.get("gate", {})
                qs    = ta.get("quality_scores", {})
                row   = {
                    "call_uuid":          d.get("call_uuid", ""),
                    "generated_at":       d.get("generated_at", ""),
                    "from":               meta.get("from", ""),
                    "to":                 meta.get("to", ""),
                    "duration_seconds":   meta.get("duration_seconds", ""),
                    "billsec":            meta.get("billsec", ""),
                    "cost":               meta.get("cost", ""),
                    "currency":           meta.get("currency", "INR"),
                    "mos":                audio.get("mos", ""),
                    "jitter_ms":          audio.get("jitter_ms", ""),
                    "silence_pct":        audio.get("silence_pct", ""),
                    "speech_pct":         audio.get("speech_pct", ""),
                    "noise_level":        audio.get("noise_level", ""),
                    "clipping":           audio.get("clipping", ""),
                    "ring_detected":      audio.get("ring_detected", ""),
                    "gate_verdict":       gate.get("verdict", ""),
                    "gate_flags":         "|".join(gate.get("flags", [])),
                    "overall":            qs.get("overall", ""),
                    "clarity":            qs.get("clarity", ""),
                    "resolution":         qs.get("resolution", ""),
                    "empathy":            qs.get("empathy", ""),
                    "efficiency":         qs.get("efficiency", ""),
                    "confidence":         ta.get("analysis_confidence", ""),
                    "caller_intent":      ta.get("caller_intent", ""),
                    "call_resolution":    ta.get("call_resolution", ""),
                    "caller_sentiment":   ta.get("caller_sentiment", ""),
                    "language_detected":  ta.get("language_detected", ""),
                    "action_items_count": len(ta.get("action_items", [])),
                    "unanswered_count":   len(ta.get("unanswered_questions", [])),
                    "scam_count":         len(ta.get("scam_indicators", [])),
                    "scam_indicators":    " | ".join(ta.get("scam_indicators", [])),
                }
                w.writerow(row)

        print(f"Exported {len(all_data)} reports → {out}")


# ---------------------------------------------------------------------------
# Print report
# ---------------------------------------------------------------------------

def _print_report(data: dict, saved_path: str):
    """Import and run main.py's _print_report, with fallback."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "main_mod", Path(__file__).parent / "main.py"
        )
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            mod._print_report(data, saved_path)
            return
    except Exception as exc:
        logger.debug("Could not load main._print_report: %s", exc)

    # Fallback: minimal report
    gate    = data.get("gate", {})
    ta      = data.get("transcript_analysis", {})
    icon    = {"PASS": "✅", "REVIEW": "🟡", "FAIL": "❌"}.get(gate.get("verdict",""), "❓")
    quality = ta.get("quality_scores", {}).get("overall", "?")
    print(f"\n{icon} {gate.get('verdict','?')}  Quality:{quality}/100")
    print(f"   Intent    : {ta.get('caller_intent','?')}")
    print(f"   Resolution: {ta.get('call_resolution','?')}")
    print(f"   Reason    : {gate.get('reason','?')}")
    print(f"   Report    : {saved_path}")
    if ta.get("scam_indicators"):
        print(f"   ⚠️ Scam   : {', '.join(ta['scam_indicators'][:3])}")


def _format_audio_section(audio) -> str:
    lines = []
    if audio.mos:        lines.append(f"MOS:     {audio.mos}/5.0")
    if audio.jitter_ms:  lines.append(f"Jitter:  {audio.jitter_ms}ms")
    if audio.silence_pct is not None: lines.append(f"Silence: {audio.silence_pct}%")
    if audio.speech_pct is not None:  lines.append(f"Speech:  {audio.speech_pct}%")
    if audio.noise_level: lines.append(f"Noise:   {audio.noise_level}")
    if audio.clipping:    lines.append("⚠️  Clipping detected")
    if audio.ring_detected: lines.append("📞 Ring tone detected (possibly unanswered)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _get_all_reports(date_filter: Optional[str] = None) -> list[Path]:
    reports = sorted(
        REPORTS_DIR.glob("*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if date_filter:
        reports = [r for r in reports if date_filter in r.name or date_filter in r.read_text()[:100]]
    return reports


def _find_latest_report() -> Optional[Path]:
    reports = _get_all_reports()
    return reports[0] if reports else None


def _find_recording(data: dict) -> Optional[Path]:
    """Find the recording file for a report."""
    # Try local_path first
    local = data.get("recording", {}).get("local_path")
    if local and Path(local).exists():
        return Path(local)

    # Try recordings/ dir by call_uuid
    uuid = data.get("call_uuid", "")
    if uuid:
        for ext in ("mp3", "ogg", "wav"):
            p = RECORDINGS_DIR / f"{uuid}.{ext}"
            if p.exists():
                return p
        # Partial match
        matches = list(RECORDINGS_DIR.glob(f"{uuid[:8]}*"))
        if matches:
            return matches[0]

    return None


def _safe_write(path: Path, data) -> bool:
    """Atomic write — write to temp then rename."""
    try:
        tmp = path.with_suffix(".tmp")
        content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as exc:
        logger.error("Failed to write %s: %s", path, exc)
        return False


if __name__ == "__main__":
    asyncio.run(main())
