"""Regression checks for local access plus private Dev Tunnel authentication.

Run: python tests/tunnel_auth_mode_test.py (from the server/ directory)
"""

import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from auth import Authenticator
from gui import _identity_summary
from provisioning import write_connection_card
import webchat


TOKEN = "control-only-secret"


class StubConfig:
    AUTH_MODE = "tunnel"
    HOST = "localhost"
    TUNNEL_AUTH = "private"
    CHAT_API_TOKEN = TOKEN
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


class StubRequest:
    def __init__(self, api_key: str = ""):
        self.headers = {"X-API-Key": api_key} if api_key else {}
        self.query = {}

    def __setitem__(self, key, value):
        setattr(self, key, value)


def auth_config(**overrides):
    values = {
        "AUTH_MODE": "tunnel",
        "HOST": "localhost",
        "TUNNEL_AUTH": "private",
        "CHAT_API_TOKEN": TOKEN,
        "ENTRA_CLIENT_ID": "",
        "ENTRA_TENANT_ID": "",
        "ENTRA_AUDIENCE": [],
        "ENTRA_ALLOWED_USERS": [],
        "ENTRA_ALLOWED_GROUPS": [],
        "ENTRA_ALLOWED_ROLES": [],
        "ENTRA_SCOPES": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def main() -> None:
    authenticator = Authenticator(auth_config())
    assert authenticator.describe() == {
        "authMode": "tunnel",
        "authRequired": False,
    }
    assert authenticator.check(StubRequest())
    assert not authenticator.check_control(StubRequest())
    assert authenticator.check_control(StubRequest(TOKEN))

    anonymous = Authenticator(auth_config(TUNNEL_AUTH="anonymous"))
    assert anonymous.mode == "apikey"
    assert not anonymous.check(StubRequest())
    assert anonymous.check(StubRequest(TOKEN))

    non_loopback = Authenticator(auth_config(HOST="0.0.0.0"))
    assert non_loopback.mode == "apikey"
    assert not non_loopback.check(StubRequest())

    assert _identity_summary(auth_config()) == (
        "Mode: localhost trusted \u00b7 remote Microsoft Dev Tunnel"
    )

    with tempfile.TemporaryDirectory() as tmp:
        card_path = os.path.join(tmp, "connection.json")
        card_text = write_connection_card(
            "https://example.devtunnels.ms", "http://localhost:3978", TOKEN,
            path=card_path, auth_mode="tunnel", tunnel_auth="private",
        )
        with open(card_path, encoding="utf-8") as file:
            card = json.load(file)
        assert TOKEN not in card_text
        assert "localhost trusted; remote Microsoft owner sign-in" in card_text
        assert card["apiKey"] == TOKEN
        assert card["authMode"] == "tunnel"

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
            config_response = await client.get("/api/webconfig")
            assert config_response.status == 200
            assert await config_response.json() == {
                "authMode": "tunnel",
                "authRequired": False,
            }

            create_response = await client.post(
                "/api/sessions", json={"title": "No token required"}
            )
            assert create_response.status == 201

            index_response = await client.get("/")
            html = await index_response.text()
            assert TOKEN not in html
            assert "<script>window.__CB_KEY=" not in html
        finally:
            await client.close()

    print("ALL TUNNEL AUTH MODE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())