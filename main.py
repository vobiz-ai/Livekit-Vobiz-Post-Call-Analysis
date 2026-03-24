"""
main.py — Post-Call Analysis Server
=====================================
FastAPI server that:
  1. Receives Vobiz SIP trunk webhooks (CallInitiated + Hangup)
  2. On Hangup: fetches recording from Vobiz API, downloads .mp3
  3. Runs audio analysis (pydub) — silence, gaps, volume, clipping
  4. Runs transcript analysis (Gemini 2.0 Flash) — intent, sentiment, quality
  5. Computes production gate verdict: PASS / REVIEW / FAIL
  6. Saves JSON report to reports/ and prints formatted summary

Run:
    uvicorn main:app --reload --port 9000

Set Vobiz trunk webhook URL to:
    https://<ngrok-url>/webhook/vobiz
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from gemini_analyzer import TranscriptAnalysis, analyze_transcript, compute_gate
from audio_analyzer import AudioReport, analyze_audio

load_dotenv(".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
)
logger = logging.getLogger("post-call.main")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
RECORDINGS_DIR = Path("recordings")
REPORTS_DIR    = Path("reports")
RECORDINGS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Vobiz credentials
# ---------------------------------------------------------------------------
VOBIZ_AUTH_ID    = os.getenv("VOBIZ_AUTH_ID", "")
VOBIZ_AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "")
VOBIZ_API_BASE   = "https://api.vobiz.ai/api/v1"

RECORDING_WAIT_SECS        = int(os.getenv("RECORDING_WAIT_SECS", "8"))
RECORDING_RETRIES          = int(os.getenv("RECORDING_RETRIES", "3"))
RECORDING_RETRY_DELAY_SECS = int(os.getenv("RECORDING_RETRY_DELAY_SECS", "5"))

# ---------------------------------------------------------------------------
# In-memory stores (agent.py POSTs to /internal/transcript)
# ---------------------------------------------------------------------------
# call_uuid → { sip_call_id, room_name, lines: [{speaker, text, timestamp}] }
call_store:       dict[str, dict] = {}
# sip_call_id → call_uuid  (for cross-referencing)
sip_to_uuid:      dict[str, str]  = {}
# room_name → call_uuid
room_to_uuid:     dict[str, str]  = {}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Post-Call Analysis", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------------------
# Vobiz Webhook  — the main entry point
# ---------------------------------------------------------------------------

@app.post("/webhook/vobiz")
async def vobiz_webhook(request: Request):
    """Receives CallInitiated and Hangup events from Vobiz SIP trunk."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    event      = body.get("Event", "")
    call_uuid  = body.get("CallUUID", "")
    sip_call_id = body.get("SIPCallID", "")

    logger.info("Webhook: Event=%s CallUUID=%s", event, call_uuid)

    if event == "CallInitiated":
        _handle_call_initiated(body)

    elif event == "Hangup":
        # Run full pipeline in background — respond to Vobiz immediately
        asyncio.create_task(_run_analysis_pipeline(body))

    return {"ok": True}


def _handle_call_initiated(body: dict):
    call_uuid   = body.get("CallUUID", "")
    sip_call_id = body.get("SIPCallID", "")
    allowed     = body.get("Allowed", True)

    call_store[call_uuid] = {
        "call_uuid":   call_uuid,
        "sip_call_id": sip_call_id,
        "from":        body.get("From", ""),
        "to":          body.get("To", ""),
        "direction":   body.get("Direction", ""),
        "initiated_at": body.get("Timestamp", ""),
        "allowed":     allowed,
        "reason":      body.get("Reason", ""),
        "lines":       [],
        "room_name":   None,
    }

    if sip_call_id:
        sip_to_uuid[sip_call_id] = call_uuid

    if not allowed:
        logger.warning("Call REJECTED: %s — %s", call_uuid, body.get("Reason"))
        _save_rejection_report(body)


# ---------------------------------------------------------------------------
# Full analysis pipeline (runs after Hangup)
# ---------------------------------------------------------------------------

async def _run_analysis_pipeline(hangup: dict):
    call_uuid   = hangup.get("CallUUID", "")
    sip_call_id = hangup.get("SIPCallID", "")
    duration    = hangup.get("Duration", 0)
    billsec     = hangup.get("Billsec", 0)
    mos         = hangup.get("MOS", None)
    jitter      = hangup.get("Jitter", None)
    cost        = hangup.get("Cost", 0)
    currency    = hangup.get("Currency", "INR")
    from_num    = hangup.get("From", "")
    to_num      = hangup.get("To", "")

    logger.info("Pipeline started: CallUUID=%s duration=%ds", call_uuid, duration)

    # 1. Fetch transcript from store
    record      = call_store.get(call_uuid, {})
    sip_id      = record.get("sip_call_id") or sip_call_id
    transcript  = record.get("lines", [])

    if not transcript:
        # Try matching by sip_call_id
        matched_uuid = sip_to_uuid.get(sip_id)
        if matched_uuid and matched_uuid != call_uuid:
            transcript = call_store.get(matched_uuid, {}).get("lines", [])
            logger.info("Found transcript via sip_call_id match: %d lines", len(transcript))

    logger.info("Transcript lines: %d", len(transcript))

    # 2. Fetch recording from Vobiz
    mp3_path = None
    recording_meta = None
    if VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN:
        logger.info("Waiting %ds for recording to be ready…", RECORDING_WAIT_SECS)
        await asyncio.sleep(RECORDING_WAIT_SECS)
        recording_meta = await _fetch_recording(call_uuid)
        if recording_meta:
            mp3_path = await _download_recording(recording_meta["recording_url"], call_uuid)
    else:
        logger.warning("VOBIZ_AUTH_ID/TOKEN not set — skipping recording download")

    # 3. Audio analysis
    audio: AudioReport = analyze_audio(mp3_path, mos=mos, jitter=jitter,
                                        duration=duration, billsec=billsec)

    # 4. Transcript analysis via Gemini
    ta: TranscriptAnalysis = await analyze_transcript(
        transcript=transcript,
        from_number=from_num,
        to_number=to_num,
        duration_seconds=duration,
        mos=mos,
        jitter=jitter,
    )

    # 5. Gate verdict
    gate = compute_gate(audio, ta, duration)

    # 6. Build + save report
    report = _build_report(
        call_uuid=call_uuid,
        hangup=hangup,
        audio=audio,
        transcript_analysis=ta,
        gate=gate,
        transcript_lines=transcript,
        mp3_path=str(mp3_path) if mp3_path else None,
        recording_meta=recording_meta,
    )

    ts    = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = REPORTS_DIR / f"CALL_{call_uuid[:8]}_{ts}.json"
    fname.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Report saved: %s", fname)

    # 7. Print formatted report
    _print_report(report, str(fname))

    # Clean up store
    call_store.pop(call_uuid, None)


# ---------------------------------------------------------------------------
# Vobiz Recording API
# ---------------------------------------------------------------------------

async def _fetch_recording(call_uuid: str, attempt: int = 1) -> dict | None:
    """Fetch recording metadata from Vobiz by call_uuid."""
    url     = f"{VOBIZ_API_BASE}/Account/{VOBIZ_AUTH_ID}/Recording/"
    headers = {"X-Auth-ID": VOBIZ_AUTH_ID, "X-Auth-Token": VOBIZ_AUTH_TOKEN}
    params  = {"call_uuid": call_uuid}

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url, headers=headers, params=params,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    objects = data.get("objects", [])
                    if objects:
                        rec = objects[0]
                        logger.info("Recording found: %s  format=%s  duration=%sms",
                                    rec.get("recording_id"), rec.get("recording_format"),
                                    rec.get("recording_duration_ms"))
                        return rec
                    else:
                        logger.warning("Recording not ready yet (attempt %d/%d)",
                                       attempt, RECORDING_RETRIES)
                        if attempt < RECORDING_RETRIES:
                            await asyncio.sleep(RECORDING_RETRY_DELAY_SECS)
                            return await _fetch_recording(call_uuid, attempt + 1)
                else:
                    logger.error("Vobiz Recording API: status=%d", resp.status)
    except Exception as exc:
        logger.error("Error fetching recording: %s", exc)

    return None


async def _download_recording(url: str, call_uuid: str) -> Path | None:
    """Download .mp3 from Vobiz to local recordings/ folder."""
    ext  = url.rsplit(".", 1)[-1] if "." in url else "mp3"
    path = RECORDINGS_DIR / f"{call_uuid}.{ext}"

    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    path.write_bytes(await resp.read())
                    size_kb = path.stat().st_size // 1024
                    logger.info("Recording downloaded: %s  (%dKB)", path, size_kb)
                    return path
                else:
                    logger.error("Download failed: status=%d", resp.status)
    except Exception as exc:
        logger.error("Error downloading recording: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_report(
    call_uuid: str,
    hangup: dict,
    audio: "AudioReport",
    transcript_analysis: "TranscriptAnalysis",
    gate: dict,
    transcript_lines: list,
    mp3_path: str | None,
    recording_meta: dict | None,
) -> dict:
    return {
        "call_uuid":    call_uuid,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "call_meta": {
            "from":        hangup.get("From"),
            "to":          hangup.get("To"),
            "direction":   hangup.get("Direction"),
            "start_time":  hangup.get("StartTime"),
            "end_time":    hangup.get("EndTime"),
            "duration_seconds": hangup.get("Duration"),
            "billsec":     hangup.get("Billsec"),
            "ring_time":   hangup.get("RingTime"),
            "cost":        hangup.get("Cost"),
            "currency":    hangup.get("Currency"),
            "sip_call_id": hangup.get("SIPCallID"),
            "trunk_id":    hangup.get("TrunkID"),
            "reason":      hangup.get("Reason"),
        },
        "recording": {
            "local_path":   mp3_path,
            "recording_id": recording_meta.get("recording_id") if recording_meta else None,
            "format":       recording_meta.get("recording_format") if recording_meta else None,
            "duration_ms":  recording_meta.get("recording_duration_ms") if recording_meta else None,
            "url":          recording_meta.get("recording_url") if recording_meta else None,
        },
        "audio_analysis":      audio.model_dump(),
        "transcript":          transcript_lines,
        "transcript_analysis": transcript_analysis.model_dump(),
        "gate":                gate,
    }


def _save_rejection_report(body: dict):
    call_uuid = body.get("CallUUID", "unknown")
    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname     = REPORTS_DIR / f"REJECTED_{call_uuid[:8]}_{ts}.json"
    report = {
        "call_uuid":    call_uuid,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event":        "CallRejected",
        "from":         body.get("From"),
        "to":           body.get("To"),
        "reason":       body.get("Reason"),
        "gate":         {"verdict": "FAIL", "flags": ["call_rejected"], "reason": body.get("Reason", "")},
    }
    fname.write_text(json.dumps(report, indent=2), encoding="utf-8")
    logger.info("Rejection report saved: %s", fname)


# ---------------------------------------------------------------------------
# Console output — formatted report
# ---------------------------------------------------------------------------

def _print_report(report: dict, saved_path: str):
    meta   = report["call_meta"]
    audio  = report["audio_analysis"]
    ta     = report["transcript_analysis"]
    gate   = report["gate"]
    scores = ta.get("quality_scores", {})

    verdict_icon = {"PASS": "✅", "REVIEW": "🟡", "FAIL": "❌"}.get(gate["verdict"], "❓")
    mos_icon     = "✅" if (audio.get("mos") or 0) >= 3.5 else "⚠️" if (audio.get("mos") or 0) >= 2.0 else "❌"
    jitter_icon  = "✅" if (audio.get("jitter_ms") or 0) <= 50 else "⚠️"
    silence_icon = "✅" if audio.get("silence_pct", 0) <= 30 else "⚠️" if audio.get("silence_pct", 0) <= 60 else "❌"
    res_icon     = "✅" if ta.get("call_resolution") == "RESOLVED" else "⚠️"

    dur = meta.get("duration_seconds") or 0
    dur_str = f"{dur//60}m {dur%60}s"

    w = 56
    line  = "═" * w
    hline = "─" * w

    def row(label, value): print(f"║  {label:<16} {str(value):<{w-20}}║")

    print(f"\n╔{line}╗")
    print(f"║{'POST-CALL ANALYSIS  —  PRODUCTION GATE':^{w}}║")
    print(f"╠{line}╣")
    row("CallUUID",   report["call_uuid"][:36])
    row("From → To",  f"{meta.get('from')} → {meta.get('to')}")
    row("Duration",   f"{dur_str}  (Billable: {meta.get('billsec','?')}s)")
    row("Cost",       f"{meta.get('cost', '?')} {meta.get('currency', 'INR')}")
    row("Gate",       f"{verdict_icon} {gate['verdict']}  (Quality: {scores.get('overall','?')}/100)")

    if gate.get("flags"):
        row("Flags",  ", ".join(gate["flags"]))

    print(f"╠{line}╣")
    print(f"║  {'AUDIO QUALITY':<{w-4}}║")
    print(f"║  {hline}  ║")
    row("MOS Score",  f"{audio.get('mos','N/A')} / 5.0  {mos_icon}")
    row("Jitter",     f"{audio.get('jitter_ms','N/A')}ms  {jitter_icon}")
    row("Silence",    f"{audio.get('silence_pct','N/A')}%  {silence_icon}")
    row("Ring Time",  f"{meta.get('ring_time','?')}s")
    if audio.get("silence_gaps"):
        gaps_str = f"{len(audio['silence_gaps'])} gap(s)"
        first    = audio["silence_gaps"][0]
        gaps_str += f" (first at ~{first.get('at','?')}, {first.get('duration_ms','?')}ms)"
        row("Gaps > 3s",  gaps_str)
    if audio.get("clipping"):
        row("Clipping",  "⚠️  Detected")
    if audio.get("volume_dBFS") is not None:
        row("Volume",    f"{audio['volume_dBFS']:.1f} dBFS")

    print(f"╠{line}╣")
    print(f"║  {'GEMINI 2.0 FLASH ANALYSIS':<{w-4}}║")
    print(f"║  {hline}  ║")

    # Summary — wrap at 50 chars
    summary = ta.get("summary", "")
    words   = summary.split()
    chunk   = ""
    first_line = True
    for word in words:
        if len(chunk) + len(word) + 1 > 48:
            label = "Summary" if first_line else ""
            row(label, chunk.strip())
            first_line = False
            chunk = word + " "
        else:
            chunk += word + " "
    if chunk.strip():
        row("" if not first_line else "Summary", chunk.strip())

    print(f"║  {hline}  ║")
    row("Intent",     ta.get("caller_intent", "")[:46])
    row("Resolution", f"{res_icon} {ta.get('call_resolution','?')}")
    row("Sentiment",  ta.get("caller_sentiment", "")[:46])
    row("Topics",     ", ".join(ta.get("topics_covered", []))[:46])

    if ta.get("action_items"):
        print(f"║  {hline}  ║")
        print(f"║  {'ACTION ITEMS':<{w-4}}║")
        for item in ta["action_items"][:4]:
            print(f"║    • {str(item)[:w-8]:<{w-8}}║")

    if ta.get("unanswered_questions"):
        print(f"║  {hline}  ║")
        print(f"║  {'UNANSWERED QUESTIONS':<{w-4}}║")
        for q in ta["unanswered_questions"][:3]:
            print(f"║    • {str(q)[:w-8]:<{w-8}}║")

    if ta.get("confusion_signals"):
        print(f"║  {hline}  ║")
        print(f"║  {'CONFUSION SIGNALS':<{w-4}}║")
        for s in ta["confusion_signals"][:3]:
            print(f"║    • {str(s)[:w-8]:<{w-8}}║")

    print(f"╠{line}╣")
    print(f"║  {'QUALITY SCORES':<{w-4}}║")
    score_line = (f"Overall:{scores.get('overall','?')}  "
                  f"Clarity:{scores.get('clarity','?')}  "
                  f"Resolution:{scores.get('resolution','?')}  "
                  f"Empathy:{scores.get('empathy','?')}")
    print(f"║  {score_line:<{w-4}}║")

    print(f"╠{line}╣")
    print(f"║  {'Gate reason: ' + gate.get('reason','')[:w-18]:<{w-4}}║")
    print(f"║  {'Report: ' + saved_path[-(w-10):]:<{w-4}}║")
    if report["recording"].get("local_path"):
        print(f"║  {'Audio: ' + str(report['recording']['local_path'])[-(w-10):]:<{w-4}}║")
    print(f"╚{line}╝\n")


# ---------------------------------------------------------------------------
# Internal endpoint — agent posts transcript lines here
# ---------------------------------------------------------------------------

class TranscriptLinePayload(BaseModel):
    call_uuid:   str = ""
    sip_call_id: str = ""
    room_name:   str = ""
    speaker:     str = "caller"
    text:        str = ""
    timestamp:   str = ""


@app.post("/internal/transcript", include_in_schema=False)
async def internal_transcript(body: TranscriptLinePayload):
    """Agent worker POSTs each spoken line here in real-time."""
    # Resolve call_uuid from any available identifier
    call_uuid = body.call_uuid

    if not call_uuid and body.sip_call_id:
        call_uuid = sip_to_uuid.get(body.sip_call_id, "")

    if not call_uuid and body.room_name:
        call_uuid = room_to_uuid.get(body.room_name, "")

    if not call_uuid:
        # Create an entry if we haven't received CallInitiated yet
        call_uuid = body.sip_call_id or body.room_name or "unknown"
        call_store.setdefault(call_uuid, {"lines": [], "sip_call_id": body.sip_call_id})

    call_store.setdefault(call_uuid, {"lines": [], "sip_call_id": body.sip_call_id})
    call_store[call_uuid]["lines"].append({
        "speaker":   body.speaker,
        "text":      body.text,
        "timestamp": body.timestamp or datetime.now(timezone.utc).isoformat(),
    })

    if body.room_name:
        room_to_uuid[body.room_name] = call_uuid
    if body.sip_call_id:
        sip_to_uuid[body.sip_call_id] = call_uuid

    return {"ok": True}


@app.post("/internal/room_mapping", include_in_schema=False)
async def internal_room_mapping(request: Request):
    """Agent registers room_name → sip_call_id mapping when participant joins."""
    body = await request.json()
    room_name   = body.get("room_name", "")
    sip_call_id = body.get("sip_call_id", "")
    phone       = body.get("phone", "")

    if room_name and sip_call_id:
        sip_to_uuid.setdefault(sip_call_id, room_name)
        call_store.setdefault(room_name, {
            "lines": [], "sip_call_id": sip_call_id, "phone": phone
        })
        call_store[room_name]["sip_call_id"] = sip_call_id
        logger.info("Room mapped: %s → SIP %s", room_name, sip_call_id)

    return {"ok": True}


# ---------------------------------------------------------------------------
# REST — Reports
# ---------------------------------------------------------------------------

@app.get("/reports")
async def list_reports():
    reports = sorted(REPORTS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    result  = []
    for f in reports[:50]:
        try:
            data = json.loads(f.read_text())
            result.append({
                "filename":   f.name,
                "call_uuid":  data.get("call_uuid"),
                "generated_at": data.get("generated_at"),
                "gate":       data.get("gate", {}).get("verdict"),
                "quality":    data.get("transcript_analysis", {}).get("quality_scores", {}).get("overall"),
                "duration":   data.get("call_meta", {}).get("duration_seconds"),
            })
        except Exception:
            pass
    return {"total": len(result), "reports": result}


@app.get("/reports/{call_uuid_prefix}")
async def get_report(call_uuid_prefix: str):
    matches = list(REPORTS_DIR.glob(f"*{call_uuid_prefix}*"))
    if not matches:
        raise HTTPException(404, "Report not found")
    latest = max(matches, key=lambda f: f.stat().st_mtime)
    return json.loads(latest.read_text())


@app.get("/health")
async def health():
    return {
        "status":           "ok",
        "active_calls":     len(call_store),
        "reports_saved":    len(list(REPORTS_DIR.glob("*.json"))),
        "recordings_saved": len(list(RECORDINGS_DIR.glob("*"))),
        "gemini_model":     os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        "vobiz_auth_set":   bool(VOBIZ_AUTH_ID and VOBIZ_AUTH_TOKEN),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0",
                port=int(os.getenv("WEBHOOK_PORT", 9000)), reload=True)
