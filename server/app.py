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
# which build is running. Keep in step with the installer (-Version) at release.
__version__ = "1.4.1"

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
    return json_response(
        {
            "status": "ok",
            "version": __version__,
            "copilot": RUNNER.exe,
            "provider": getattr(RUNNER, "name", "copilot"),
            "providerAvailable": getattr(RUNNER, "available", True),
            "scope": CONFIG.COPILOT_SCOPE,
            "workdir": RUNNER.workdir,
            "authMode": "production" if CONFIG.APP_ID else "local-no-auth",
            "allowlistEntries": len(CONFIG.ALLOWED_USER_IDS),
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
            "CHAT_API_TOKEN is empty -> the mobile/web chat API (/api/chat) has NO auth. "
            "Anyone with the tunnel URL can run Copilot. Set CHAT_API_TOKEN in .env."
        )
    web.run_app(APP, host=CONFIG.HOST, port=CONFIG.PORT)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001
        print(error, file=sys.stderr)
        raise
