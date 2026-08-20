# Vobiz + LiveKit — Post-Call Analysis

Automatic transcription, summarisation, and quality analysis for every LiveKit voice call
carried on a Vobiz SIP trunk — triggered by the trunk's hangup webhook, scored by Gemini,
and reduced to a single **PASS / REVIEW / FAIL** verdict.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![LiveKit Agents](https://img.shields.io/badge/LiveKit-Agents%201.5-1FD5F9.svg)](https://github.com/livekit/agents)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![Gemini](https://img.shields.io/badge/Gemini-2.0%20Flash-4285F4.svg)](https://ai.google.dev/)
[![Docs](https://img.shields.io/badge/docs-docs.vobiz.ai-0B5FFF.svg)](https://docs.vobiz.ai)

> The call ends. Ten seconds later a structured report exists on disk: what the caller
> wanted, whether they got it, how the audio sounded, which fraud patterns the transcript
> matched, and whether a human needs to look at it.

---

## Table of contents

- [Overview](#overview)
- [What you can build with it](#what-you-can-build-with-it)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running it](#running-it)
- [Webhook events and transcript matching](#webhook-events-and-transcript-matching)
- [Production gate rules](#production-gate-rules)
- [Report JSON structure](#report-json-structure)
- [HTTP API reference](#http-api-reference)
- [Analyzer CLI reference](#analyzer-cli-reference)
- [Transcript analyser behaviour](#transcript-analyser-behaviour)
- [Audio analyser behaviour](#audio-analyser-behaviour)
- [Extending](#extending)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Team Vobiz](#built-by-team-vobiz)

---

## Overview

Voice agents are easy to demo and hard to trust. A demo call sounds fine; the hundredth
call of the day is the one where the STT dropped a sentence, the agent answered a question
the caller never asked, or the caller hung up eight seconds in. You only find out if
something looks at every call after it ends.

This example is that something. It is a complete post-call pipeline for a LiveKit voice
agent whose telephony leg runs over a [Vobiz](https://vobiz.ai) SIP trunk. While the call
is live, `agent.py` streams every finalised transcript line — both the agent's and the
caller's — into a local FastAPI server. When the call ends, Vobiz fires a `Hangup` webhook
at that same server, carrying the CDR: duration, billable seconds, MOS, jitter, cost.
That webhook is the trigger. The server pulls the recording from the Vobiz Recording API,
measures the audio with `pydub`, sends the transcript to Gemini for a structured
assessment, applies a deterministic gate on top of both, writes a JSON report, and prints
a summary to the terminal.

The important design decision is that the transcript does **not** come from the recording.
Re-transcribing an MP3 after the fact costs money, takes time, and loses speaker
attribution. Here the transcript is captured live from the LiveKit session — Deepgram
`nova-3` for the caller, the agent's own turns for the agent side — so by the time the
hangup webhook arrives the text is already sitting in memory, correctly labelled by
speaker and timestamped. The recording is used only for what text cannot tell you: silence
ratios, background noise, clipping, and whether one side of the call was inaudible.

You end up with `reports/CALL_<uuid>_<timestamp>.json` per call, a `PASS` / `REVIEW` /
`FAIL` verdict you can route on, and a CLI (`analyzer.py`) for replaying, comparing,
aggregating, and exporting everything you have collected. The gate thresholds, the Gemini
model, and every audio threshold are environment variables, so the judgement is yours to
tune rather than something baked into the code.

---

## What you can build with it

- **Automated QA for an agent fleet.** Score every call instead of the two percent a human
  reviewer can listen to, and send only `REVIEW` and `FAIL` calls to a person.
- **A fraud and abuse tripwire.** The analyser prompt carries a scam-pattern taxonomy
  (marketplace deals, advance payment, lottery and prize claims, crypto returns, fake tech
  support, KYC-expiry pressure). Three or more indicators fail the call outright, so
  suspicious conversations surface the moment they end rather than at month-end.
- **A regression gate in CI for prompt changes.** Run a fixed set of calls against a new
  system prompt, then `python analyzer.py --stats` to compare pass rates and average
  quality before and after.
- **Voice-quality monitoring for a trunk or route.** MOS, jitter, silence percentage,
  one-sided audio, and clipping are recorded per call, so a degrading carrier route shows
  up as a trend rather than as an anecdote.
- **Compliance and disclosure evidence.** Every call produces a timestamped transcript,
  an intent, a resolution state, and an action-item list you can retain against your own
  policy.
- **Lead and follow-up extraction.** `action_items`, `unanswered_questions`, and
  `topics_covered` are structured fields — feed them straight into a ticket or a CRM task
  instead of asking someone to listen to the recording.

---

## How it works

```
1. Call ends → Vobiz fires POST /webhook/vobiz  (Hangup event)
                │
                ├── Duration, Billsec, MOS, Jitter, Cost  ← from Hangup CDR
                │
2. Recording fetched from Vobiz API  (waits RECORDING_WAIT_SECS, default 8s)
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
4. Transcript Analysis  (Gemini, GEMINI_MODEL — default gemini-2.0-flash)
   ├── Summary (2-3 sentences)
   ├── Caller intent + sentiment arc
   ├── Agent sentiment + tone
   ├── Language detected (English / Hinglish / Hindi)
   ├── Key moments (up to 5 turning points)
   ├── Action items
   ├── Unanswered questions
   ├── Confusion signals
   ├── Scam indicators (marketplace, messaging-app, UPI fraud, crypto, etc.)
   ├── Call resolution (RESOLVED/UNRESOLVED/ESCALATED/TRANSFERRED/ABANDONED)
   ├── Quality scores (Overall, Clarity, Resolution, Empathy, Efficiency)
   └── Analysis confidence (0-100)
                │
5. Production Gate  →  PASS  /  REVIEW  /  FAIL
                │
6. JSON report saved → reports/CALL_{uuid}_{timestamp}.json
   Formatted summary printed to terminal
```

### The pipeline, step by step

**What triggers it.** `POST /webhook/vobiz` in `main.py`. The handler reads `Event` from
the JSON body. On `CallInitiated` it records the phone → call mapping (and, if
`Allowed` is `false`, immediately writes `reports/REJECTED_<uuid>_<ts>.json` with a `FAIL`
verdict). On `Hangup` it hands the body to `_run_analysis_pipeline()` via
`asyncio.create_task()` and returns `{"ok": true}` straight away, so Vobiz never waits on
Gemini or on a recording download.

**Where the transcript comes from.** Not the recording — the live session. In `agent.py`
two LiveKit handlers capture both sides:

| Event | Filter | Stored as |
|-------|--------|-----------|
| `conversation_item_added` | `msg.role == "assistant"`, de-duplicated on `msg.id` | `speaker: "agent"` |
| `user_input_transcribed` | `event.is_final` only | `speaker: "caller"` |

Each line is `POST`ed to `{BACKEND_URL}/internal/transcript` with a 3-second timeout, and
failures are logged at debug level and swallowed — a transcript post must never disturb a
live call. The server appends each line to `phone_store[phone]["lines"]`, an in-process
dictionary keyed by the caller's E.164 number.

**Which model summarises.** `gemini_analyzer.analyze_transcript()` calls
`client.models.generate_content()` from `google-genai` with
`response_mime_type="application/json"`, `response_schema=TranscriptAnalysis`,
`temperature=0.1`, and `max_output_tokens=2048` — so the model is constrained to the
Pydantic schema rather than asked politely for JSON. The model is `GEMINI_MODEL`
(default `gemini-2.0-flash`), with automatic fallback through `gemini-2.0-flash` →
`gemini-2.5-flash` → `gemini-2.5-pro` → `gemini-flash-latest`. Transcripts longer than
`MAX_TRANSCRIPT_LINES` (200) are truncated to the first 20 plus the last 180 lines, which
preserves the opening and the close — the two parts that decide intent and resolution.

**What the output looks like.** A `TranscriptAnalysis` object with these fields:

| Field | Type | Notes |
|-------|------|-------|
| `summary` | `str` | 2-3 sentences |
| `caller_intent` | `str` | Truncated to 200 characters by `_validate_and_fix()` |
| `caller_sentiment` / `agent_sentiment` | `str` | Free text, e.g. `"trusting → cautious"` |
| `language_detected` | `str` | English / Hinglish / Hindi |
| `key_moments` | `list[KeyMoment]` | `{at, event}`, up to 5 |
| `action_items` | `list[str]` | De-duplicated, empties stripped |
| `unanswered_questions` | `list[str]` | Feeds the `multiple_unanswered_questions` flag |
| `topics_covered` | `list[str]` | |
| `call_resolution` | `str` | One of `RESOLVED`, `UNRESOLVED`, `ESCALATED`, `TRANSFERRED`, `ABANDONED` — anything else is coerced to `UNRESOLVED` |
| `confusion_signals` | `list[str]` | |
| `scam_indicators` | `list[str]` | |
| `quality_scores` | `QualityScores` | `overall`, `clarity`, `resolution`, `empathy`, `efficiency`, each clamped to 0-100 |
| `analysis_confidence` | `int` | 0-100 |

`compute_gate()` then combines that object with the `AudioReport` and the CDR duration into
`{verdict, flags, reason, score_breakdown}`, and `_build_report()` assembles the final
document — `call_meta`, `recording`, `audio_analysis`, `transcript`, `transcript_analysis`,
`gate` — which is written to `reports/CALL_<first 8 chars of uuid>_<UTC timestamp>.json`.

`analyze_transcript()` never raises. If there is no transcript, if every model fails, or if
the API key is missing, it returns an empty analysis whose `summary` names the reason
(`no_transcript`, `empty_transcript_lines`, `all_models_failed`) and whose confidence is 0.
A report is always written.

---

## Architecture

| File | Responsibility |
|------|----------------|
| `main.py` | FastAPI server. Receives the Vobiz trunk webhook, orchestrates the pipeline, holds the in-memory transcript stores, fetches and downloads recordings, builds and saves reports, prints the terminal summary, and serves the `/reports` and `/health` endpoints. |
| `agent.py` | LiveKit agent worker registered as `post-call-analysis`. Dials the SIP participant over the Vobiz trunk, runs the conversation (Deepgram `nova-3` STT, `gpt-4o-mini`, OpenAI `tts-1`, Silero VAD, BVC telephony noise cancellation), captures both sides of the transcript, and posts lines and the room mapping to `main.py`. Exposes a `transfer_call` LLM tool. |
| `gemini_analyzer.py` | Prompt construction, language heuristic, the Gemini call with retry and model fallback, the `TranscriptAnalysis` / `QualityScores` / `KeyMoment` schemas and their validators, post-processing in `_validate_and_fix()`, and `compute_gate()`. |
| `audio_analyzer.py` | `pydub` recording analysis — noise-floor calibration, silence and gap detection, volume, clipping, per-channel stereo comparison, ring detection — merged with the MOS and jitter values from the CDR. Returns an `AudioReport` and never raises. |
| `analyzer.py` | Standalone CLI. Replays a saved report through the analyser, lists and compares reports, aggregates statistics, watches for new reports, and exports to CSV or JSON. |
| `make_call.py` | Creates a LiveKit agent dispatch for `post-call-analysis` with the destination number in the job metadata, in a room named `post-call-analysis-<digits>-<random>`. |
| `requirements.txt` | Python dependencies. |
| `.env.example` | Template for `.env`. |
| `recordings/` | Downloaded call audio. Created on import, gitignored. |
| `reports/` | JSON analysis reports. Created on import, gitignored. |

### Process topology

```
  make_call.py ──dispatch──► LiveKit Cloud ──SIP──► Vobiz trunk ──PSTN──► callee
                                  │                      │
                             agent.py                    │ Hangup webhook (CDR)
                                  │                      ▼
                    transcript lines + room mapping   main.py  ──► Vobiz Recording API
                    POST /internal/*  ───────────────►  │    ◄── recordings/*.mp3
                                                        │
                                      audio_analyzer ───┼─── gemini_analyzer
                                                        ▼
                                              reports/CALL_*.json  +  terminal summary
                                                        │
                                                   analyzer.py (replay, stats, export)
```

`agent.py` and `main.py` are separate processes. They share nothing but HTTP: the agent
posts to `BACKEND_URL`, and the server holds the state.

---

## Prerequisites

| Requirement | Why |
|-------------|-----|
| **Python 3.11 or newer** | The code uses `X \| None` annotations and modern typing throughout. |
| **ffmpeg** | `pydub` shells out to it to decode MP3 recordings. Without it, audio analysis returns an `AudioReport` with `error` set and the rest of the pipeline still runs. |
| **A [Vobiz](https://vobiz.ai) account** | An outbound SIP trunk with a webhook URL, plus an Auth ID and Auth Token from **Console → Settings → API Keys** for the Recording API. |
| **A [LiveKit](https://livekit.io) project** | API key, secret, and websocket URL, plus an outbound SIP trunk (`ST_…`) pointed at your Vobiz trunk. |
| **A [Deepgram](https://console.deepgram.com) API key** | Caller-side speech-to-text (`nova-3`, `language="multi"`). |
| **An [OpenAI](https://platform.openai.com) API key** | The in-call LLM (`gpt-4o-mini`) and TTS (`tts-1`, voice `alloy`). Not used for the analysis. |
| **A [Gemini](https://aistudio.google.com/apikey) API key** | The post-call transcript analysis. |
| **ngrok or another HTTPS tunnel** | Vobiz must reach your webhook server over public HTTPS while you develop locally. |

---

## Setup

**1. Clone and enter the repository**

```bash
git clone https://github.com/vobiz-ai/Livekit-Vobiz-Post-Call-Analysis.git
cd Livekit-Vobiz-Post-Call-Analysis
```

**2. Install ffmpeg**

```bash
brew install ffmpeg          # macOS
# sudo apt install ffmpeg    # Ubuntu/Debian
ffmpeg -version              # confirm it is on PATH
```

**3. Create a virtual environment and install dependencies**

```bash
python3 --version            # 3.11 or newer
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**4. Configure `.env`**

```bash
cp .env.example .env
```

Fill in the values — see [Configuration](#configuration) for the full table.

| Variable | Where to find it |
|----------|-----------------|
| `LIVEKIT_URL` | LiveKit Cloud dashboard |
| `LIVEKIT_API_KEY` | LiveKit Cloud → Settings → API Keys |
| `LIVEKIT_API_SECRET` | LiveKit Cloud → Settings → API Keys |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com) |
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) |
| `OUTBOUND_TRUNK_ID` | LiveKit Cloud → SIP → Outbound Trunks (`ST_…`) |
| `VOBIZ_SIP_DOMAIN` | Vobiz Console → SIP → your trunk |
| `VOBIZ_AUTH_ID` | Vobiz Console → Settings → API Keys |
| `VOBIZ_AUTH_TOKEN` | Vobiz Console → Settings → API Keys |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

**5. Start a tunnel**

```bash
ngrok http 9000
# Copy the https://xxxx.ngrok-free.app URL
```

**6. Point the Vobiz trunk at the tunnel**

Go to **[Vobiz Console → SIP → Outbound Trunks](https://console.vobiz.ai/app/sip/out/trunks)**
and edit your trunk:

```
Webhook URL:    https://xxxx.ngrok-free.app/webhook/vobiz
Webhook Method: POST
```

Record the same URL in `.env` for your own reference:

```bash
WEBHOOK_BASE_URL=https://xxxx.ngrok-free.app
```

**7. Confirm the server can see its credentials**

```bash
uvicorn main:app --port 9000 &
curl -s localhost:9000/health
# {"status":"ok","active_calls":0,"reports_saved":0,"recordings_saved":0,
#  "gemini_model":"gemini-2.0-flash","vobiz_auth_set":true}
```

If `vobiz_auth_set` is `false`, the recording download will be skipped and the audio
section of every report will be empty.

---

## Configuration

Every variable below is read by the code. `.env` is loaded with `load_dotenv(".env")` from
the working directory, so run the commands from the repository root.

### LiveKit and speech providers

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LIVEKIT_URL` | Yes | — | LiveKit websocket URL, e.g. `wss://your-project.livekit.cloud`. Read by `make_call.py` and by the LiveKit agent worker. |
| `LIVEKIT_API_KEY` | Yes | — | LiveKit API key. |
| `LIVEKIT_API_SECRET` | Yes | — | LiveKit API secret. |
| `DEEPGRAM_API_KEY` | Yes | — | Read by `livekit-plugins-deepgram` for the `nova-3` multilingual STT that produces the caller-side transcript. |
| `OPENAI_API_KEY` | Yes | — | Read by `livekit-plugins-openai` for the in-call `gpt-4o-mini` LLM and `tts-1` voice. Not used for post-call analysis. |

### Vobiz SIP and the agent

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OUTBOUND_TRUNK_ID` | Yes for outbound | `""` | LiveKit outbound SIP trunk ID (`ST_…`) passed to `create_sip_participant`. Empty means the dial fails and the job shuts down. |
| `VOBIZ_SIP_DOMAIN` | No | `""` | Used to build the transfer target `sip:<destination>@<VOBIZ_SIP_DOMAIN>`. If empty, `transfer_call` falls back to `tel:<destination>`. |
| `DEFAULT_TRANSFER_NUMBER` | No | `""` | Destination used when the `transfer_call` tool is invoked with no argument. Empty returns "No transfer number configured." |
| `BACKEND_URL` | No | `http://localhost:9000` | Where `agent.py` posts transcript lines and the room mapping. Must match where `main.py` is listening. |

### Vobiz Recording API

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VOBIZ_AUTH_ID` | No | `""` | Sent as the `X-Auth-ID` header and used in the path `GET https://api.vobiz.ai/api/v1/Account/{VOBIZ_AUTH_ID}/Recording/`. If this or the token is empty, recording download is skipped and the pipeline continues without audio metrics. |
| `VOBIZ_AUTH_TOKEN` | No | `""` | Sent as the `X-Auth-Token` header. |
| `RECORDING_WAIT_SECS` | No | `8` | Seconds to wait after hangup before the first Recording API call, giving Vobiz time to finish processing. |
| `RECORDING_RETRIES` | No | `3` | Attempts to fetch the recording metadata before giving up. |
| `RECORDING_RETRY_DELAY_SECS` | No | `5` | Delay between those attempts. |

### Webhook server

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `WEBHOOK_PORT` | No | `9000` | Port used when the server is started with `python main.py`. Ignored when you pass `--port` to `uvicorn`. |
| `WEBHOOK_BASE_URL` | No | — | Reference only — the public tunnel URL you pasted into the Vobiz Console. Nothing in the code reads it; it lives in `.env` so you can find it again. |

### Gemini analysis

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | Yes | `""` | Gemini API key. Without it (or its alternative below) the analyser returns an empty analysis with `all_models_failed`. |
| `GOOGLE_GENERATIVE_AI_API_KEY` | No | `""` | Alternative name for the same key, checked if `GEMINI_API_KEY` is unset. |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` | Primary model. Tried first, then the fallback chain. Also reported by `/health`. |
| `GEMINI_MAX_RETRIES` | No | `3` | Attempts per model before moving to the next one in the chain. |
| `GEMINI_RETRY_BASE_DELAY` | No | `1.5` | Backoff base — the delay before attempt *n* is `base ** n` seconds. |
| `MAX_TRANSCRIPT_LINES` | No | `200` | Token budget. Longer transcripts keep the first 20 and the last `MAX_TRANSCRIPT_LINES - 20` lines, with an elision marker between them. |

### Production gate thresholds

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GATE_MIN_DURATION_SECS` | No | `15` | Calls shorter than this are flagged `call_too_short` and FAIL. |
| `GATE_MAX_SILENCE_PCT` | No | `60` | Silence above this percentage is flagged `mostly_silence` and FAILs. |
| `GATE_MIN_MOS` | No | `2.0` | MOS below this is flagged `very_poor_audio_quality` and FAILs. |
| `GATE_REVIEW_SILENCE_PCT` | No | `30` | Silence above this is flagged `high_silence` for REVIEW. |
| `GATE_REVIEW_MOS` | No | `3.5` | MOS below this is flagged `poor_audio_quality` for REVIEW. |
| `GATE_REVIEW_MAX_JITTER_MS` | No | `50` | Jitter above this is flagged `high_jitter` for REVIEW. |

### Audio analysis thresholds

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUDIO_SILENCE_OFFSET_DB` | No | `14` | Added to the measured noise floor to derive the per-file silence threshold. |
| `AUDIO_SILENCE_THRESH_DB` | No | `-40` | Fixed fallback threshold used when the noise floor cannot be measured. |
| `AUDIO_MIN_SILENCE_MS` | No | `400` | Shortest run of quiet counted as silence. |
| `AUDIO_LONG_SILENCE_MS` | No | `3000` | Gap length reported individually in `silence_gaps`. |
| `AUDIO_CLIPPING_THRESH` | No | `-0.5` | Peak dBFS above which the recording is marked as clipping. |
| `AUDIO_LOW_VOLUME_THRESH` | No | `-35.0` | Mean dBFS below which the recording is marked low volume. |
| `AUDIO_ONE_SIDED_DIFF_DB` | No | `18.0` | Per-channel difference above which stereo audio is marked one-sided. |

### Analyzer CLI paths

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `REPORTS_DIR` | No | `reports` | Where `analyzer.py` reads and writes reports. `main.py` always uses `reports/`. |
| `RECORDINGS_DIR` | No | `recordings` | Where `analyzer.py` looks for downloaded audio. `main.py` always uses `recordings/`. |

---

## Running it

Four terminals, all from the repository root:

```bash
# Terminal 1 — webhook server + analysis engine
source .venv/bin/activate
uvicorn main:app --reload --port 9000

# Terminal 2 — public tunnel
ngrok http 9000

# Terminal 3 — LiveKit agent worker
source .venv/bin/activate
python agent.py start

# Terminal 4 — place a call
source .venv/bin/activate
python make_call.py --to +15550003333
```

`make_call.py` accepts exactly one flag, `--to`, and requires E.164 format. It prints the
dispatch it created:

```
Agent  : post-call-analysis
Calling: +15550003333
Room   : post-call-analysis-15550003333-7631
-------------------------------------------------------
Dispatched — ID: AD_xxxxxxxxxxxx
Agent is dialing. Post-call analysis fires automatically after hangup.
Watch main.py terminal for the analysis report.
```

Terminal 3 logs the room, the participant joining, the room mapping post, and the dial.
Terminal 1 logs each webhook as it arrives. Roughly ten seconds after hangup — the
`RECORDING_WAIT_SECS` pause plus the Gemini round trip — the report is printed:

```
╔════════════════════════════════════════════════════════╗
║         POST-CALL ANALYSIS  —  PRODUCTION GATE         ║
╠════════════════════════════════════════════════════════╣
║  CallUUID         a6d9b22e-46d7-4342-9d98-f31b7387f2d7 ║
║  From → To        +15550002222 → +15550003333           ║
║  Duration         2m 45s  (Billable: 158s)              ║
║  Cost             1.58 INR                              ║
║  Gate             PASS  (Quality: 84/100)               ║
╠════════════════════════════════════════════════════════╣
║  AUDIO QUALITY (from Vobiz CDR + pydub)                 ║
║  MOS Score        4.3 / 5.0                             ║
║  Jitter           12ms                                  ║
║  Silence          14.2%                                 ║
║  Ring Time        5s                                    ║
║  Noise Level      quiet                                 ║
╠════════════════════════════════════════════════════════╣
║  GEMINI 2.0 FLASH ANALYSIS                             ║
║  Summary          Caller asked about sending money to   ║
║                   a marketplace contact. Agent advised  ║
║                   caution and recommended not sending.  ║
╠════════════════════════════════════════════════════════╣
║  SCAM INDICATORS DETECTED                              ║
║    • Marketplace deal — unverified seller               ║
║    • Advance payment requested                          ║
╠════════════════════════════════════════════════════════╣
║  Intent           Buy a phone online — needs payment    ║
║  Resolution       UNRESOLVED                            ║
║  Sentiment        concerned → cautious                  ║
║  Language         English                               ║
╠════════════════════════════════════════════════════════╣
║  ACTION ITEMS                                          ║
║  • Do not send money to an unverified seller            ║
╠════════════════════════════════════════════════════════╣
║  QUALITY SCORES                                        ║
║  Overall:84  Clarity:90  Resolution:60  Empathy:88      ║
║  Confidence: 95/100                                    ║
╠════════════════════════════════════════════════════════╣
║  Gate reason: REVIEW: scam_indicators_detected(2)       ║
║  Report: reports/CALL_a6d9b22e_20260325_143012.json     ║
║  Audio:  recordings/a6d9b22e-46d7-4342.mp3              ║
╚════════════════════════════════════════════════════════╝
```

The same document is on disk at the path shown on the second-to-last line, and is
available over HTTP at `GET /reports/a6d9b22e`.

---

## Webhook events and transcript matching

| Event | What happens |
|-------|-------------|
| `CallInitiated` (`Allowed=true`) | Stores phone → call mapping, ready to collect transcript |
| `CallInitiated` (`Allowed=false`) | Saves a rejection report immediately with the reason and a `FAIL` verdict |
| `Hangup` | Triggers the full analysis pipeline asynchronously |

### How transcript matching works

A single call carries **three different identifiers**, and none of them match each other:

| ID | Source | Example |
|----|--------|---------|
| `CallInitiated.CallUUID` | Vobiz | `5Jnsfveg5En1f2GBqM3tnRK2XhHA` |
| `Hangup.CallUUID` | Vobiz CDR | `926fd4e5-0887-41d4-88bd-4bb42f8bd1ab` |
| LiveKit SIP participant ID | LiveKit | `SCL_jHpRcjcaqRxy` |

**Solution:** the `From` phone number is the only field consistent across all events and
the agent's room name (`post-call-analysis-15550003333-xxxx`). All transcript lines are
stored under `phone_store[phone]` and resolved by `hangup.From` at analysis time.

`_run_analysis_pipeline()` tries four strategies in order and stops at the first that
yields lines:

1. **Phone match** — `phone_store[hangup.From]`. The normal path.
2. **SIP call ID** — `sip_to_phone[hangup.SIPCallID]`, populated by `CallInitiated` and by
   the agent's room-mapping post.
3. **Room name** — the last 10 digits of `From` are matched against every registered room
   name, which contains the dialled digits.
4. **Longest active transcript** — the record in `phone_store` with the most lines. A
   development convenience; with concurrent calls it can attribute the wrong transcript,
   so watch for `Transcript found via fallback` in the log.

---

## Production gate rules

### FAIL (any one condition)

| Flag | Condition |
|------|-----------|
| `call_too_short` | Duration < `GATE_MIN_DURATION_SECS` (15s) |
| `mostly_silence` | Silence > `GATE_MAX_SILENCE_PCT` (60%) of recording |
| `very_poor_audio_quality` | MOS < `GATE_MIN_MOS` (2.0) |
| `intent_not_captured` | `caller_intent` is empty, `unknown`, `none`, `n/a`, or `unclear` |
| `call_abandoned` | `call_resolution == "ABANDONED"` |
| `critically_low_quality` | Overall quality < 25/100 |
| `multiple_scam_indicators` | 3 or more scam signals detected |

### REVIEW (no FAIL, but one or more)

| Flag | Condition |
|------|-----------|
| `high_silence` | Silence above `GATE_REVIEW_SILENCE_PCT` (30%) |
| `poor_audio_quality` | MOS below `GATE_REVIEW_MOS` (3.5) |
| `high_jitter` | Jitter above `GATE_REVIEW_MAX_JITTER_MS` (50ms) |
| `one_sided_audio` | One channel much louder than the other |
| `call_unresolved` / `call_escalated` | Issue not resolved |
| `low_quality_score` | Overall quality 25–50 |
| `multiple_unanswered_questions` | 2+ caller questions not addressed |
| `high_confusion` | 2+ confusion signals detected |
| `audio_clipping` | Peak amplitude near 0dBFS |
| `low_audio_volume` | Mean volume below `AUDIO_LOW_VOLUME_THRESH` |
| `scam_indicators_detected(N)` | Any scam signal found |
| `low_analysis_confidence` | Analyser confidence < 40 |

### PASS

All of the above conditions are clear. The `reason` string then reports the overall score
and the resolution state.

Note that FAIL and REVIEW conditions overlap deliberately: a call with MOS 1.8 collects
both `very_poor_audio_quality` (FAIL) and `poor_audio_quality` (REVIEW), and the verdict
is `FAIL` with both flags listed.

---

## Report JSON structure

```json
{
  "call_uuid": "926fd4e5-0887-41d4-88bd-4bb42f8bd1ab",
  "generated_at": "2026-03-25T14:30:12Z",

  "call_meta": {
    "from": "+15550002222",
    "to": "+15550003333",
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
    "summary": "Caller inquired about sending money to a marketplace contact…",
    "caller_intent": "Send payment to an online seller for a phone",
    "caller_sentiment": "trusting → cautious",
    "agent_sentiment": "professional, cautionary",
    "language_detected": "English",
    "key_moments": [
      { "at": "0:18", "event": "Caller named the marketplace as the source" },
      { "at": "1:32", "event": "Agent advised not to send money" }
    ],
    "action_items": [
      "Do not send money to an unverified seller",
      "Verify seller identity before any payment"
    ],
    "unanswered_questions": [],
    "topics_covered": ["UPI payment", "online marketplace", "scam awareness"],
    "call_resolution": "UNRESOLVED",
    "confusion_signals": [],
    "scam_indicators": [
      "Marketplace deal — unverified seller",
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

A rejected call — `CallInitiated` with `Allowed=false` — produces a much shorter
`reports/REJECTED_<uuid>_<ts>.json` containing `call_uuid`, `from`, `to`, `reason`, and a
`gate` of `{"verdict": "FAIL", "flags": ["call_rejected"], …}`.

---

## HTTP API reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/webhook/vobiz` | Vobiz SIP trunk webhook. Handles `CallInitiated` and `Hangup`; always returns `{"ok": true}`. |
| `GET`  | `/reports` | The 50 most recent reports, newest first, each summarised as filename, `call_uuid`, `generated_at`, gate verdict, overall quality, and duration. |
| `GET`  | `/reports/{uuid_prefix}` | The full report JSON whose filename contains the prefix; the most recently modified match wins. `404` if nothing matches. |
| `GET`  | `/health` | `status`, `active_calls`, `reports_saved`, `recordings_saved`, `gemini_model`, `vobiz_auth_set`. |
| `POST` | `/internal/transcript` | Called by `agent.py` for every finalised line. Hidden from the OpenAPI schema. |
| `POST` | `/internal/room_mapping` | Called by `agent.py` when the SIP participant joins, registering room ↔ phone ↔ SIP call ID. Hidden from the OpenAPI schema. |

The interactive OpenAPI docs are at `http://localhost:9000/docs` while the server runs.

---

## Analyzer CLI reference

Re-analyse any call without placing a new one. Exactly one mode flag is required.

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

# Batch summary over every saved report
python analyzer.py --batch

# Batch for a specific date
python analyzer.py --batch --date 2026-03-25

# Export all reports to CSV
python analyzer.py --export csv

# Export all reports to a JSON array
python analyzer.py --export json
```

Modifiers, valid alongside the single-call modes:

| Flag | Effect |
|------|--------|
| `--model MODEL` | Overrides `GEMINI_MODEL` for this run by setting the environment variable before the analyser loads. |
| `--transcript-only` | Reuses the stored `audio_analysis` instead of re-reading the recording. |
| `--audio-only` | Reuses the stored `transcript_analysis` instead of calling Gemini. |
| `--no-save` | Prints the result without writing `<report>_reanalyzed.json`. |
| `--verbose` / `-v` | Raises the log level to `INFO`. |
| `--date YYYY-MM-DD` | Filters `--batch` to one day. |

Re-analysis writes to a sibling file — `CALL_abc_2026….json` becomes
`CALL_abc_2026…_reanalyzed.json` — so the original report is never overwritten.

```bash
# Re-analyse the latest call with a different model
python analyzer.py --latest --model gemini-2.5-pro
```

---

## Transcript analyser behaviour

### Retry and fallback chain

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

Retries happen only on transient errors — the error string is matched against `429`,
`500`, `503`, `RESOURCE_EXHAUSTED`, `UNAVAILABLE`, `INTERNAL`, `timeout`, and `rate`.
Anything else abandons that model immediately and moves to the next in the chain.

### Response validation

Every field returned by the model goes through Pydantic validators:

- **Scores** — clamped to `[0, 100]` by `field_validator`.
- **Resolution** — forced to one of `RESOLVED/UNRESOLVED/ESCALATED/TRANSFERRED/ABANDONED`.
- **Lists** — de-duplicated, empty strings removed.
- **KeyMoments** — handles a dict, a Python repr string (`"{'at': '0:32', …}"`), or a
  plain string.
- **Consistency** — abandoned calls are capped at quality 60 with resolution capped at 20;
  resolved calls get a resolution score of at least 60.

Two further corrections run in `_validate_and_fix()`:

- A call shorter than 15 seconds is forced to `ABANDONED` unless it was `TRANSFERRED`, and
  its confidence is capped at 40.
- If the transcript contains no `caller` lines at all, `caller_never_spoke` is added to
  `confusion_signals` and confidence is capped at 30.

If structured parsing fails, the raw response text is stripped of markdown fences, the
first `{…}` block is extracted with a regex, and parsing is retried — with a second pass
that coerces `key_moments` before giving up.

### Language and fraud context

`_detect_language()` counts Hinglish markers (`haan`, `nahi`, `theek`, `paisa`, `aap`, …)
against total words and labels the transcript *Hinglish*, *primarily English with some
Hindi*, or *English* above ratios of 0.15 and 0.05. The label is passed to the model as a
hint rather than used as a filter.

The system prompt carries a scam-pattern taxonomy — online marketplace fake sellers,
messaging-app advance payment, cheap-electronics fraud, crypto investment schemes,
KYC-expiry scams, lottery and prize claims, fake tech support — plus a scoring rubric and
explicit instructions to lower `analysis_confidence` for short, one-word, or unclear
transcripts. Calls under 30 seconds get an additional prompt paragraph telling the model to
score conservatively.

---

## Audio analyser behaviour

### Auto-calibrated silence detection

Instead of a fixed -40dBFS threshold, the threshold is computed per file:

```
noise_floor = average dBFS of the quietest 10% of 100ms chunks
silence_threshold = noise_floor + AUDIO_SILENCE_OFFSET_DB   (default 14dB)
```

This avoids false positives on noisy telephony recordings. When the noise floor cannot be
measured, `AUDIO_SILENCE_THRESH_DB` (-40) is used instead, and the value actually applied
is reported as `silence_threshold_used`.

### What gets analysed

| Metric | Method |
|--------|--------|
| Silence % | pydub `detect_silence` with the auto-threshold |
| Long gaps > 3s | Silent ranges with timestamp |
| Speech % | 100 − silence_pct |
| Volume | mean dBFS |
| Peak / clipping | `max_dBFS` above `AUDIO_CLIPPING_THRESH` |
| Noise floor | Quietest 10% of chunks |
| Noise level | `quiet / moderate / noisy / very_noisy` |
| Ring detection | Periodic loud/silent pattern in the first 6 seconds |
| Stereo channels | Per-channel volume → dominant speaker |
| One-sided audio | More than `AUDIO_ONE_SIDED_DIFF_DB` between channels |
| MOS, jitter, duration, billsec | Passed through from the Vobiz Hangup CDR |

### Failure handling

- Minimum size check (< 1KB → skip).
- Minimum duration check (< 0.5s → skip).
- `_safe_load()` tries `mp3 / ogg / wav / flac / m4a`.
- `MemoryError` on very large files is caught.
- Every failure still returns a valid `AudioReport` with `analyzed=false` and `error` set,
  so the pipeline continues and the report still gets written.

---

## Extending

### Add a scam pattern

In `gemini_analyzer.py`, extend the list inside `_SYSTEM_PROMPT`:

```python
_SYSTEM_PROMPT = """
...
    • Your new pattern here
...
"""
```

### Add a gate rule

In `gemini_analyzer.py`, add to `compute_gate()`:

```python
# FAIL if confidence is very low on a long call
if ta.analysis_confidence < 20 and duration_seconds > 60:
    fail_flags.append("very_low_confidence_long_call")
```

### Change the model

```bash
# In .env
GEMINI_MODEL=gemini-2.5-pro

# Or per run
python analyzer.py --latest --model gemini-2.5-pro
```

### Send the report somewhere

In `main.py`, at the end of `_run_analysis_pipeline()` after `_print_report()`:

```python
await post_to_crm(report)   # your own function
```

Do this inside a `try`/`except`. The pipeline runs in a background task, so an unhandled
exception there is logged by asyncio and silently loses the rest of the step.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| No `Webhook: Event=…` line ever appears in the server log | The trunk is not pointed at your tunnel, or the tunnel restarted and changed URL | Confirm ngrok is running, then re-paste the current `https://…/webhook/vobiz` URL into the Vobiz Console trunk settings and into `WEBHOOK_BASE_URL`. |
| `Final transcript: 0 lines` and a summary of `Analysis could not be completed (no_transcript)` | `agent.py` was not running, or it posted to the wrong place | Start `python agent.py start` before dialling, and make sure `BACKEND_URL` (default `http://localhost:9000`) matches the port `uvicorn` is bound to. |
| `Transcript found via fallback` in the log, and the report describes a different call | Two calls were in flight and neither the phone nor the SIP call ID matched, so step 4 picked the record with the most lines | Run one call at a time in development. In production, make sure `CallInitiated` reaches the server so `sip_to_phone` is populated before hangup. |
| `VOBIZ_AUTH_ID/TOKEN not set — skipping recording download`; audio fields all `null` | Recording API credentials are missing | Add `VOBIZ_AUTH_ID` and `VOBIZ_AUTH_TOKEN` from Vobiz Console → Settings → API Keys and check `/health` reports `"vobiz_auth_set": true`. |
| `Recording not ready yet (attempt 1/3)` repeated, then no audio | The recording was still being processed when the pipeline asked for it | Raise `RECORDING_WAIT_SECS` (try 15) and `RECORDING_RETRIES`. |
| Every report reads `Analysis could not be completed (all_models_failed)` | `GEMINI_API_KEY` is missing or invalid — `_get_client()` raises, the error is non-transient, and the whole chain is abandoned | Set a valid key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey); run with `--verbose` to see the underlying error. |
| A call that clearly connected is scored `ABANDONED` and fails | The call was under 15 seconds — `_validate_and_fix()` forces `ABANDONED` and the gate adds `call_too_short` | Expected for very short calls. Lower `GATE_MIN_DURATION_SECS` if your traffic is legitimately brief. |
| `Couldn't find ffprobe or avconv` / audio `error` field set | `pydub` cannot find ffmpeg | `brew install ffmpeg` (macOS) or `sudo apt install ffmpeg` (Linux), then confirm `ffmpeg -version`. |
| MOS and Jitter show `N/A` although the recording downloaded | The Hangup payload carried no `MOS`/`Jitter` keys — these come from the CDR, not from the audio | Nothing to fix locally; the rest of the audio analysis is unaffected. |
| Transcripts vanish between calls, or `/health` shows `active_calls: 0` after activity | `phone_store` is in-process memory, and `uvicorn --reload` restarts the process on every file save | Avoid editing files mid-call, and do not run `uvicorn` with more than one worker — the stores are not shared across processes. |
| `Port 9000 already in use` | A previous server is still bound | `kill $(lsof -ti :9000)` then restart. |
| `KeyMoment` validation error on an older report | The report predates the coercing parser | Re-run it through `python analyzer.py --report <file>`, which applies the current validators. |

---

## Security notes

- **Transcripts are personal data, and they leave your machine.** The full text of every
  call — both sides — is sent to Google's Gemini API for analysis, and the caller's number
  and the dialled number are included in the prompt context. Nothing is redacted before
  that call is made. Before running this against real traffic, decide whether your consent
  notices, data-processing agreements, and retention policy cover sending call content to
  a third-party model provider, and strip or tokenise anything you cannot send.
- **Recordings and reports are written to plain local disk.** `recordings/*.mp3` and
  `reports/*.json` contain full audio and the full transcript. Both directories are
  gitignored, neither is encrypted, and nothing ever deletes them. Add retention and
  access control before using this beyond a laptop.
- **`/webhook/vobiz` and both `/internal/*` endpoints are unauthenticated.** Anything that
  can reach the port can inject a fake `Hangup` event or fabricate transcript lines. CORS
  is also set to `allow_origins=["*"]`. That is deliberate for local development behind a
  tunnel; put the server behind a verified signature, a shared secret, or a private network
  before exposing it.
- **A tunnel URL is a public URL.** While ngrok is running, your webhook server is on the
  open internet. Stop the tunnel when you are not testing.
- **Recording URLs are credentials-adjacent.** `recording_url` from the Vobiz Recording API
  is stored verbatim in the report JSON. Treat reports as sensitive artefacts, not as logs
  you can paste into a ticket.
- **`.env` holds five separate provider secrets** — LiveKit, Deepgram, OpenAI, Gemini, and
  the Vobiz Auth ID and Token. It is gitignored; keep it that way, use `.env.example` as
  the template, and rotate anything that has ever been committed or shared.
- **Transfer destinations are model-chosen.** The `transfer_call` tool takes a free-text
  `destination` and falls back to `DEFAULT_TRANSFER_NUMBER`. Restrict it to an allowlist
  before letting it dial arbitrary numbers.

---

## Roadmap

> Planned improvements to this example. Ideas and pull requests are welcome —
> open an issue to discuss anything here.

- [ ] **Durable storage for reports and in-flight transcripts.** `phone_store`,
      `call_store`, `sip_to_phone`, and `room_to_phone` are plain dictionaries in one
      process, and reports are loose JSON files read back with `glob()`. A small database
      would survive a restart, make `/reports` queryable by date, verdict, or number, and
      let more than one worker serve the webhook.
- [ ] **Redact the transcript before it reaches the model.** The prompt currently contains
      the raw text plus both phone numbers. A redaction pass over card numbers, UPI IDs,
      and account details — with the caller's number tokenised — would narrow what leaves
      the deployment.
- [ ] **Authenticate the webhook and the internal endpoints.** A signature check on
      `/webhook/vobiz` and a shared secret on `/internal/*`, with CORS narrowed from `*`,
      would make the server safe to expose directly rather than only through a tunnel.
- [ ] **Ship the verdict somewhere.** The pipeline ends at a printed box and a file. Native
      delivery of `REVIEW` and `FAIL` reports to a webhook, a queue, or a ticketing system
      would close the loop the [Extending](#extending) section currently leaves to the
      reader.
- [ ] **A backfill mode.** `analyzer.py --batch` only summarises reports that already
      exist locally. Pulling the Vobiz CDR and Recording API for a date range would let you
      analyse calls that happened while the server was not running — audio-only, since no
      live transcript was captured.
- [ ] **A test suite.** There are none. Unit coverage for `compute_gate()` thresholds,
      `_validate_and_fix()` corrections, and the four-step transcript matcher, plus a
      fixture-driven test that stubs the Gemini client, would let the scoring rules change
      safely.
- [ ] **Metrics and tracing.** Pass rate, average quality by day, Gemini latency and
      fallback frequency, and recording-fetch retry counts are all logged as text today;
      emitting them as metrics would turn the log lines into a dashboard.

---

## Contributing

Issues and pull requests are welcome. If you are changing behaviour rather than wording,
please:

1. Fork the repository and work on a branch.
2. Keep changes grounded in the existing structure — new configuration through environment
   variables with defaults, new gate flags through `compute_gate()`.
3. Run a real call end to end before opening the PR, then re-run the saved report through
   `python analyzer.py --report <file>` to confirm the analyser path still works.
4. Check the module still imports cleanly:

   ```bash
   source .venv/bin/activate
   python -c "import main, agent, analyzer, gemini_analyzer, audio_analyzer"
   ```

5. Never commit `.env`, `recordings/`, or `reports/` — all three are gitignored, and the
   last two contain call content.

Describe what you changed and how you verified it. If you are unsure whether an idea fits,
open an issue first.

---

## License

Released under the [MIT License](./LICENSE) © Vobiz.

MIT is permissive: you may use, modify, and redistribute this code, including in
closed-source commercial products, provided the copyright notice and licence text
are retained. There is no warranty. If your organisation needs a different
licensing arrangement, contact [piyush@vobiz.ai](mailto:piyush@vobiz.ai).

---

## Built by Team Vobiz

[Vobiz](https://vobiz.ai) is a programmable voice and SIP-trunking platform for
voice APIs, SIP trunking, and AI voice agents. This repository is built and
maintained by the Vobiz team.

**Maintainer:** Piyush Sahoo — [piyush@vobiz.ai](mailto:piyush@vobiz.ai) · [LinkedIn](https://www.linkedin.com/in/piyush-s713/)

Questions, or want to talk through an integration? Open an issue on this repo,
or reach out directly at [piyush@vobiz.ai](mailto:piyush@vobiz.ai).

**Useful links:** [Docs](https://docs.vobiz.ai) · [API reference](https://docs.vobiz.ai/api-reference) · [Sign up](https://vobiz.ai)
