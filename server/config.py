"""Configuration for the Copilot CLI <-> Teams bridge.

Values are read from environment variables (loaded from a local .env file).
The APP_ID / APP_PASSWORD / APP_TYPE / APP_TENANTID attribute names are the ones
that botbuilder's ConfigurationBotFrameworkAuthentication expects, so do not rename them.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from paths import app_base_dir

# Load .env that sits next to the writable base dir (the server/ folder from
# source, or next to the .exe when frozen), regardless of the working directory.
load_dotenv(app_base_dir() / ".env")


def _as_bool(value, default=False):
    if value is None or value == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_list(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class DefaultConfig:
    """Bridge configuration."""

    # --- Local server ---
    PORT = int(os.environ.get("PORT", "3978"))
    HOST = os.environ.get("HOST", "localhost")

    # --- Azure Bot / Bot Framework (names required by botbuilder) ---
    APP_ID = os.environ.get("MicrosoftAppId", "")
    APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")
    APP_TYPE = os.environ.get("MicrosoftAppType", "MultiTenant")
    APP_TENANTID = os.environ.get("MicrosoftAppTenantId", "")

    # --- GitHub Copilot CLI ---
    # Empty COPILOT_PATH => auto-discover the installed copilot executable.
    COPILOT_PATH = os.environ.get("COPILOT_PATH", "")
    # 'workdir' restricts file access to COPILOT_WORKDIR (safer).
    # 'full' grants whole-machine access (--allow-all).
    COPILOT_SCOPE = os.environ.get("COPILOT_SCOPE", "workdir")
    COPILOT_WORKDIR = os.environ.get("COPILOT_WORKDIR", "")
    COPILOT_MODEL = os.environ.get("COPILOT_MODEL", "")
    # Hard timeout (seconds) for a single Copilot run. 0 (or any non-positive
    # value) means NO timeout - AI tasks can legitimately run for hours or longer,
    # so we let Copilot work as long as it needs and rely on the client showing a
    # progress indicator. Set a positive number only if you want a hard cap.
    COPILOT_TIMEOUT = int(os.environ.get("COPILOT_TIMEOUT", "0"))
    COPILOT_SESSION_CONTINUITY = _as_bool(
        os.environ.get("COPILOT_SESSION_CONTINUITY"), True
    )

    # --- AI provider selection (pluggable backends) ---
    # Which backend turns a prompt into a reply: 'copilot' (default), 'claude',
    # or 'openai'. See server/providers/. Unknown values fall back to 'copilot'.
    AI_PROVIDER = os.environ.get("AI_PROVIDER", "copilot")
    # Claude CLI: empty CLAUDE_PATH => auto-discover 'claude' on PATH.
    CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "")
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")
    # OpenAI (or any OpenAI-compatible endpoint). Only active when a key is set.
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "")

    # --- Persistent chat sessions ---
    # Directory holding one JSON file per conversation (the durable, server-owned
    # session store). EMPTY => default to <server dir>/sessions, resolved in webchat.
    SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "")

    # --- Security / UX ---
    # Comma-separated AAD object ids allowed to use the bot.
    # EMPTY => everyone who can reach the bot may run Copilot CLI (NOT recommended).
    ALLOWED_USER_IDS = _as_list(os.environ.get("ALLOWED_USER_IDS", ""))
    MAX_REPLY_CHARS = int(os.environ.get("MAX_REPLY_CHARS", "16000"))
    CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "3500"))

    # --- Image / file attachments (mobile & web clients) ---
    # Clients may send image attachments with a chat turn; the server saves them
    # under <workdir>/.uploads/<sessionId>/ and passes each to the Copilot CLI via
    # its native ``--attachment <path>`` flag. These cap abuse over the public tunnel.
    MAX_ATTACHMENT_MB = int(os.environ.get("MAX_ATTACHMENT_MB", "25"))
    MAX_ATTACHMENTS = int(os.environ.get("MAX_ATTACHMENTS", "8"))

    # Token guarding the direct mobile/web chat API (/api/chat) exposed via the tunnel.
    # EMPTY => no auth (anyone with the tunnel URL can run Copilot - unsafe). Set a strong value.
    CHAT_API_TOKEN = os.environ.get("CHAT_API_TOKEN", "")

    # --- Dev Tunnel (public ingress, used by the orchestrated launcher.py) ---
    # When enabled, launcher.py starts a Microsoft Dev Tunnel once the server is
    # healthy, exposing a public HTTPS URL that forwards to the local port.
    TUNNEL_ENABLED = _as_bool(os.environ.get("TUNNEL_ENABLED"), True)
    TUNNEL_ID = os.environ.get("TUNNEL_ID", "copilot-bridge")
    TUNNEL_ANONYMOUS = _as_bool(os.environ.get("TUNNEL_ANONYMOUS"), True)
    # Empty DEVTUNNEL_PATH => auto-discover (bundled tools/ -> PATH -> winget link).
    DEVTUNNEL_PATH = os.environ.get("DEVTUNNEL_PATH", "")
