"""HTTP contract checks for versioned knowledge and message evidence.

Run: python tests/knowledge_api_test.py (from the server/ directory)
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
            session = await (await client.post(
                "/api/sessions", json={"title": "Knowledge source"}
            )).json()
            store = app["session_store"]
            store.append_message(session["id"], "user", "Choose immutable packages")
            store.append_message(session["id"], "assistant", "They avoid overwrites")
            messages = store.get(session["id"])["messages"]

            invalid = await client.post("/api/knowledge", json={
                "type": "decision", "title": "Missing evidence",
                "bodyMarkdown": "This must fail.", "evidenceMessageIds": [],
            })
            assert invalid.status == 400

            response = await client.post("/api/knowledge", json={
                "type": "decision", "title": "Use immutable packages",
                "bodyMarkdown": "Each device writes immutable event packages.",
                "evidenceMessageIds": [message["id"] for message in messages],
                "project": "Copilot Bridge", "labels": ["Sync"],
            })
            assert response.status == 201
            created = await response.json()
            assert created["versionNumber"] == 1
            assert len(created["evidence"]) == 2
            assert all("&message=" in value["deepLink"] for value in created["evidence"])

            listed = await (await client.get(
                "/api/knowledge?query=immutable&project=Copilot%20Bridge"
            )).json()
            assert listed["count"] == 1
            by_session = await (await client.get(
                f"/api/sessions/{session['id']}/knowledge"
            )).json()
            assert by_session["items"][0]["id"] == created["id"]

            response = await client.post(
                f"/api/knowledge/{created['id']}/versions", json={
                    "bodyMarkdown": created["bodyMarkdown"] + "\n\nNever overwrite peers.",
                    "evidenceMessageIds": [messages[1]["id"]],
                    "confidence": "high",
                },
            )
            assert response.status == 201
            versioned = await response.json()
            assert versioned["versionNumber"] == 2
            assert len(versioned["versions"]) == 2

            response = await client.patch(
                f"/api/knowledge/{created['id']}", json={"status": "verified"}
            )
            assert response.status == 200
            assert (await response.json())["status"] == "verified"
            assert (await (await client.get(
                f"/api/knowledge/{created['id']}"
            )).json())["evidence"][0]["messageId"] == messages[1]["id"]

            response = await client.delete(f"/api/knowledge/{created['id']}")
            assert response.status == 200
            assert (await client.get(f"/api/knowledge/{created['id']}")).status == 404
        finally:
            await client.close()

    webapp = Path(_SERVER_DIR) / "webapp"
    product_js = (webapp / "v23.js").read_text(encoding="utf-8")
    product_css = (webapp / "v23.css").read_text(encoding="utf-8")
    assert "v23CreateNav('knowledge', v23T('知识库', 'Knowledge'), 'knowledge')" in product_js
    assert "id = 'productKnowledge'" in product_js
    assert "v24NewKnowledge" in product_js
    assert "evidence.messageId" in product_js
    assert "v24OpenEvidence" in product_js
    assert "knowledgeListRequest" in product_js
    assert "request !== v23State.knowledgeListRequest" in product_js
    assert "pending = []" in product_js
    assert "renderPending()" in product_js
    assert "updateSendEnabled()" in product_js
    assert ".v24-knowledge-workspace" in product_css
    assert "#productKnowledge.detail-open" in product_css
    app_shell = (webapp / "index.html").read_text(encoding="utf-8")
    assert "replace(/[&<>\"']/g" in app_shell
    assert "'\"':'&quot;'" in app_shell
    assert '"\'":\'&#39;\'' in app_shell
    print("ALL KNOWLEDGE API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())