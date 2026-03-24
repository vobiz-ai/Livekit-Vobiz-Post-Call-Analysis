# 07 — Post-Call Analysis

Production gate post-call analysis for every SIP call using **Gemini 2.0 Flash** + **pydub** audio analysis.

After every call ends, the system automatically:
1. Receives the Vobiz Hangup webhook (MOS, Jitter, Duration, Cost)
2. Downloads the call recording (.mp3) from Vobiz Recording API
3. Runs **audio analysis** via pydub — silence %, gaps, volume, clipping
4. Runs **transcript analysis** via Gemini 2.0 Flash — intent, sentiment, quality scores, action items
5. Computes a **production gate verdict**: PASS / REVIEW / FAIL
6. Saves a full JSON report to `reports/` and prints a formatted summary

---

## Architecture

```
make_call.py → LiveKit → agent.py (captures transcript)
                                     │
                              POSTs each line to
                              main.py /internal/transcript
                                     │
Vobiz SIP Trunk                     │
  └─ CallInitiated webhook ──────→ main.py /webhook/vobiz
  └─ Hangup webhook ──────────────→ main.py
                                     │
                              1. Fetch recording from Vobiz API
                              2. Download .mp3
                              3. Audio analysis (pydub)
                              4. Transcript analysis (Gemini 2.0 Flash)
                              5. Gate verdict
                              6. Save + print report
```

---

## Setup

### 1. Install dependencies

```bash
cd 07-post-call-analysis
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Also install ffmpeg (required by pydub)
brew install ffmpeg   # macOS
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

Fill in:
- `VOBIZ_AUTH_ID` — from Vobiz Console → Settings → API Keys
- `VOBIZ_AUTH_TOKEN` — from Vobiz Console → Settings → API Keys
- `WEBHOOK_BASE_URL` — your ngrok URL (set after step 3)

All other keys are pre-filled.

### 3. Run ngrok

```bash
ngrok http 9000
```

Copy the `https://xxxx.ngrok-free.app` URL.

Set in `.env`:
```
WEBHOOK_BASE_URL=https://xxxx.ngrok-free.app
```

Set in **Vobiz Console → SIP → Outbound Trunks → Webhook URL**:
```
https://xxxx.ngrok-free.app/webhook/vobiz
```
Set **Webhook Method** to `POST`.

---

## Running

4 terminals:

```bash
# Terminal 1 — webhook server + analysis engine
uvicorn main:app --reload --port 9000

# Terminal 2 — ngrok (already running from setup)
ngrok http 9000

# Terminal 3 — LiveKit agent worker
python agent.py start

# Terminal 4 — make a call
python make_call.py --to +919148227303
```

After the call ends, wait ~10s, then check Terminal 1 for the analysis report.

---

## Webhook Events Received

| Event | What happens |
|-------|-------------|
| `CallInitiated` (Allowed=true) | Stores CallUUID → SIPCallID mapping |
| `CallInitiated` (Allowed=false) | Saves rejection report immediately |
| `Hangup` | Triggers full analysis pipeline |

---

## Analysis Output

```
╔════════════════════════════════════════════════════════╗
║          POST-CALL ANALYSIS  —  PRODUCTION GATE        ║
╠════════════════════════════════════════════════════════╣
║  CallUUID  : 6e558798-499c-4a68-bc77-46f2c53d1f69      ║
║  From → To : +917971542961 → +919148227303             ║
║  Duration  : 5m 00s  (Billable: 295s)                  ║
║  Cost      : 1.5 INR                                   ║
║  Gate      : ✅ PASS  (Quality: 82/100)                 ║
╠════════════════════════════════════════════════════════╣
║  AUDIO QUALITY (pydub + Vobiz CDR)                     ║
║  MOS Score : 4.2 / 5.0  ✅                             ║
║  Jitter    : 15ms  ✅                                   ║
║  Silence   : 12%  ✅                                    ║
║  Gaps > 3s : 1 gap (at ~0:45, 3200ms)                  ║
╠════════════════════════════════════════════════════════╣
║  GEMINI 2.0 FLASH ANALYSIS                             ║
║  Intent    : Billing dispute — refund request          ║
║  Resolved  : ✅ RESOLVED                                ║
║  Sentiment : frustrated → satisfied                    ║
╠════════════════════════════════════════════════════════╣
║  ACTION ITEMS                                          ║
║  • Process refund ₹2,400 within 3 business days        ║
╠════════════════════════════════════════════════════════╣
║  Quality: 82  Clarity: 90  Resolution: 75  Empathy: 85 ║
╠════════════════════════════════════════════════════════╣
║  Report : reports/CALL_6e558798_20260324_143000.json   ║
╚════════════════════════════════════════════════════════╝
```

---

## Gate Rules

| Verdict | Conditions |
|---------|-----------|
| **FAIL** | Call < 15s, or silence > 60%, or MOS < 2.0, or intent not captured, or call abandoned |
| **REVIEW** | Silence 30–60%, or MOS 2.0–3.5, or jitter > 50ms, or unresolved/escalated, or quality < 50 |
| **PASS** | None of the above |

---

## Standalone Replay Tool

Re-analyze any call without making a new call:

```bash
# Most recent call
python analyzer.py --latest

# By call UUID prefix
python analyzer.py --call-uuid abc123

# By report file
python analyzer.py --report reports/CALL_abc123_20260324.json

# Audio only (no Gemini)
python analyzer.py --recording recordings/abc123.mp3 --audio-only

# List all reports
python analyzer.py --list

# Batch summary for today
python analyzer.py --batch --date 2026-03-24

# Use a different model
python analyzer.py --latest --model gemini-1.5-pro
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/vobiz` | Vobiz SIP trunk webhook receiver |
| `GET`  | `/reports` | List all saved reports |
| `GET`  | `/reports/{uuid_prefix}` | Get specific report |
| `GET`  | `/health` | System status |

---

## Report JSON Structure

```json
{
  "call_uuid": "6e558798-...",
  "generated_at": "2026-03-24T14:30:00Z",
  "call_meta": {
    "from": "+917971542961",
    "to": "+919148227303",
    "duration_seconds": 300,
    "billsec": 295,
    "ring_time": 5,
    "cost": 1.5,
    "currency": "INR"
  },
  "recording": {
    "local_path": "recordings/6e558798.mp3",
    "recording_id": "d7801b2e-...",
    "format": "mp3"
  },
  "audio_analysis": {
    "mos": 4.2,
    "jitter_ms": 15,
    "silence_pct": 12.0,
    "silence_gaps": [{"at": "0:45", "duration_ms": 3200}],
    "volume_dBFS": -18.5,
    "clipping": false
  },
  "transcript": [
    {"speaker": "agent", "text": "Hello, this is Vobiz...", "timestamp": "..."},
    {"speaker": "caller", "text": "Hi, I have a billing question", "timestamp": "..."}
  ],
  "transcript_analysis": {
    "summary": "Caller raised a billing dispute...",
    "caller_intent": "Refund for duplicate charge",
    "caller_sentiment": "frustrated → satisfied",
    "call_resolution": "RESOLVED",
    "action_items": ["Process refund within 3 days"],
    "quality_scores": {"overall": 82, "clarity": 90, "resolution": 75, "empathy": 85, "efficiency": 78}
  },
  "gate": {
    "verdict": "PASS",
    "flags": [],
    "reason": "All checks passed. Quality: 82/100"
  }
}
```

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LIVEKIT_URL` | ✅ | LiveKit Cloud WebSocket URL |
| `LIVEKIT_API_KEY` | ✅ | LiveKit API key |
| `LIVEKIT_API_SECRET` | ✅ | LiveKit API secret |
| `OPENAI_API_KEY` | ✅ | OpenAI (for TTS/LLM during call) |
| `DEEPGRAM_API_KEY` | ✅ | Deepgram STT |
| `OUTBOUND_TRUNK_ID` | ✅ | Vobiz SIP trunk ID |
| `VOBIZ_AUTH_ID` | ✅ | Vobiz account ID (for Recording API) |
| `VOBIZ_AUTH_TOKEN` | ✅ | Vobiz auth token (for Recording API) |
| `GEMINI_API_KEY` | ✅ | Google Gemini API key |
| `GEMINI_MODEL` | | Model to use (default: `gemini-2.0-flash`) |
| `WEBHOOK_PORT` | | Port for FastAPI server (default: `9000`) |
| `WEBHOOK_BASE_URL` | | ngrok public URL |
| `BACKEND_URL` | | Where agent POSTs transcripts (default: `http://localhost:9000`) |
