"""
make_call.py — Dispatch an outbound call to the post-call analysis agent
Usage:
    python make_call.py --to +91XXXXXXXXXX
"""
import argparse
import asyncio
import json
import os
import random

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env")

AGENT_NAME = "post-call-analysis"


async def main():
    parser = argparse.ArgumentParser(description="Place a call via post-call-analysis agent")
    parser.add_argument("--to", required=True, help="E.164 phone number e.g. +919148227303")
    args = parser.parse_args()

    phone = args.to.strip()
    if not phone.startswith("+"):
        print("ERROR: Phone number must be in E.164 format (+91...)")
        return

    url        = os.getenv("LIVEKIT_URL")
    api_key    = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not (url and api_key and api_secret):
        print("ERROR: Missing LiveKit credentials in .env")
        return

    lk   = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)
    room = f"{AGENT_NAME}-{phone.replace('+', '')}-{random.randint(1000, 9999)}"

    print(f"\nAgent  : {AGENT_NAME}")
    print(f"Calling: {phone}")
    print(f"Room   : {room}")
    print("-" * 55)

    try:
        dispatch = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room,
                metadata=json.dumps({"phone_number": phone}),
            )
        )
        print(f"Dispatched — ID: {dispatch.id}")
        print("Agent is dialing. Post-call analysis fires automatically after hangup.")
        print("Watch main.py terminal for the analysis report.")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
