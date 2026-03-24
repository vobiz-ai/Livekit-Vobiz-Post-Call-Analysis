"""
agent.py — Post-Call Analysis LiveKit Agent
=============================================
Standard outbound voice agent that:
  1. Places the call via Vobiz SIP trunk
  2. Captures EVERY transcript line (agent + caller) in real-time
  3. POSTs each line to main.py /internal/transcript
  4. On participant_connected: registers SIP call ID mapping
  5. On room disconnect: posts final mapping to main.py

The actual post-call analysis is triggered by the Vobiz Hangup webhook
arriving at main.py — NOT from this agent.

Run:
    python agent.py start
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from dotenv import load_dotenv
from livekit import agents, api
from livekit.agents import Agent, AgentSession, RoomInputOptions, llm
from livekit.plugins import deepgram, noise_cancellation, openai, silero

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("post-call.agent")

OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID", "")
SIP_DOMAIN        = os.getenv("VOBIZ_SIP_DOMAIN", "")
BACKEND_URL       = os.getenv("BACKEND_URL", "http://localhost:9000")
DEFAULT_TRANSFER  = os.getenv("DEFAULT_TRANSFER_NUMBER", "")


# ---------------------------------------------------------------------------
# Per-call state
# ---------------------------------------------------------------------------

class CallState:
    def __init__(self, phone: str, room_name: str):
        self.phone_number  = phone
        self.room_name     = room_name
        self.sip_call_id:  Optional[str] = None
        self.call_start    = time.time()
        self._reported_ids: set = set()


# ---------------------------------------------------------------------------
# Backend reporting
# ---------------------------------------------------------------------------

async def _post_transcript(state: CallState, speaker: str, text: str):
    if not text.strip():
        return
    payload = {
        "sip_call_id": state.sip_call_id or "",
        "room_name":   state.room_name,
        "phone":       state.phone_number,   # ← primary key for matching
        "speaker":     speaker,
        "text":        text,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with aiohttp.ClientSession() as http:
            await http.post(f"{BACKEND_URL}/internal/transcript",
                            json=payload, timeout=aiohttp.ClientTimeout(total=3))
    except Exception as exc:
        logger.debug("Transcript post failed (non-critical): %s", exc)


async def _post_room_mapping(state: CallState):
    if not state.sip_call_id:
        return
    payload = {
        "room_name":   state.room_name,
        "sip_call_id": state.sip_call_id,
        "phone":       state.phone_number,
    }
    try:
        async with aiohttp.ClientSession() as http:
            await http.post(f"{BACKEND_URL}/internal/room_mapping",
                            json=payload, timeout=aiohttp.ClientTimeout(total=3))
        logger.info("Room mapping posted: %s → SIP %s", state.room_name, state.sip_call_id)
    except Exception as exc:
        logger.warning("Room mapping post failed: %s", exc)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class ConversationAgent(Agent):
    """
    A general-purpose outbound voice agent.
    Greets the caller, has a conversation, and can transfer if needed.
    The quality of this conversation is what gets analyzed post-call.
    """

    def __init__(self, state: CallState, ctx: agents.JobContext) -> None:
        self._state = state
        self._ctx   = ctx
        super().__init__(
            instructions="""
            You are a professional voice assistant calling from Vobiz.

            Key behaviors:
            1. Introduce yourself clearly as an AI assistant from Vobiz.
            2. Ask how you can help the caller today.
            3. Be concise and respectful — this is a phone call.
            4. If asked to transfer, use the transfer_call tool.
            5. If the caller wants to end the call, say a polite goodbye.

            Remember: this call will be recorded and analyzed for quality.
            """,
        )

    async def on_enter(self) -> None:
        # Wait for SIP participant to join
        participant_joined: asyncio.Event = getattr(self._state, "_participant_joined", None)

        if participant_joined and not participant_joined.is_set():
            try:
                await asyncio.wait_for(participant_joined.wait(), timeout=30.0)
                await asyncio.sleep(0.3)
            except asyncio.TimeoutError:
                logger.warning("No answer within 30s")
                return

        await self.session.generate_reply(
            instructions="Greet the caller, introduce yourself as a Vobiz AI assistant, and ask how you can help."
        )

    @llm.function_tool(description="Transfer the call to a human agent or specific number.")
    async def transfer_call(self, destination: str = "") -> str:
        dest = destination or DEFAULT_TRANSFER
        if not dest:
            return "No transfer number configured."

        clean = dest.replace("tel:", "").replace("sip:", "")
        uri   = f"sip:{clean}@{SIP_DOMAIN}" if SIP_DOMAIN else f"tel:{clean}"

        for p in self._ctx.room.remote_participants.values():
            try:
                await self._ctx.api.sip.transfer_sip_participant(
                    api.TransferSIPParticipantRequest(
                        room_name=self._ctx.room.name,
                        participant_identity=p.identity,
                        transfer_to=uri,
                        play_dialtone=False,
                    )
                )
                logger.info("Transferred to %s", uri)
                return f"Transfer initiated to {dest}."
            except Exception as exc:
                logger.error("Transfer failed: %s", exc)
        return "Transfer failed."


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: agents.JobContext):
    logger.info("Room: %s", ctx.room.name)

    phone: str = ""
    try:
        if ctx.job.metadata:
            phone = json.loads(ctx.job.metadata).get("phone_number", "")
    except Exception:
        pass

    state = CallState(phone=phone, room_name=ctx.room.name)

    # Shared event — set when SIP participant answers
    participant_joined        = asyncio.Event()
    state._participant_joined = participant_joined  # type: ignore[attr-defined]

    # Register participant_connected BEFORE dialing
    @ctx.room.on("participant_connected")
    def on_participant(participant):
        logger.info("Participant joined: %s", participant.identity)
        participant_joined.set()
        # Extract SIP call ID from participant attributes
        sip_id = (
            participant.attributes.get("sip.callID")
            or participant.attributes.get("sip_call_id")
            or participant.identity
        )
        state.sip_call_id = sip_id
        asyncio.ensure_future(_post_room_mapping(state))

    @ctx.room.on("disconnected")
    def on_disconnect(_reason=None):
        logger.info("Room disconnected: %s", ctx.room.name)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM(model="gpt-4o-mini"),    # LLM for conversation (NOT analysis)
        tts=openai.TTS(model="tts-1", voice="alloy"),
        vad=silero.VAD.load(),
    )

    # ── Capture AGENT speech ──────────────────────────────────────────────
    @session.on("conversation_item_added")
    def on_item(event):
        msg: llm.ChatMessage = event.item
        if msg.role != "assistant":
            return
        text = msg.text_content or ""
        if not text.strip() or msg.id in state._reported_ids:
            return
        state._reported_ids.add(msg.id)
        asyncio.ensure_future(_post_transcript(state, "agent", text))

    # ── Capture CALLER speech (final STT results) ─────────────────────────
    @session.on("user_input_transcribed")
    def on_transcribed(event):
        if not event.is_final:
            return
        text = event.transcript or ""
        asyncio.ensure_future(_post_transcript(state, "caller", text))

    await session.start(
        room=ctx.room,
        agent=ConversationAgent(state, ctx),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    if phone and phone.startswith("+"):
        logger.info("Dialing %s…", phone)
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=OUTBOUND_TRUNK_ID,
                    sip_call_to=phone,
                    participant_identity=f"sip_{phone.replace('+', '')}",
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered.")
        except Exception as exc:
            logger.error("Dial failed: %s", exc)
            ctx.shutdown()
    else:
        # Inbound — on_enter() handles greeting
        logger.info("No phone number in metadata — treating as inbound.")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="post-call-analysis",
        )
    )
