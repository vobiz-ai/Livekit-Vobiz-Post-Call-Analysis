# 07 — Post-Call Analysis

> Production-grade AI post-call analysis for every SIP phone call.  
> Receives the Vobiz SIP webhook on hangup, downloads the recording, runs audio analysis with **pydub**, analyzes the full transcript with **Gemini 2.0 Flash**, and prints a structured **PASS / REVIEW / FAIL** production gate verdict — automatically, every time.

---

## How It Works

```
1. Call ends → Vobiz fires POST /webhook/vobiz  (Hangup event)
                │
                ├── Duration, Billsec, MOS, Jitter, Cost  ← from Hangup CDR
                │
2. Recording fetched from Vobiz API  (waits 8s for processing)
   → Downloaded to recordings/{call_uuid}.mp3
                │
3. Audio Analysis  (pydub)
   ├── Silence % + long gap detection
   ├── Auto-calibrated noise floor → silence threshold
   ├── Stereo per-channel: agent volume vs caller volume
   ├── Dominant speaker detection
   ├── Background noise classification (quiet/moderate/noisy/very_noisy)
   ├── Ring tone detection (unanswered call detection)
   └── Clipping, low volume, one-sided audio
                │
4. Transcript Analysis  (Gemini 2.0 Flash)
   ├── Summary (2-3 sentences)
   ├── Caller intent + sentiment arc
   ├── Agent sentiment + tone
   ├── Language detected (English / Hinglish / Hindi)
   ├── Key moments (up to 5 turning points)
   ├── Action items
   ├── Unanswered questions
   ├── Confusion signals
   ├── 🚨 Scam indicators (OLX, Telegram, UPI fraud, crypto, etc.)
   ├── Call resolution (RESOLVED/UNRESOLVED/ESCALATED/TRANSFERRED/ABANDONED)
   ├── Quality scores (Overall, Clarity, Resolution, Empathy, Efficiency)
   └── Analysis confidence (0-100)
                │
5. Production Gate  →  PASS  /  REVIEW  /  FAIL
                │
6. JSON report saved → reports/CALL_{uuid}_{timestamp}.json
   Formatted summary printed to terminal
```

---

## Files

```
07-post-call-analysis/
├── main.py            # FastAPI server — Vobiz webhook receiver + pipeline orchestration
├── agent.py           # LiveKit agent — captures two-sided transcript in real time
├── gemini_analyzer.py # Gemini 2.0 Flash transcript analysis with retry + fallback
├── audio_analyzer.py  # pydub audio quality analysis — silence, noise, stereo, ring
├── analyzer.py        # Standalone CLI replay tool — re-analyze, batch, stats, export
├── make_call.py       # Dispatch an outbound call via LiveKit
├── requirements.txt
├── .env.example
├── .gitignore
├── recordings/        # Downloaded .mp3 files (auto-created)
└── reports/           # JSON analysis reports (auto-created)
```

---

## Setup

### 1. Prerequisites

```bash
# Python 3.11+ required
python3 --version

# ffmpeg required by pydub for audio decoding
brew install ffmpeg        # macOS
# sudo apt install ffmpeg  # Ubuntu/Debian
```

### 2. Install dependencies

```bash
cd 07-post-call-analysis

# Create venv (Python 3.11)
/Users/piyuzz/.pyenv/versions/3.11.9/bin/python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Where to find it |
|----------|-----------------|
| `LIVEKIT_URL` | LiveKit Cloud dashboard |
| `LIVEKIT_API_KEY` | LiveKit Cloud → Settings → API Keys |
| `LIVEKIT_API_SECRET` | LiveKit Cloud → Settings → API Keys |
| `OPENAI_API_KEY` | platform.openai.com |
| `DEEPGRAM_API_KEY` | console.deepgram.com |
| `OUTBOUND_TRUNK_ID` | LiveKit Cloud → SIP Trunks |
| `VOBIZ_AUTH_ID` | Vobiz Console → Settings → API Keys |
| `VOBIZ_AUTH_TOKEN` | Vobiz Console → Settings → API Keys |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

### 4. Start ngrok

```bash
ngrok http 9000
# Copy the https://xxxx.ngrok-free.app URL
```

### 5. Set webhook in Vobiz Console

Go to **[Vobiz Console → SIP → Outbound Trunks](https://console.vobiz.ai/app/sip/out/trunks)** → edit your trunk:

```
Webhook URL:    https://xxxx.ngrok-free.app/webhook/vobiz
Webhook Method: POST
```

Update `.env`:
```bash
WEBHOOK_BASE_URL=https://xxxx.ngrok-free.app
```

---

## Running

4 terminals:

```bash
# Terminal 1 — webhook server + analysis engine
source .venv/bin/activate
uvicorn main:app --reload --port 9000

# Terminal 2 — ngrok (already running)
ngrok http 9000

# Terminal 3 — LiveKit agent worker
source .venv/bin/activate
python agent.py start

# Terminal 4 — make a call
source .venv/bin/activate
python make_call.py --to +91XXXXXXXXXX
```

**After the call ends (~10s)** the analysis fires automatically and you'll see:

```
╔════════════════════════════════════════════════════════╗
║         POST-CALL ANALYSIS  —  PRODUCTION GATE         ║
╠════════════════════════════════════════════════════════╣
║  CallUUID         a6d9b22e-46d7-4342-9d98-f31b7387f2d7 ║
║  From → To        +917971543270 → +919148227303         ║
║  Duration         2m 45s  (Billable: 158s)              ║
║  Cost             1.58 INR                              ║
║  Gate             ✅ PASS  (Quality: 84/100)             ║
╠════════════════════════════════════════════════════════╣
║  AUDIO QUALITY (from Vobiz CDR + pydub)                 ║
║  MOS Score        4.3 / 5.0  ✅                          ║
║  Jitter           12ms  ✅                               ║
║  Silence          14.2%  ✅                              ║
║  Ring Time        5s                                    ║
║  Noise Level      quiet                                 ║
╠════════════════════════════════════════════════════════╣
║  GEMINI 2.0 FLASH ANALYSIS                             ║
║  Summary          Caller asked about sending money to   ║
║                   a contact from OLX. Agent advised     ║
║                   caution and recommended not sending.  ║
╠════════════════════════════════════════════════════════╣
║  ⚠️  SCAM INDICATORS DETECTED                          ║
║    🚨 OLX deal — unverified seller                      ║
║    🚨 Advance payment requested                         ║
╠════════════════════════════════════════════════════════╣
║  Intent           Buy iPhone from OLX — needs payment   ║
║  Resolution       ⚠️ UNRESOLVED                         ║
║  Sentiment        concerned → cautious                  ║
║  Language         English                               ║
╠════════════════════════════════════════════════════════╣
║  ACTION ITEMS                                          ║
║  • Do not send money to unverified OLX seller           ║
╠════════════════════════════════════════════════════════╣
║  QUALITY SCORES                                        ║
║  Overall:84  Clarity:90  Resolution:60  Empathy:88      ║
║  Confidence: 95/100  ✅                                  ║
╠════════════════════════════════════════════════════════╣
║  Gate reason: REVIEW: scam_indicators_detected(2)       ║
║  Report: reports/CALL_a6d9b22e_20260325_143012.json     ║
║  Audio:  recordings/a6d9b22e-46d7-4342.mp3              ║
╚════════════════════════════════════════════════════════╝
```

---

## Webhook Events

| Event | What happens |
|-------|-------------|
| `CallInitiated` (Allowed=true) | Stores phone → call mapping, ready to collect transcript |
| `CallInitiated` (Allowed=false) | Saves rejection report immediately with reason |
| `Hangup` | Triggers full analysis pipeline asynchronously |

### How transcript matching works

Vobiz uses **three different IDs** across a call's lifecycle — none of them match each other:

| ID | Source | Example |
|----|--------|---------|
| `CallInitiated.CallUUID` | Vobiz | `5Jnsfveg5En1f2GBqM3tnRK2XhHA` |
| `Hangup.CallUUID` | Vobiz CDR | `926fd4e5-0887-41d4-88bd-4bb42f8bd1ab` |
| LiveKit SIP participant ID | LiveKit | `SCL_jHpRcjcaqRxy` |

**Solution:** The `From` phone number is the only field consistent across all events and the agent's room name (`post-call-analysis-919148227303-xxxx`). All transcript lines are stored under `phone_store[phone]` and resolved by `hangup.From` at analysis time.

---

## Production Gate Rules

### FAIL (any one condition)

| Flag | Condition |
|------|-----------|
| `call_too_short` | Duration < 15 seconds |
| `mostly_silence` | Silence > 60% of recording |
| `very_poor_audio_quality` | MOS < 2.0 |
| `intent_not_captured` | `caller_intent` is unknown/empty |
| `call_abandoned` | Caller hung up without completing interaction |
| `critically_low_quality` | Overall quality < 25/100 |
| `multiple_scam_indicators` | 3 or more scam signals detected |

### REVIEW (no FAIL, but one or more)

| Flag | Condition |
|------|-----------|
| `high_silence` | Silence 30–60% |
| `poor_audio_quality` | MOS 2.0–3.5 |
| `high_jitter` | Jitter > 50ms |
| `one_sided_audio` | One channel much louder than the other |
| `call_unresolved` / `call_escalated` | Issue not resolved |
| `low_quality_score` | Overall quality 25–50 |
| `multiple_unanswered_questions` | 2+ caller questions not addressed |
| `high_confusion` | 2+ confusion signals detected |
| `audio_clipping` | Peak amplitude near 0dBFS |
| `low_audio_volume` | Mean volume < -35dBFS |
| `scam_indicators_detected(N)` | Any scam signal found |
| `low_analysis_confidence` | Gemini confidence < 40% |

### PASS

All of the above conditions are clear.

---

## Analyzer CLI Tool

Re-analyze any call without making a new one:

```bash
# Most recent call
python analyzer.py --latest

# By UUID prefix
python analyzer.py --call-uuid a6d9b22e

# By report file
python analyzer.py --report reports/CALL_a6d9b22e_20260325.json

# Audio file only (no Gemini)
python analyzer.py --recording recordings/a6d9b22e.mp3

# List all reports
python analyzer.py --list

# Aggregate statistics across all reports
python analyzer.py --stats

# Compare two calls side-by-side
python analyzer.py --compare a6d9b22e 926fd4e5

# Live watch mode — prints new reports as they arrive
python analyzer.py --watch

# Batch analyze all calls
python analyzer.py --batch

# Batch for a specific date
python analyzer.py --batch --date 2026-03-25

# Export all reports to CSV
python analyzer.py --export csv

# Export all reports to JSON array
python analyzer.py --export json

# Re-analyze with a different Gemini model
python analyzer.py --latest --model gemini-2.5-pro

# Skip audio analysis (transcript only)
python analyzer.py --latest --transcript-only

# Skip Gemini (audio analysis only)
python analyzer.py --latest --audio-only

# Don't save re-analyzed report
python analyzer.py --latest --no-save

# Verbose logging
python analyzer.py --latest --verbose
```

---

## Gemini Analyzer Robustness

### Retry + fallback chain

```
Attempt 1: gemini-2.0-flash
  ↓ fail (transient)
Attempt 2: gemini-2.0-flash  (1.5s delay)
  ↓ fail (transient)
Attempt 3: gemini-2.0-flash  (2.25s delay)
  ↓ fail (non-transient / exhausted)
Try: gemini-2.5-flash
  ↓ fail
Try: gemini-2.5-pro
  ↓ fail
Try: gemini-flash-latest
  ↓ fail
Return empty analysis (never crashes)
```

Retries only on transient errors: `429 RESOURCE_EXHAUSTED`, `500 INTERNAL`, `503 UNAVAILABLE`, `timeout`, `rate limit`.

### Response validation

Every field returned by Gemini goes through Pydantic validators:

- **Scores** — clamped to `[0, 100]` by `field_validator`
- **Resolution** — forced to one of `RESOLVED/UNRESOLVED/ESCALATED/TRANSFERRED/ABANDONED`
- **Lists** — deduplicated, empty strings removed
- **KeyMoments** — handles dict, Python repr string (`"{'at': '0:32', ...}"`), or plain string
- **Consistency** — abandoned calls capped at quality 60; resolved calls get resolution ≥ 60

### Indian telecom context

The Gemini system prompt explicitly includes:

- Hinglish vocabulary recognition (`haan`, `nahi`, `theek hai`, `paisa`, etc.)
- Indian scam pattern taxonomy:
  - OLX/Quikr fake sellers
  - Telegram/WhatsApp advance payment
  - Cheap electronics fraud
  - Crypto investment schemes
  - KYC update scams
  - Lottery/prize scams
  - Fake tech support

---

## Audio Analyzer Robustness

### Auto-calibrated silence detection

Instead of a fixed -40dBFS threshold, the threshold is computed per-file:

```
noise_floor = average dBFS of the quietest 10% of 100ms chunks
silence_threshold = noise_floor + 14dB
```

This avoids false positives in noisy call center / telecom recordings.

### What gets analyzed

| Metric | Method |
|--------|--------|
| Silence % | pydub `detect_silence` with auto-threshold |
| Long gaps > 3s | Silent ranges with timestamp |
| Speech % | 100 - silence_pct |
| Volume | mean dBFS |
| Peak / clipping | max_dBFS > -0.5 |
| Noise floor | Quietest 10% of chunks |
| Noise level | `quiet / moderate / noisy / very_noisy` |
| Ring detection | Periodic loud/silent in first 6s |
| Stereo channels | Per-channel volume → dominant speaker |
| One-sided audio | > 18dB difference between channels |

### Corrupt file handling

- Minimum size check (< 1KB → skip)
- Minimum duration check (< 0.5s → skip)
- `_safe_load()` tries `mp3 / ogg / wav / flac / m4a` formats
- Catches `MemoryError` for very large files
- All failures return a valid `AudioReport` with `error` field set

---

## Report JSON Structure

```json
{
  "call_uuid": "926fd4e5-0887-41d4-88bd-4bb42f8bd1ab",
  "generated_at": "2026-03-25T14:30:12Z",

  "call_meta": {
    "from": "+917971543270",
    "to": "+919148227303",
    "duration_seconds": 165,
    "billsec": 158,
    "ring_time": 5,
    "cost": 1.58,
    "currency": "INR",
    "reason": "NORMAL_CLEARING"
  },

  "recording": {
    "local_path": "recordings/926fd4e5.mp3",
    "recording_id": "d7801b2e-e76d-4dd8-be9c-9e015a7267b8",
    "format": "mp3",
    "duration_ms": "165000.00"
  },

  "audio_analysis": {
    "analyzed": true,
    "mos": 4.3,
    "jitter_ms": 12,
    "duration_seconds": 165,
    "silence_pct": 14.2,
    "speech_pct": 85.8,
    "silence_gaps": [
      { "at": "0:32", "duration_ms": 3800 }
    ],
    "volume_dBFS": -18.5,
    "max_dBFS": -2.1,
    "noise_floor_dBFS": -55.2,
    "noise_level": "quiet",
    "clipping": false,
    "low_volume": false,
    "ring_detected": false,
    "one_sided": false,
    "dominant_speaker": "balanced",
    "silence_threshold_used": -41.2
  },

  "transcript": [
    { "speaker": "agent",  "text": "Hello, this is Vobiz AI…", "timestamp": "…" },
    { "speaker": "caller", "text": "Hi, I want to send money…", "timestamp": "…" }
  ],

  "transcript_analysis": {
    "summary": "Caller inquired about sending ₹15,000 to an OLX contact…",
    "caller_intent": "Send payment to OLX seller for iPhone",
    "caller_sentiment": "trusting → cautious",
    "agent_sentiment": "professional, cautionary",
    "language_detected": "English",
    "key_moments": [
      { "at": "0:18", "event": "Caller mentioned OLX as the source" },
      { "at": "1:32", "event": "Agent advised not to send money" }
    ],
    "action_items": [
      "Do not send money to unverified OLX seller",
      "Verify seller identity before any payment"
    ],
    "unanswered_questions": [],
    "topics_covered": ["UPI payment", "OLX", "scam awareness"],
    "call_resolution": "UNRESOLVED",
    "confusion_signals": [],
    "scam_indicators": [
      "OLX deal — unverified seller",
      "Advance payment requested before goods"
    ],
    "quality_scores": {
      "overall": 84,
      "clarity": 90,
      "resolution": 60,
      "empathy": 88,
      "efficiency": 82
    },
    "analysis_confidence": 95
  },

  "gate": {
    "verdict": "REVIEW",
    "flags": ["scam_indicators_detected(2)", "call_unresolved"],
    "reason": "REVIEW: scam_indicators_detected(2), call_unresolved",
    "score_breakdown": {
      "overall": 84,
      "clarity": 90,
      "resolution": 60,
      "empathy": 88,
      "efficiency": 82,
      "confidence": 95
    }
  }
}
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/vobiz` | Vobiz SIP trunk webhook (CallInitiated + Hangup) |
| `GET`  | `/reports` | List all reports (paginated, latest first) |
| `GET`  | `/reports/{uuid_prefix}` | Get specific report JSON |
| `GET`  | `/health` | System status (model, auth, counts) |
| `POST` | `/internal/transcript` | Agent posts transcript lines (internal) |
| `POST` | `/internal/room_mapping` | Agent registers phone ↔ SIP mapping (internal) |

---

## Environment Variables

```bash
# ── LiveKit ──────────────────────────────────────────────
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── STT (call transcript) ─────────────────────────────────
DEEPGRAM_API_KEY=xxxx

# ── TTS + LLM (during call) ───────────────────────────────
OPENAI_API_KEY=sk-xxxx

# ── Vobiz SIP ─────────────────────────────────────────────
VOBIZ_SIP_DOMAIN=xxxx.sip.vobiz.ai
OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxx
DEFAULT_TRANSFER_NUMBER=+91XXXXXXXXXX

# ── Vobiz Recording API ───────────────────────────────────
VOBIZ_AUTH_ID=MA_XXXXXXXX          # Vobiz Console → Settings → API Keys
VOBIZ_AUTH_TOKEN=your_auth_token   # Vobiz Console → Settings → API Keys

# ── Gemini ────────────────────────────────────────────────
GEMINI_API_KEY=AIzaXXXXXXXXXXXXXXX              # aistudio.google.com/apikey
# GOOGLE_GENERATIVE_AI_API_KEY=AIza...          # alternative env var name
GEMINI_MODEL=gemini-2.0-flash                   # default model

# ── Webhook server ────────────────────────────────────────
WEBHOOK_PORT=9000
WEBHOOK_BASE_URL=https://xxxx.ngrok-free.app    # ngrok URL

# ── Backend (where agent posts transcripts) ───────────────
BACKEND_URL=http://localhost:9000

# ── Gate thresholds (optional, these are defaults) ────────
GATE_MIN_DURATION_SECS=15
GATE_MAX_SILENCE_PCT=60
GATE_MIN_MOS=2.0
GATE_REVIEW_SILENCE_PCT=30
GATE_REVIEW_MOS=3.5
GATE_REVIEW_MAX_JITTER_MS=50

# ── Gemini retry config ───────────────────────────────────
GEMINI_MAX_RETRIES=3
GEMINI_RETRY_BASE_DELAY=1.5
MAX_TRANSCRIPT_LINES=200

# ── Audio thresholds ──────────────────────────────────────
AUDIO_SILENCE_OFFSET_DB=14         # added to noise floor
AUDIO_MIN_SILENCE_MS=400
AUDIO_LONG_SILENCE_MS=3000
AUDIO_CLIPPING_THRESH=-0.5
AUDIO_LOW_VOLUME_THRESH=-35.0

# ── Recording fetch ───────────────────────────────────────
RECORDING_WAIT_SECS=8
RECORDING_RETRIES=3
RECORDING_RETRY_DELAY_SECS=5
```

---

## Troubleshooting

**`Webhook not received`**
→ Check ngrok is running and the URL is set in Vobiz Console trunk settings.
→ Check `WEBHOOK_BASE_URL` in `.env` matches the ngrok URL.

**`Transcript: 0 lines`**
→ Make sure `agent.py start` is running before making the call.
→ Check `BACKEND_URL=http://localhost:9000` in agent's `.env`.

**`Recording not found`**
→ Add `VOBIZ_AUTH_ID` and `VOBIZ_AUTH_TOKEN` to `.env`.
→ Increase `RECORDING_WAIT_SECS=15` if Vobiz takes longer to process.

**`Gemini API key invalid`**
→ Get a new key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
→ Ensure the key is for an active Google Cloud project.

**`pydub/ffmpeg error`**
→ Run `brew install ffmpeg` (macOS) or `sudo apt install ffmpeg` (Linux).

**`Port 9000 already in use`**
→ `kill $(lsof -ti :9000)` then restart.

**`KeyMoment validation error`** (older reports)
→ Use `python analyzer.py --latest` which applies the fixed parser automatically.

---

## Extending

### Add a new scam pattern to detection

In `gemini_analyzer.py`, update the system prompt's scam list:

```python
_SYSTEM_PROMPT = """
...
  - Your new pattern here
...
"""
```

### Add a new gate rule

In `gemini_analyzer.py`, add to `compute_gate()`:

```python
# FAIL if confidence is very low on a long call
if ta.analysis_confidence < 20 and duration_seconds > 60:
    fail_flags.append("very_low_confidence_long_call")
```

### Change the Gemini model

```bash
# In .env
GEMINI_MODEL=gemini-2.5-pro

# Or per-run
python analyzer.py --latest --model gemini-2.5-pro
```

### Add a webhook to your CRM

In `main.py`, after `_print_report()`:

```python
await post_to_crm(report)   # your own function
```
