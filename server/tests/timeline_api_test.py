"""In-process API contract tests for settings and the prompt timeline.

Run:  python tests/timeline_api_test.py   (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

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
            defaults = await response.json()
            assert defaults["promptPreviewLength"] == 200
            assert defaults["uiLanguage"] == "zh"
            response = await client.patch(
                "/api/settings", json={
                    "promptPreviewLength": 300, "uiLanguage": "en",
                }, headers=headers()
            )
            assert response.status == 200
            updated = await response.json()
            assert updated["promptPreviewLength"] == 300
            assert updated["uiLanguage"] == "en"

            response = await client.patch(
                "/api/settings", json={"promptPreviewLength": 5}, headers=headers()
            )
            assert response.status == 400
            response = await client.patch(
                "/api/settings", json={"uiLanguage": "fr"}, headers=headers()
            )
            assert response.status == 400
            response = await client.get("/api/settings")
            assert response.status == 401
        finally:
            await client.close()

    webapp = Path(_SERVER_DIR) / "webapp"
    html = (webapp / "index.html").read_text(encoding="utf-8")
    product_js = (webapp / "v23.js").read_text(encoding="utf-8")
    product_css = (webapp / "v23.css").read_text(encoding="utf-8")
    service_worker = (webapp / "sw.js").read_text(encoding="utf-8")
    assert 'id="timelineBtn"' in html
    assert "$('#timelineBtn').onclick = openTimeline;" in html
    assert "initialParams.get('message')" in html
    assert "wrap.dataset.messageId = messageId" in html
    assert "findMessageElement(messageToReveal)" in html
    assert "url.searchParams.set('message', messageId)" in html
    assert "url.searchParams.delete('knowledge')" in html
    assert "timeline.inert = false" in html
    assert "timeline.inert = true" in html
    assert 'id="uiLanguageInput"' in html
    assert "window.cbUiText = uiText" in html
    assert "localStorage.setItem('cb_ui_language'" in html
    assert "addMsg(kind, m.text || ''" in html
    assert "const v23T =" in product_js
    assert "Interface language" in product_js
    assert "Translates product UI, not conversation content" in product_js
    assert "#v23GlobalSearch, #v23SyncIndicator { display: none; }" in product_css
    assert "#v23GlobalSearch, #v23SyncIndicator, #timelineBtn" not in product_css
    assert "copilot-bridge-v267-dashboard-controls" in service_worker
    print("ALL PROMPT TIMELINE API TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())