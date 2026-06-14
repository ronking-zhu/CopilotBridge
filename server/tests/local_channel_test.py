"""End-to-end local test of the bot WITHOUT Azure or Teams.

It stands up a tiny fake Bot Connector (the thing the real Azure Bot Service would be),
sends a message Activity to the running bridge server's /api/messages, and captures the
activities the bot sends back (typing, ack, and the final Copilot result).

Usage (with the server already running via run_server.ps1):
    python local_channel_test.py
    python local_channel_test.py "list files in this folder"

This is the closest thing to a real Teams round-trip you can run on a machine whose
tenant blocks Azure Bot credentials.
"""

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone

from aiohttp import web, ClientSession

import builtins


def print(*args, **kwargs):  # noqa: A001 - shadow builtin so emoji never crash a cp1252 console
    msg = (kwargs.get("sep", " ")).join(str(a) for a in args)
    try:
        sys.stdout.buffer.write((msg + kwargs.get("end", "\n")).encode("utf-8", "replace"))
        sys.stdout.buffer.flush()
    except Exception:
        builtins.print(msg.encode("ascii", "replace").decode("ascii"))


BRIDGE_URL = "http://localhost:3978/api/messages"
CONNECTOR_HOST = "localhost"
CONNECTOR_PORT = 3979

captured = []


async def _record(request: web.Request) -> web.Response:
    """Pretend to be the Bot Connector: record the activity, ack with a resource id."""
    body = await request.json()
    captured.append(body)
    text = body.get("text")
    if body.get("type") == "message" and text:
        print(f"  <- bot says: {text!r}")
    elif body.get("type"):
        print(f"  <- bot activity: {body.get('type')}")
    return web.json_response({"id": str(uuid.uuid4())})


def _make_activity(prompt: str) -> dict:
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channelId": "emulator",
        "serviceUrl": f"http://{CONNECTOR_HOST}:{CONNECTOR_PORT}",
        "from": {"id": "user-local", "name": "Local Tester", "aadObjectId": "local-test-aad"},
        "recipient": {"id": "bot", "name": "Copilot Bridge"},
        "conversation": {"id": f"conv-{uuid.uuid4().hex[:8]}"},
        "text": prompt,
        "locale": "en-US",
    }


async def main():
    prompt = " ".join(sys.argv[1:]) or "Reply with exactly: CHANNEL_ROUNDTRIP_OK"

    # 1. Start the fake connector.
    app = web.Application()
    app.router.add_post("/v3/conversations/{conversation_id}/activities", _record)
    app.router.add_post("/v3/conversations/{conversation_id}/activities/{activity_id}", _record)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONNECTOR_HOST, CONNECTOR_PORT)
    await site.start()
    print(f"Fake connector listening on http://{CONNECTOR_HOST}:{CONNECTOR_PORT}")

    # 2. Send a message to the bridge server.
    activity = _make_activity(prompt)
    print(f"Sending prompt to bot: {prompt!r}\n")
    try:
        async with ClientSession() as session:
            async with session.post(BRIDGE_URL, json=activity) as resp:
                print(f"POST /api/messages -> HTTP {resp.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR: could not reach the bridge server at {BRIDGE_URL}.")
        print("Start it first:  .\\scripts\\run_server.ps1   (or python app.py)")
        print(f"Details: {exc}")
        await runner.cleanup()
        return

    # 3. Wait for the (async) Copilot result to come back to the connector.
    print("\nWaiting for Copilot result (up to 120s)...")
    deadline = asyncio.get_event_loop().time() + 120
    found = False
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(1)
        for act in captured:
            txt = (act.get("text") or "")
            if act.get("type") == "message" and "Working on it" not in txt and txt.strip():
                found = True
                break
        if found:
            break

    await runner.cleanup()

    print("\n==================== RESULT ====================")
    msgs = [a.get("text") for a in captured if a.get("type") == "message" and a.get("text")]
    for i, m in enumerate(msgs, 1):
        print(f"[{i}] {m}")
    print("===============================================")
    print("PASS" if found else "NO RESULT (check server logs / Copilot)")


if __name__ == "__main__":
    asyncio.run(main())
