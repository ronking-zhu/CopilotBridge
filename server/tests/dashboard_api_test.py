"""In-process API tests for the unified AI dashboard and inbox.

Run:  python tests/dashboard_api_test.py   (from the server/ directory)
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


class StubResult:
    ok = True
    text = "completed dashboard answer"
    exit_code = 0
    timed_out = False
    session_id = None


class StubRunner:
    name = "copilot"

    def __init__(self, workdir: str):
        self.workdir = workdir

    async def run(self, prompt, conversation_id=None, new_session=False,
                  session_id=None, attachments=None, history=None):
        await asyncio.sleep(0.02)
        return StubResult()


class StubConfig:
    CHAT_API_TOKEN = ""
    MAX_ATTACHMENT_MB = 25
    MAX_ATTACHMENTS = 8
    SESSION_WATCHER_ENABLED = False
    SESSION_WATCHER_INTERVAL = 5.0
    SESSION_WATCHER_SETTLE_SCANS = 1
    ONEDRIVE_SYNC_ENABLED = False

    def __init__(self, sessions_dir: str):
        self.SESSIONS_DIR = sessions_dir


async def poll(client: TestClient, job_id: str) -> dict:
    for _ in range(50):
        response = await client.get(f"/api/chat/{job_id}")
        result = await response.json()
        if result.get("status") == "done":
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("job did not complete")


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
            created = await (await client.post(
                "/api/sessions", json={"title": "Dashboard conversation"}
            )).json()
            start = await (await client.post("/api/chat", json={
                "message": "run a task", "conversationId": created["id"],
            })).json()
            done = await poll(client, start["jobId"])
            assert done["ok"] is True
            assert done.get("inboxItemId")

            dashboard_response = await client.get("/api/dashboard")
            assert dashboard_response.status == 200
            dashboard = await dashboard_response.json()
            assert dashboard["unreadCount"] == 1
            assert dashboard["runningJobCount"] == 0
            assert dashboard["watcher"]["enabled"] is False
            assert len(dashboard["inbox"]) == 1
            assert dashboard["sync"]["enabled"] is False
            item = dashboard["inbox"][0]
            assert item["source"] == "bridge"
            assert item["conversationId"] == created["id"]
            assert item["summary"] == "completed dashboard answer"

            response = await client.patch(
                f"/api/inbox/{item['id']}", json={"status": "seen"}
            )
            assert response.status == 200
            assert (await response.json())["status"] == "seen"

            store = app["session_store"]
            store.create_inbox_item(
                dedupe_key="native-test", source="vscode-insiders",
                source_key="vscode-insiders:native", native_session_id="native",
                title="Editor task", summary="Editor completed",
            )
            response = await client.get("/api/inbox?status=unread")
            unread = await response.json()
            assert len(unread["items"]) == 1
            assert unread["unreadCount"] == 1

            response = await client.post("/api/inbox/mark-all-seen")
            assert response.status == 200
            assert (await response.json())["updated"] == 1
            assert (await (await client.get("/api/dashboard")).json())["unreadCount"] == 0

            sync_status = await client.get("/api/sync/status")
            assert sync_status.status == 200
            assert (await sync_status.json())["enabled"] is False
            sync_connect = await client.post("/api/sync/connect")
            assert sync_connect.status == 503
        finally:
            await client.close()
    print("ALL DASHBOARD API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())