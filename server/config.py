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


# --- Built-in TEST Entra ID (zhurongqing@outlook.com) -------------------------
# Ship a working Microsoft sign-in config out of the box so people can try Entra
# sign-in without first standing up their own app registration. These are NON
# secret identifiers for a personal *test* tenant owned by zhurongqing@outlook.com;
# replace them with your own app registration for production. Any value the user
# sets (env / .env / Control Panel → Save) overrides the matching default below.
TEST_ENTRA_TENANT_ID = "53f87c5f-245b-41f9-8791-e4a42a2dc058"
TEST_ENTRA_CLIENT_ID = "4b1ffcfa-7e64-4765-84ad-1796ca93ff98"
TEST_ENTRA_AUDIENCE = f"api://{TEST_ENTRA_CLIENT_ID},{TEST_ENTRA_CLIENT_ID}"
TEST_ENTRA_SCOPES = f"api://{TEST_ENTRA_CLIENT_ID}/access_as_user"
TEST_ENTRA_ADMIN_CONTACT = "zhurongqing@outlook.com"


# --- Reserved Microsoft Entra ID (to be filled when available) ---------------
# Placeholder slots for an official Microsoft (corp) tenant + app registration.
# Empty for now; the Control Panel's "Detect" dropdown offers it as a choice that
# shows "not configured yet" until these are populated (here or via env). When you
# have the real GUIDs, set them and Microsoft sign-in is one click away.
MS_ENTRA_TENANT_ID = os.environ.get("MS_ENTRA_TENANT_ID", "")
MS_ENTRA_CLIENT_ID = os.environ.get("MS_ENTRA_CLIENT_ID", "")
MS_ENTRA_AUDIENCE = os.environ.get("MS_ENTRA_AUDIENCE", "")
MS_ENTRA_SCOPES = os.environ.get("MS_ENTRA_SCOPES", "")
MS_ENTRA_ADMIN_CONTACT = os.environ.get("MS_ENTRA_ADMIN_CONTACT", "")


def entra_presets():
    """Built-in sign-in presets offered by the Control Panel's "Detect" dropdown.

    Each is a dict with ``key``/``name``/``label`` plus the **non-secret**
    tenant/client/audience/scopes to drop into the Microsoft sign-in dialog. A
    preset whose ``tenant_id``/``client_id`` are empty is a reserved slot shown as
    "not configured yet" (e.g. Microsoft until its GUIDs are known).
    """
    return [
        {
            "key": "outlook",
            "name": "Outlook (zhurongqing@outlook.com)",
            "label": "Outlook \u2014 zhurongqing@outlook.com (test)",
            "tenant_id": TEST_ENTRA_TENANT_ID,
            "client_id": TEST_ENTRA_CLIENT_ID,
            "audience": TEST_ENTRA_AUDIENCE,
            "scopes": TEST_ENTRA_SCOPES,
            "admin_contact": TEST_ENTRA_ADMIN_CONTACT,
        },
        {
            "key": "microsoft",
            "name": "Microsoft",
            "label": "Microsoft \u2014 (not configured yet)",
            "tenant_id": MS_ENTRA_TENANT_ID,
            "client_id": MS_ENTRA_CLIENT_ID,
            "audience": MS_ENTRA_AUDIENCE,
            "scopes": MS_ENTRA_SCOPES,
            "admin_contact": MS_ENTRA_ADMIN_CONTACT,
        },
    ]


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

    # --- Native AI session watcher / unified inbox ---
    # Poll read-only CLI and VS Code stores for completed responses. Native APIs
    # are eventually consistent, so require one unchanged scan before alerting.
    SESSION_WATCHER_ENABLED = _as_bool(
        os.environ.get("SESSION_WATCHER_ENABLED"), True
    )
    SESSION_WATCHER_INTERVAL = float(
        os.environ.get("SESSION_WATCHER_INTERVAL", "5")
    )
    SESSION_WATCHER_SETTLE_SCANS = int(
        os.environ.get("SESSION_WATCHER_SETTLE_SCANS", "1")
    )

    # --- OneDrive App Folder synchronization ---
    # A released product may use its publisher-owned multi-tenant Public Client
    # registration. Without one, auto mode uses the signed-in local OneDrive
    # desktop folder instead of an invalid test-tenant application.
    ONEDRIVE_SYNC_ENABLED = _as_bool(
        os.environ.get("ONEDRIVE_SYNC_ENABLED"), True
    )
    ONEDRIVE_TRANSPORT = os.environ.get("ONEDRIVE_TRANSPORT", "auto").strip().lower()
    ONEDRIVE_CLIENT_ID = os.environ.get("ONEDRIVE_CLIENT_ID", "").strip()
    ONEDRIVE_TENANT_ID = os.environ.get("ONEDRIVE_TENANT_ID", "common")
    ONEDRIVE_LOCAL_ROOT = os.environ.get("ONEDRIVE_LOCAL_ROOT", "").strip()
    ONEDRIVE_SYNC_SPACE_ID = os.environ.get("ONEDRIVE_SYNC_SPACE_ID", "default")
    ONEDRIVE_SYNC_INTERVAL = float(
        os.environ.get("ONEDRIVE_SYNC_INTERVAL", "900")
    )

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

    # Host-only secret used by the desktop control plane. Legacy AUTH_MODE=apikey
    # also uses it for the web API.
    CHAT_API_TOKEN = os.environ.get("CHAT_API_TOKEN", "")

    # --- Identity / auth policy for the direct chat + control API ---
    # The default 'tunnel' mode trusts the loopback-only local listener and relies
    # on the private Dev Tunnel for Microsoft account authentication remotely.
    # It fails back to 'apikey' if HOST is non-loopback or the tunnel is anonymous.
    AUTH_MODE = os.environ.get("AUTH_MODE", "tunnel").strip().lower()
    # Entra ID app registration the clients sign in against + this server validates.
    # ENTRA_TENANT_ID: your tenant GUID (single-tenant, recommended) or
    #   'organizations'/'common' for multi-tenant. ENTRA_AUDIENCE: the API's
    #   Application ID URI (api://<id>) or client id the access token is issued for
    #   (comma-separated to accept more than one).
    # Default to the built-in TEST Entra ID above so Microsoft sign-in works out of
    # the box; a user's own env / .env / Control Panel values take precedence.
    ENTRA_TENANT_ID = os.environ.get("ENTRA_TENANT_ID", "") or TEST_ENTRA_TENANT_ID
    ENTRA_CLIENT_ID = os.environ.get("ENTRA_CLIENT_ID", "") or TEST_ENTRA_CLIENT_ID
    ENTRA_AUDIENCE = _as_list(os.environ.get("ENTRA_AUDIENCE", "") or TEST_ENTRA_AUDIENCE)
    # The scope(s) the client requests for this API (e.g. api://<id>/access_as_user).
    ENTRA_SCOPES = _as_list(os.environ.get("ENTRA_SCOPES", "") or TEST_ENTRA_SCOPES)
    # WHO is allowed (fail-closed: at least one of these must match, else denied).
    #   users  = object ids (oid) and/or emails / UPNs (case-insensitive)
    #   groups = Entra security-group object ids (needs the groups claim configured)
    #   roles  = app-role values you defined on the app registration (recommended)
    ENTRA_ALLOWED_USERS = _as_list(os.environ.get("ENTRA_ALLOWED_USERS", ""))
    ENTRA_ALLOWED_GROUPS = _as_list(os.environ.get("ENTRA_ALLOWED_GROUPS", ""))
    ENTRA_ALLOWED_ROLES = _as_list(os.environ.get("ENTRA_ALLOWED_ROLES", ""))
    # Email of the admin who owns the app registration. Imported from a share code
    # so a colleague's "Request access" can address the approval email automatically.
    # Defaults to the built-in test tenant's admin so the email is pre-addressed.
    ENTRA_ADMIN_CONTACT = os.environ.get("ENTRA_ADMIN_CONTACT", "") or TEST_ENTRA_ADMIN_CONTACT

    # --- Dev Tunnel (public ingress, used by the orchestrated launcher.py) ---
    # When enabled, launcher.py starts a Microsoft Dev Tunnel once the server is
    # healthy, exposing a public HTTPS URL that forwards to the local port.
    TUNNEL_ENABLED = _as_bool(os.environ.get("TUNNEL_ENABLED"), True)
    TUNNEL_ID = os.environ.get("TUNNEL_ID", "copilot-bridge")
    # Tunnel-LAYER access control (who may even reach the relay). MS edition default
    # is 'private' = Microsoft Entra, OWNER-ONLY: only the account that signed into
    # Dev Tunnel on this host can connect (most secure; no per-user config needed).
    # Override with TUNNEL_AUTH=tenant (whole Entra tenant) | anonymous | org:<name>.
    TUNNEL_AUTH = (os.environ.get("TUNNEL_AUTH", "") or "private").strip().lower()
    # Retained for any legacy reader; derived from TUNNEL_AUTH now.
    TUNNEL_ANONYMOUS = (TUNNEL_AUTH == "anonymous")
    # Empty DEVTUNNEL_PATH => auto-discover (bundled tools/ -> PATH -> winget link).
    DEVTUNNEL_PATH = os.environ.get("DEVTUNNEL_PATH", "")
