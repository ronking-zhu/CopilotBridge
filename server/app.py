"""aiohttp entry point for the Copilot CLI <-> Teams bridge.

Routes:
  POST /api/messages  -> Bot Framework messaging endpoint (point DevTunnel/Azure Bot here)
  GET  /health        -> simple health/diagnostics JSON
"""

import logging
import sys
import traceback
from datetime import datetime, timezone
from http import HTTPStatus

# Windows consoles default to cp1252; force UTF-8 so Unicode in logs never crashes the process.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from aiohttp import web
from aiohttp.web import Request, Response, json_response
from botbuilder.core import TurnContext
from botbuilder.core.integration import aiohttp_error_middleware
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)

from bot import CopilotBridgeBot
from config import DefaultConfig
from providers import available_providers, build_provider
from webchat import setup_web_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("copilot_bridge")

# Product version. Surfaced in /health so clients and the upgrade flow can see
# which build is running. Defined in version.py (a tiny, dependency-free module)
# so the desktop Control Panel can read it without importing this whole module.
# Keep version.py in step with the installer (-Version) at release.
from version import __version__

CONFIG = DefaultConfig()

ADAPTER = CloudAdapter(ConfigurationBotFrameworkAuthentication(CONFIG))


async def on_error(context: TurnContext, error: Exception):
    logger.error("[on_turn_error] %s", error)
    traceback.print_exc()
    try:
        await context.send_activity("The bridge hit an error. Check the host server logs.")
    except Exception:  # noqa: BLE001
        pass


ADAPTER.on_turn_error = on_error

# The active AI backend (Copilot CLI by default; selectable via AI_PROVIDER).
RUNNER = build_provider(CONFIG)
BOT = CopilotBridgeBot(ADAPTER, CONFIG, RUNNER)


async def messages(req: Request) -> Response:
    return await ADAPTER.process(req, BOT)


async def health(req: Request) -> Response:  # noqa: ARG001
    # Report the REAL request-auth posture. The new identity layer (AUTH_MODE)
    # supersedes the legacy Bot-Framework/Teams indicators for the web+mobile
    # clients, so surface it here; fall back to the old signals otherwise.
    identity_mode = (getattr(CONFIG, "AUTH_MODE", "apikey") or "apikey").strip().lower()
    if identity_mode in ("tunnel", "entra", "both"):
        auth_mode = identity_mode
        allowlist = (len(getattr(CONFIG, "ENTRA_ALLOWED_USERS", []))
                     + len(getattr(CONFIG, "ENTRA_ALLOWED_GROUPS", []))
                     + len(getattr(CONFIG, "ENTRA_ALLOWED_ROLES", [])))
    elif CONFIG.APP_ID:
        auth_mode = "production"
        allowlist = len(CONFIG.ALLOWED_USER_IDS)
    elif getattr(CONFIG, "CHAT_API_TOKEN", ""):
        auth_mode = "apikey"
        allowlist = len(CONFIG.ALLOWED_USER_IDS)
    else:
        auth_mode = "local-no-auth"
        allowlist = len(CONFIG.ALLOWED_USER_IDS)
    return json_response(
        {
            "status": "ok",
            "version": __version__,
            "copilot": RUNNER.exe,
            "provider": getattr(RUNNER, "name", "copilot"),
            "providerAvailable": getattr(RUNNER, "available", True),
            "scope": CONFIG.COPILOT_SCOPE,
            "workdir": RUNNER.workdir,
            "authMode": auth_mode,
            "allowlistEntries": allowlist,
            "time": datetime.now(timezone.utc).isoformat(),
        }
    )


async def providers(req: Request) -> Response:  # noqa: ARG001
    """List the AI backends known to this server and whether each is usable."""
    return json_response(
        {"active": getattr(RUNNER, "name", "copilot"), "providers": available_providers(CONFIG)}
    )


APP = web.Application(middlewares=[aiohttp_error_middleware])
APP.router.add_post("/api/messages", messages)
APP.router.add_get("/health", health)
APP.router.add_get("/api/providers", providers)

# Direct mobile/web chat API + the mobile web app (served at /).
setup_web_routes(APP, CONFIG, RUNNER)

# Localhost control plane (status / shutdown / tunnel) used by the Control Panel.
from control import setup_control_routes  # noqa: E402

setup_control_routes(APP, CONFIG)


def main():
    logger.info(
        "Copilot CLI bridge starting on http://%s:%s (copilot=%s, scope=%s)",
        CONFIG.HOST, CONFIG.PORT, RUNNER.exe, CONFIG.COPILOT_SCOPE,
    )
    if not CONFIG.APP_ID:
        logger.warning(
            "MicrosoftAppId is empty -> LOCAL/no-auth mode (inbound Bot Framework JWT is "
            "NOT validated). Set it before exposing via DevTunnel/Teams."
        )
    if not CONFIG.ALLOWED_USER_IDS:
        logger.warning(
            "ALLOWED_USER_IDS is empty -> ANY user who can reach the bot can run Copilot "
            "CLI on this machine. Add your AAD object id (send '/whoami') to lock it down."
        )
    if not CONFIG.CHAT_API_TOKEN:
        logger.warning(
            "CHAT_API_TOKEN is empty -> local control APIs have no host secret. "
            "Set CHAT_API_TOKEN in .env."
        )
    web.run_app(APP, host=CONFIG.HOST, port=CONFIG.PORT)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001
        print(error, file=sys.stderr)
        raise
