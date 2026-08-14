"""Tests Bridge/native id separation and provider history consistency.

Run:  python tests/identity_provider_test.py   (from the server/ directory)
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
    text = "stub answer"
    exit_code = 0
    timed_out = False
    session_id = None


class StubRunner:
    name = "copilot"

    def __init__(self, workdir: str):
        self.workdir = workdir
        self.calls: list[dict] = []

    async def run(self, prompt, conversation_id=None, new_session=False,
                  session_id=None, attachments=None, history=None):
        self.calls.append({
            "prompt": prompt,
            "session_id": session_id,
            "history": list(history or []),
        })
        return StubResult()


class StubConfig:
    CHAT_API_TOKEN = ""
    MAX_ATTACHMENT_MB = 25
    MAX_ATTACHMENTS = 8

    def __init__(self, sessions_dir: str):
        self.SESSIONS_DIR = sessions_dir


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = os.path.join(tmp, "workspace")
        os.makedirs(workdir)
        runner = StubRunner(workdir)
        app = web.Application()
        webchat.setup_web_routes(app, StubConfig(os.path.join(tmp, "sessions")), runner)
        client = TestClient(TestServer(app))
        await client.start_server()
        original_read = webchat.copilot_sessions.read_session
        try:
            created_response = await client.post("/api/sessions", json={"title": "New"})
            created = await created_response.json()
            bridge_id = created["id"]
            for prompt in ("first question", "second question"):
                response = await client.post("/api/chat-sync", json={
                    "message": prompt, "conversationId": bridge_id,
                })
                assert response.status == 200

            assert runner.calls[0]["session_id"] != bridge_id
            assert runner.calls[1]["session_id"] == runner.calls[0]["session_id"]
            assert runner.calls[0]["history"] == []
            assert [message["text"] for message in runner.calls[1]["history"]] == [
                "first question", "stub answer",
            ]

            def read_native(session_id: str, source: str = ""):
                assert session_id == "native-cli-session"
                assert source in ("", "cli")
                return {
                    "id": session_id,
                    "title": "Imported CLI",
                    "source": "cli",
                    "createdAt": 10.0,
                    "updatedAt": 12.0,
                    "messages": [
                        {"role": "user", "text": "native question", "ts": 10.0},
                        {"role": "assistant", "text": "native answer", "ts": 12.0},
                    ],
                }

            webchat.copilot_sessions.read_session = read_native
            imported_response = await client.post(
                "/api/copilot-sessions/cli:native-cli-session/import"
            )
            assert imported_response.status == 201
            imported = await imported_response.json()
            assert imported["id"] != "native-cli-session"

            response = await client.post("/api/chat-sync", json={
                "message": "continue imported", "conversationId": imported["id"],
            })
            assert response.status == 200
            assert runner.calls[-1]["session_id"] == "native-cli-session"

            store = app["session_store"]
            linked = store.find_by_external_ref("copilot-cli", "native-cli-session")
            assert linked and linked["id"] == imported["id"]

            def read_vscode(session_id: str, source: str = ""):
                assert session_id == "native-vscode-session"
                assert source in ("", "vscode")
                return {
                    "id": session_id,
                    "title": "Imported VS Code",
                    "source": "vscode",
                    "createdAt": 20.0,
                    "updatedAt": 22.0,
                    "messages": [
                        {"role": "user", "text": "editor question", "ts": 20.0},
                        {"role": "assistant", "text": "editor answer", "ts": 22.0},
                    ],
                }

            webchat.copilot_sessions.read_session = read_vscode
            response = await client.post(
                "/api/copilot-sessions/vscode:native-vscode-session/import"
            )
            vscode_import = await response.json()
            assert response.status == 201
            assert vscode_import["id"] != "native-vscode-session"
            response = await client.post("/api/chat-sync", json={
                "message": "continue editor history",
                "conversationId": vscode_import["id"],
            })
            assert response.status == 200
            assert runner.calls[-1]["session_id"] != "native-vscode-session"
            assert [message["text"] for message in runner.calls[-1]["history"]] == [
                "editor question", "editor answer",
            ]
        finally:
            webchat.copilot_sessions.read_session = original_read
            await client.close()
    print("ALL IDENTITY AND PROVIDER HISTORY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())