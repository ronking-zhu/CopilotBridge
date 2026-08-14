"""In-process tests for client image attachments on the chat API.

Spins up the real aiohttp web routes (webchat.setup_web_routes) with a STUB
Copilot runner, so the test never invokes the actual CLI. It verifies that:

  * POST /api/chat with an image saves the file under <workdir>/.uploads/<sid>/,
    passes its path to runner.run(attachments=...), and records the attachment
    metadata on the stored user message.
  * GET /api/uploads/<sid>/<file> serves the bytes (with header OR ?key= auth)
    and rejects a missing/wrong token.
  * /api/chat-sync behaves the same.
  * An image-only turn (no text) is accepted; an empty turn is rejected.
  * Oversized / unsupported images are rejected with 400.

Run:  python tests/image_upload_test.py   (from the server/ directory)
"""

import asyncio
import base64
import os
import sys
import tempfile

# Import the server modules (webchat imports copilot_sessions/session_store as
# top-level modules, so the server dir must be on sys.path).
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webchat

# 1x1 transparent PNG.
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9Q"
           "DwADhgGAWjR9awAAAABJRU5ErkJggg==")
TOKEN = "test-token-123"


class StubResult:
    def __init__(self, text):
        self.text = text
        self.ok = True
        self.exit_code = 0
        self.timed_out = False
        self.session_id = None


class StubRunner:
    """Stands in for CopilotRunner: records the args of each run() call."""

    def __init__(self, workdir):
        self.workdir = workdir
        self.calls = []

    async def run(self, prompt, conversation_id=None, new_session=False,
                  session_id=None, attachments=None, history=None):
        self.calls.append({
            "prompt": prompt, "session_id": session_id,
            "attachments": list(attachments or []),
        })
        return StubResult(f"stub-reply for: {prompt!r} with {len(attachments or [])} image(s)")


class StubConfig:
    AUTH_MODE = "apikey"

    def __init__(self, sessions_dir, max_mb=25, max_files=8):
        self.CHAT_API_TOKEN = TOKEN
        self.SESSIONS_DIR = sessions_dir
        self.MAX_ATTACHMENT_MB = max_mb
        self.MAX_ATTACHMENTS = max_files


def _hdr():
    return {"X-API-Key": TOKEN}


async def _make_client(tmp, max_mb=25, max_files=8):
    workdir = os.path.join(tmp, "workspace")
    os.makedirs(workdir, exist_ok=True)
    sessions_dir = os.path.join(tmp, "sessions")
    runner = StubRunner(workdir)
    config = StubConfig(sessions_dir, max_mb, max_files)
    app = web.Application()
    webchat.setup_web_routes(app, config, runner)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, runner, workdir


async def _poll(client, job_id, tries=50):
    for _ in range(tries):
        r = await client.get(f"/api/chat/{job_id}", headers=_hdr())
        j = await r.json()
        if j.get("status") == "done":
            return j
        await asyncio.sleep(0.05)
    raise AssertionError("job never finished")


async def test_chat_with_image(tmp):
    client, runner, workdir = await _make_client(tmp)
    try:
        body = {
            "message": "what is this?",
            "conversationId": "webapp",
            "images": [{"name": "pic.png", "mime": "image/png", "data": PNG_B64}],
        }
        r = await client.post("/api/chat", json=body, headers=_hdr())
        assert r.status == 200, f"start status {r.status}"
        start = await r.json()
        sid = start["sessionId"]
        done = await _poll(client, start["jobId"])
        assert done["ok"] is True, done

        # runner received exactly one attachment path, under the session uploads dir.
        assert len(runner.calls) == 1, runner.calls
        paths = runner.calls[0]["attachments"]
        assert len(paths) == 1, paths
        up_dir = os.path.join(workdir, ".uploads", sid)
        assert os.path.isfile(paths[0]), paths[0]
        assert os.path.abspath(paths[0]).startswith(os.path.abspath(up_dir)), paths[0]
        # The prompt forwarded to Copilot is the user's text.
        assert runner.calls[0]["prompt"] == "what is this?"

        # The stored user message carries attachment metadata with a serve URL.
        r = await client.get(f"/api/sessions/{sid}", headers=_hdr())
        sess = await r.json()
        user_msg = sess["messages"][0]
        atts = user_msg.get("attachments")
        assert atts and atts[0]["mime"] == "image/png" and atts[0]["name"] == "pic.png", atts
        url = atts[0]["url"]
        assert url.startswith(f"/api/uploads/{sid}/"), url

        # The image is served back, via header auth and via ?key= query.
        raw = base64.b64decode(PNG_B64)
        r = await client.get(url, headers=_hdr())
        assert r.status == 200, r.status
        assert await r.read() == raw, "served bytes mismatch (header auth)"
        r = await client.get(url + f"?key={TOKEN}")
        assert r.status == 200 and await r.read() == raw, "served bytes mismatch (query auth)"
        # No / wrong token -> 401.
        r = await client.get(url)
        assert r.status == 401, f"expected 401, got {r.status}"
        print("PASS test_chat_with_image")
    finally:
        await client.close()


async def test_chat_sync_image_only(tmp):
    client, runner, workdir = await _make_client(tmp)
    try:
        body = {
            "conversationId": "sync-conv",
            "images": [{"mime": "image/jpeg", "data": PNG_B64}],  # bytes are png; mime drives ext
        }
        r = await client.post("/api/chat-sync", json=body, headers=_hdr())
        assert r.status == 200, f"sync status {r.status}: {await r.text()}"
        out = await r.json()
        sid = out["sessionId"]
        # Image-only turn => default prompt sent to Copilot.
        assert runner.calls[0]["prompt"] == webchat._DEFAULT_IMAGE_PROMPT, runner.calls
        assert len(runner.calls[0]["attachments"]) == 1
        # Saved with the .jpg extension implied by the mime.
        assert runner.calls[0]["attachments"][0].endswith(".jpg")
        r = await client.get(f"/api/sessions/{sid}", headers=_hdr())
        sess = await r.json()
        assert sess["messages"][0]["attachments"][0]["mime"] == "image/jpeg"
        print("PASS test_chat_sync_image_only")
    finally:
        await client.close()


async def test_rejections(tmp):
    # Empty turn (no text, no images) -> 400.
    client, runner, _ = await _make_client(tmp)
    try:
        r = await client.post("/api/chat", json={"conversationId": "x"}, headers=_hdr())
        assert r.status == 400, f"empty turn expected 400, got {r.status}"

        # Unsupported mime -> 400.
        r = await client.post("/api/chat", json={
            "message": "hi", "conversationId": "x",
            "images": [{"mime": "application/x-msdownload", "data": PNG_B64}],
        }, headers=_hdr())
        assert r.status == 400, f"bad mime expected 400, got {r.status}"

        # Too many images -> 400 (default max 8).
        many = [{"mime": "image/png", "data": PNG_B64} for _ in range(9)]
        r = await client.post("/api/chat", json={
            "message": "hi", "conversationId": "x", "images": many,
        }, headers=_hdr())
        assert r.status == 400, f"too many expected 400, got {r.status}"

        # Not base64 -> 400.
        r = await client.post("/api/chat", json={
            "message": "hi", "conversationId": "x",
            "images": [{"mime": "image/png", "data": "@@@not-base64@@@"}],
        }, headers=_hdr())
        assert r.status == 400, f"bad base64 expected 400, got {r.status}"
        print("PASS test_rejections")
    finally:
        await client.close()


async def test_size_cap(tmp):
    # 1 MB cap; build a >1MB base64 png-ish blob (data validates as base64, mime png).
    client, runner, _ = await _make_client(tmp, max_mb=1)
    try:
        big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024 + 10)).decode()
        r = await client.post("/api/chat", json={
            "message": "hi", "conversationId": "x",
            "images": [{"mime": "image/png", "data": big}],
        }, headers=_hdr())
        assert r.status == 400, f"oversized expected 400, got {r.status}: {await r.text()}"
        print("PASS test_size_cap")
    finally:
        await client.close()


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        await test_chat_with_image(os.path.join(tmp, "a"))
        await test_chat_sync_image_only(os.path.join(tmp, "b"))
        await test_rejections(os.path.join(tmp, "c"))
        await test_size_cap(os.path.join(tmp, "d"))
    print("\nALL SERVER IMAGE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
