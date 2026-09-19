"""In-process API tests for the unified AI dashboard and inbox.

Run:  python tests/dashboard_api_test.py   (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile
import time

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
            shell_response = await client.get("/?dashboard=1")
            assert shell_response.status == 200
            shell = await shell_response.text()
            assert 'id="dashboardLink"' in shell
            assert 'href="/?dashboard=1"' in shell
            assert "http://localhost" not in shell.split('id="dashboardLink"', 1)[1].split("</a>", 1)[0]

            created = await (await client.post(
                "/api/sessions", json={"title": "Dashboard conversation"}
            )).json()
            organized_response = await client.patch(
                f"/api/sessions/{created['id']}", json={
                    "isFavorite": True,
                    "isPinned": True,
                    "project": "Bridge",
                    "labels": ["Release", "Windows"],
                },
            )
            assert organized_response.status == 200
            organized = await organized_response.json()
            assert organized["isFavorite"] is True
            assert organized["isPinned"] is True
            assert organized["project"] == "Bridge"
            assert organized["labels"] == ["Release", "Windows"]
            invalid_combination = await client.patch(
                f"/api/sessions/{created['id']}",
                json={"title": "Partially changed", "labels": "invalid"},
            )
            assert invalid_combination.status == 400
            unchanged = await (await client.get(
                f"/api/sessions/{created['id']}"
            )).json()
            assert unchanged["title"] == "Dashboard conversation"
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

            reminder_response = await client.post(
                f"/api/inbox/{item['id']}/remind", json={"minutes": 30}
            )
            assert reminder_response.status == 200
            reminder = await reminder_response.json()
            assert reminder["status"] == "seen"
            assert reminder["remindAt"] > time.time()

            store = app["session_store"]
            with store._lock, store._conn:
                store._conn.execute(
                    "UPDATE inbox_items SET remind_at=? WHERE id=?",
                    (time.time() - 1, item["id"]),
                )
            due = await (await client.get("/api/inbox?status=unread")).json()
            assert any(candidate["id"] == item["id"] for candidate in due["items"])
            assert next(
                candidate for candidate in due["items"] if candidate["id"] == item["id"]
            )["remindAt"] is None

            response = await client.patch(
                f"/api/inbox/{item['id']}", json={"status": "seen"}
            )
            assert response.status == 200
            assert (await response.json())["status"] == "seen"

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

            store.create_inbox_item(
                dedupe_key="complete-all-test", source="bridge",
                source_key="bridge:complete-all", title="Bulk complete",
                summary="Complete this item",
            )
            response = await client.post("/api/inbox/complete-all")
            assert response.status == 200
            assert (await response.json())["updated"] >= 1
            attention = await (await client.get(
                "/api/inbox?status=unread,seen"
            )).json()
            assert attention["items"] == []

            sync_status = await client.get("/api/sync/status")
            assert sync_status.status == 200
            assert (await sync_status.json())["enabled"] is False
            sync_connect = await client.post("/api/sync/connect")
            assert sync_connect.status == 503
            invalid = await client.patch(
                f"/api/sessions/{created['id']}", json={"labels": "not-an-array"}
            )
            assert invalid.status == 400
        finally:
            await client.close()
    print("ALL DASHBOARD API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())