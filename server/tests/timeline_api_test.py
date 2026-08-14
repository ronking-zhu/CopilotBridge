"""In-process API contract tests for settings and the prompt timeline.

Run:  python tests/timeline_api_test.py   (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import webchat

TOKEN = "timeline-test-token"


class StubConfig:
    AUTH_MODE = "apikey"

    def __init__(self, sessions_dir: str):
        self.CHAT_API_TOKEN = TOKEN
        self.SESSIONS_DIR = sessions_dir
        self.MAX_ATTACHMENT_MB = 25
        self.MAX_ATTACHMENTS = 8


class StubRunner:
    def __init__(self, workdir: str):
        self.workdir = workdir


def headers() -> dict:
    return {"X-API-Key": TOKEN}


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = os.path.join(tmp, "workspace")
        os.makedirs(workdir)
        app = web.Application()
        webchat.setup_web_routes(
            app, StubConfig(os.path.join(tmp, "sessions")), StubRunner(workdir)
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/api/sessions", json={"title": "Timeline"}, headers=headers()
            )
            assert response.status == 201
            session = await response.json()
            store = app["session_store"]
            store.append_message(session["id"], "user", "A long prompt that becomes a preview")
            store.append_message(session["id"], "assistant", "The answer")

            response = await client.get(
                f"/api/sessions/{session['id']}/turns?previewLength=20", headers=headers()
            )
            assert response.status == 200
            turns = await response.json()
            assert len(turns) == 1
            assert turns[0]["previewText"] == "A long prompt tha..."
            assert turns[0]["messageCount"] == 2

            response = await client.get("/api/settings", headers=headers())
            assert (await response.json())["promptPreviewLength"] == 200
            response = await client.patch(
                "/api/settings", json={"promptPreviewLength": 300}, headers=headers()
            )
            assert response.status == 200
            assert (await response.json())["promptPreviewLength"] == 300

            response = await client.patch(
                "/api/settings", json={"promptPreviewLength": 5}, headers=headers()
            )
            assert response.status == 400
            response = await client.get("/api/settings")
            assert response.status == 401
        finally:
            await client.close()
    print("ALL PROMPT TIMELINE API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())