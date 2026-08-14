"""Contract checks for machine-aware unified History.

Run: python tests/history_api_test.py (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import webchat


class StubConfig:
    CHAT_API_TOKEN = ""
    MAX_ATTACHMENT_MB = 25
    MAX_ATTACHMENTS = 8
    SESSION_WATCHER_ENABLED = False
    ONEDRIVE_SYNC_ENABLED = False

    def __init__(self, sessions_dir: str):
        self.SESSIONS_DIR = sessions_dir


class StubRunner:
    name = "copilot"

    def __init__(self, workdir: str):
        self.workdir = workdir


async def main() -> None:
    original_name = os.environ.get("COPILOTBRIDGE_DEVICE_NAME")
    original_list = webchat.copilot_sessions.list_sessions
    os.environ["COPILOTBRIDGE_DEVICE_NAME"] = "History Laptop"
    webchat.copilot_sessions.list_sessions = lambda: [{
        "id": "native-1",
        "nativeId": "native-1",
        "sourceKey": "cli:native-1",
        "source": "cli",
        "title": "Native history",
        "updatedAt": 20,
        "messageCount": 2,
        "promptCount": 1,
    }]
    try:
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
                created = await (await client.post(
                    "/api/sessions", json={"title": "Synced history"}
                )).json()
                store = app["session_store"]
                for index in range(3):
                    store.append_message(created["id"], "user", f"question {index}")
                    store.append_message(created["id"], "assistant", f"answer {index}")
                response = await client.get("/api/history")
                assert response.status == 200
                history = await response.json()
                assert history["machines"] == [{
                    "id": app["session_store"].device_id,
                    "name": "History Laptop",
                    "isLocal": True,
                }]
                assert len(history["items"]) == 2
                synced = next(
                    item for item in history["items"]
                    if item.get("conversationId") == created["id"]
                )
                native = next(item for item in history["items"] if item.get("importable"))
                assert synced["machineName"] == "History Laptop"
                assert synced["importable"] is False
                assert synced["promptCount"] == 3
                assert native["sourceKey"] == "cli:native-1"
                assert native["machineName"] == "History Laptop"
                assert native["promptCount"] == 1
            finally:
                await client.close()

        webapp = Path(_SERVER_DIR) / "webapp"
        html = (webapp / "index.html").read_text(encoding="utf-8")
        service_worker = (webapp / "sw.js").read_text(encoding="utf-8")
        assert '>History</button>' in html
        assert "'/api/history'" in html
        assert "machine-filter" in html
        assert "machine + ' - ' + title" in html
        assert "cb_hide_short_sessions" in html
        assert "cb_short_session_prompts" in html
        assert 'class="threshold" type="number"' in html
        assert "sessionPromptCount(session) <= shortSessionPrompts" in html
        assert "SHORT_SESSION_PROMPTS" not in html
        assert "promptCount" in html
        assert "copilot-bridge-v22" in service_worker
    finally:
        webchat.copilot_sessions.list_sessions = original_list
        if original_name is None:
            os.environ.pop("COPILOTBRIDGE_DEVICE_NAME", None)
        else:
            os.environ["COPILOTBRIDGE_DEVICE_NAME"] = original_name

    print("ALL HISTORY API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())