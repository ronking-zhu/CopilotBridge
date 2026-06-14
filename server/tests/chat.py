"""Interactive REPL to drive the Copilot agent through the running bridge server.

Keeps ONE conversation for the whole session, so the Copilot CLI keeps context across
turns (same --session-id). Type instructions; the agent runs them on the host (sandboxed
to workspace/ by default) and prints the result.

Prereq: the server is running (`.\\scripts\\run_server.ps1` or `python app.py`).

    python chat.py
"""

import asyncio
import builtins
import sys
import uuid
from datetime import datetime, timezone

from aiohttp import web, ClientSession


def print(*args, **kwargs):  # noqa: A001 - UTF-8 safe on a cp1252 Windows console
    msg = (kwargs.get("sep", " ")).join(str(a) for a in args)
    try:
        sys.stdout.buffer.write((msg + kwargs.get("end", "\n")).encode("utf-8", "replace"))
        sys.stdout.buffer.flush()
    except Exception:
        builtins.print(msg.encode("ascii", "replace").decode("ascii"))


BRIDGE_URL = "http://localhost:3978/api/messages"
CONNECTOR_HOST = "localhost"
CONNECTOR_PORT = 3979
CONVERSATION_ID = f"chat-{uuid.uuid4().hex[:8]}"

replies: "asyncio.Queue[str]" = None  # set in main()


async def _record(request: web.Request) -> web.Response:
    """Stand in for the Bot Connector: collect the agent's reply text."""
    body = await request.json()
    text = body.get("text") or ""
    if body.get("type") == "message" and text and "Working on it" not in text:
        await replies.put(text)
    return web.json_response({"id": str(uuid.uuid4())})


def _activity(prompt: str) -> dict:
    return {
        "type": "message",
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "channelId": "console",
        "serviceUrl": f"http://{CONNECTOR_HOST}:{CONNECTOR_PORT}",
        "from": {"id": "chat-user", "name": "Console", "aadObjectId": "chat-user"},
        "recipient": {"id": "bot", "name": "Copilot Bridge"},
        "conversation": {"id": CONVERSATION_ID},
        "text": prompt,
        "locale": "en-US",
    }


async def _send_and_wait(session: ClientSession, prompt: str, timeout: int = 600):
    try:
        async with session.post(BRIDGE_URL, json=_activity(prompt)) as resp:
            if resp.status not in (200, 201, 202):
                print(f"(server returned HTTP {resp.status})")
                return
    except Exception as exc:  # noqa: BLE001
        print(f"(could not reach the server at {BRIDGE_URL} - is it running?)  {exc}")
        return

    print("...thinking...")
    try:
        first = await asyncio.wait_for(replies.get(), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"(no reply within {timeout}s)")
        return

    chunks = [first]
    while True:  # drain any additional chunks of a long reply
        try:
            chunks.append(await asyncio.wait_for(replies.get(), timeout=2.0))
        except asyncio.TimeoutError:
            break

    print("\nCopilot:\n" + "\n".join(chunks) + "\n")


async def main():
    global replies
    replies = asyncio.Queue()

    app = web.Application()
    app.router.add_post("/v3/conversations/{cid}/activities", _record)
    app.router.add_post("/v3/conversations/{cid}/activities/{aid}", _record)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, CONNECTOR_HOST, CONNECTOR_PORT).start()

    print(f"Connected to {BRIDGE_URL}")
    print(f"Conversation: {CONVERSATION_ID}  (context is kept across turns)")
    print("Type an instruction for the Copilot agent. 'exit' or Ctrl+C to quit.\n")

    loop = asyncio.get_event_loop()
    async with ClientSession() as session:
        while True:
            try:
                print("you> ", end="")  # printed on the same (binary) channel as replies
                prompt = await loop.run_in_executor(None, input)
            except (EOFError, KeyboardInterrupt):
                break
            prompt = prompt.strip()
            if not prompt:
                continue
            if prompt.lower() in ("exit", "quit", "/exit", "/quit"):
                break
            await _send_and_wait(session, prompt)

    await runner.cleanup()
    print("bye")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
