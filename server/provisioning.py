"""First-run provisioning for the Copilot Bridge app server.

Makes the server self-configuring so it can be shipped as a single ``.exe`` that
"just works" on any host:

* :func:`ensure_api_token` generates a strong, **host-unique** control-plane
    ``CHAT_API_TOKEN`` the first time the server runs and persists it to ``.env``.
* :func:`write_connection_card` records the public URL + host control key for
    local desktop helpers in ``connection.json`` and returns a printable block.

Both are idempotent and never overwrite a value the user set themselves.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from paths import app_base_dir

# Writable state lives in the base dir: the server/ folder from source, or next
# to the .exe when frozen (so a host's generated key persists across runs).
DEFAULT_ENV_PATH = app_base_dir() / ".env"
CONNECTION_PATH = app_base_dir() / "connection.json"


def _read_env_token(env_path: Path) -> str:
    """Return the current CHAT_API_TOKEN value from .env, or '' if absent/empty."""
    return _read_env_value(env_path, "CHAT_API_TOKEN")


def _read_env_value(env_path: Path, key: str) -> str:
    """Return the current ``key`` value from .env, or '' if absent/empty/commented."""
    if not env_path.is_file():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith(f"{key}=") and not s.startswith("#"):
                return s.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def _upsert_env_line(env_path: Path, key: str, value: str) -> None:
    """Set ``key=value`` in .env, replacing an existing line or appending it.

    Preserves every other line. Creates the file (and parents) if missing.
    """
    line = f"{key}={value}"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    replaced = False
    for i, existing in enumerate(lines):
        stripped = existing.strip()
        if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
            lines[i] = line
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(line)

    # Trailing newline keeps the file POSIX-clean.
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_token(nbytes: int = 32) -> str:
    """Return a strong, URL-safe random token (~43 chars for 32 bytes)."""
    return secrets.token_urlsafe(nbytes)


def ensure_api_token(env_path: Path | str = DEFAULT_ENV_PATH) -> tuple[str, bool]:
    """Return ``(token, created)``.

    If ``.env`` already has a non-empty ``CHAT_API_TOKEN`` it is returned as-is
    (``created=False``). Otherwise a fresh host-unique token is generated,
    written to ``.env``, exported to ``os.environ`` for the current process, and
    returned (``created=True``).
    """
    env_path = Path(env_path)
    existing = _read_env_token(env_path)
    if existing:
        os.environ.setdefault("CHAT_API_TOKEN", existing)
        return existing, False

    token = generate_token()
    _upsert_env_line(env_path, "CHAT_API_TOKEN", token)
    os.environ["CHAT_API_TOKEN"] = token
    return token, True


def ensure_tunnel_id(env_path: Path | str = DEFAULT_ENV_PATH) -> tuple[str, bool]:
    """Return ``(tunnel_id, created)`` — a **host-unique** Dev Tunnel id.

    Dev Tunnel ids are GLOBALLY unique across the whole Dev Tunnels service, so a
    hardcoded shared id (the old ``copilot-bridge``) collides: only the first
    account to claim it can host it, and everyone else gets
    ``Unauthorized tunnel access ... expected [host]``. We therefore mint a
    per-host id (``copilot-bridge-<random>``) on first run and persist it to
    ``.env`` (and ``os.environ``), exactly like the API key. An id the user set
    themselves is respected.
    """
    env_path = Path(env_path)
    existing = _read_env_value(env_path, "TUNNEL_ID")
    if existing and existing != "copilot-bridge":
        os.environ.setdefault("TUNNEL_ID", existing)
        return existing, False

    # Lowercase letters/digits/hyphens only; short random suffix avoids collisions.
    tunnel_id = "copilot-bridge-" + secrets.token_hex(4)
    _upsert_env_line(env_path, "TUNNEL_ID", tunnel_id)
    os.environ["TUNNEL_ID"] = tunnel_id
    return tunnel_id, True


_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _dsregcmd_status() -> str:
    """Return raw ``dsregcmd /status`` text (Windows), or '' on any failure."""
    import subprocess

    try:
        proc = subprocess.run(
            ["dsregcmd", "/status"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def detect_entra_tenant() -> tuple[str, str]:
    """Best-effort ``(tenant_id, tenant_name)`` of the Entra ID tenant THIS Windows
    device is joined/registered to, or ``('', '')`` if it can't be determined.

    Parses ``dsregcmd /status`` for ``TenantId`` (a GUID) and ``TenantName``. Works
    for Entra-joined, hybrid-joined, and workplace-joined (registered) devices, and
    never raises -- callers treat an empty result as "not detected".
    """
    text = _dsregcmd_status()
    if not text:
        return "", ""
    tenant_id = ""
    tenant_name = ""
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        k = key.strip().lower()
        v = val.strip()
        if not tenant_id and k == "tenantid" and _GUID_RE.match(v):
            tenant_id = v
        elif not tenant_name and k == "tenantname" and v:
            tenant_name = v
        if tenant_id and tenant_name:
            break
    return tenant_id, tenant_name


def ensure_entra_tenant(env_path: Path | str = DEFAULT_ENV_PATH) -> tuple[str, bool]:
    """Return ``(tenant_id, created)``.

    On first run, pre-fill ``ENTRA_TENANT_ID`` in ``.env`` from the tenant this
    machine is already joined to, so an admin who later turns on Microsoft sign-in
    (``AUTH_MODE=entra``) doesn't have to hunt for the GUID. Respects a value the
    user already set, and **never** changes ``AUTH_MODE`` -- detection only fills the
    tenant; sign-in stays off until the admin opts in and adds an app registration
    plus an allowlist. Returns ``('', False)`` when nothing could be detected.
    """
    env_path = Path(env_path)
    existing = _read_env_value(env_path, "ENTRA_TENANT_ID")
    if existing:
        os.environ.setdefault("ENTRA_TENANT_ID", existing)
        return existing, False

    tenant_id, _name = detect_entra_tenant()
    if not tenant_id:
        return "", False
    _upsert_env_line(env_path, "ENTRA_TENANT_ID", tenant_id)
    os.environ["ENTRA_TENANT_ID"] = tenant_id
    return tenant_id, True


# Maps the keyword args of :func:`set_identity_config` to their ``.env`` keys.
_IDENTITY_ENV_KEYS = {
    "auth_mode": "AUTH_MODE",
    "tenant_id": "ENTRA_TENANT_ID",
    "client_id": "ENTRA_CLIENT_ID",
    "audience": "ENTRA_AUDIENCE",
    "scopes": "ENTRA_SCOPES",
    "allowed_users": "ENTRA_ALLOWED_USERS",
    "allowed_groups": "ENTRA_ALLOWED_GROUPS",
    "allowed_roles": "ENTRA_ALLOWED_ROLES",
    # Who to email an access request to (the admin who owns the app registration).
    # Carried in a share code so a colleague's “Request access” knows where to send.
    "admin_contact": "ENTRA_ADMIN_CONTACT",
}


def set_identity_config(env_path: Path | str = DEFAULT_ENV_PATH, **fields) -> dict:
    """Upsert identity / Entra settings into ``.env`` (and ``os.environ``).

    Accepts any of ``auth_mode, tenant_id, client_id, audience, scopes,
    allowed_users, allowed_groups, allowed_roles`` as strings (the list-valued ones
    are stored as their raw comma-separated text). ``None`` values are skipped so a
    caller can update just a subset. Returns the ``{ENV_KEY: value}`` map written.
    """
    env_path = Path(env_path)
    written: dict = {}
    for arg, env_key in _IDENTITY_ENV_KEYS.items():
        if fields.get(arg) is None:
            continue
        value = str(fields[arg]).strip()
        _upsert_env_line(env_path, env_key, value)
        os.environ[env_key] = value
        written[env_key] = value
    return written


def set_port(port, env_path: Path | str = DEFAULT_ENV_PATH) -> int:
    """Persist the server listen ``PORT`` to ``.env`` (and ``os.environ``).

    Used by the Control Panel's "change port" action. Validates the range and
    returns the normalized int. The next server start reads this value; the tunnel
    forwards the same port.
    """
    p = int(str(port).strip())
    if p < 1 or p > 65535:
        raise ValueError("port must be between 1 and 65535")
    _upsert_env_line(Path(env_path), "PORT", str(p))
    os.environ["PORT"] = str(p)
    return p


def get_allowed_users(env_path: Path | str = DEFAULT_ENV_PATH) -> list[str]:
    """Return the current ``ENTRA_ALLOWED_USERS`` allow-list read fresh from ``.env``.

    A list of the comma-separated entries (emails / UPNs / object ids), with blanks
    dropped and surrounding whitespace stripped. Reads the file directly (not the
    process snapshot) so the Control Panel reflects edits made since startup.
    """
    raw = _read_env_value(Path(env_path), "ENTRA_ALLOWED_USERS")
    return [u.strip() for u in raw.split(",") if u.strip()]


def set_allowed_users(users, env_path: Path | str = DEFAULT_ENV_PATH) -> str:
    """Persist ``users`` (a list/iterable) as ``ENTRA_ALLOWED_USERS`` in ``.env``.

    De-duplicates case-insensitively while preserving order, joins with commas, and
    writes via :func:`set_identity_config`. Returns the stored comma-separated text.
    """
    seen: set = set()
    ordered: list = []
    for u in users or []:
        u = str(u).strip()
        if not u:
            continue
        low = u.lower()
        if low in seen:
            continue
        seen.add(low)
        ordered.append(u)
    value = ",".join(ordered)
    set_identity_config(env_path=env_path, allowed_users=value)
    return value


def detect_current_user() -> str:
    """Best-effort UPN of the Windows user currently signed in, or ``''``.

    Runs ``whoami /upn``, which returns the Entra ID / Active Directory User
    Principal Name (e.g. ``alice@contoso.com``) on a joined device. Returns ``''``
    on any failure (e.g. a local-only account) so callers treat it as "unknown".
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["whoami", "/upn"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


# A short, human-pasteable prefix so a share code is recognizable and versioned.
_SHARE_PREFIX = "CBCFG1."


def _b64url_encode(raw: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    import base64
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def make_share_code(tenant_id, client_id, audience="", scopes="", admin_contact="") -> str:
    """Pack the **non-secret** Entra sign-in config into a shareable code.

    A colleague who runs their own Copilot Bridge against the same tenant/app pastes
    this instead of hand-typing tenant id, client id, audience and scopes. It carries
    ONLY public identifiers — never the API key, the allow-list, or any secret — so it
    is safe to send over chat/email. ``audience``/``scopes`` may be a list or a
    comma-separated string. ``admin_contact`` (optional) is the admin's email so the
    colleague's “Request access” can address the approval mail automatically.
    Returns ``''`` if there's nothing meaningful to share.
    """
    import json

    def _csv(v):
        if isinstance(v, (list, tuple)):
            return ",".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()

    tenant_id = str(tenant_id or "").strip()
    client_id = str(client_id or "").strip()
    if not tenant_id or not client_id:
        return ""
    payload = {
        "v": 1,
        "t": tenant_id,
        "c": client_id,
        "a": _csv(audience),
        "s": _csv(scopes),
    }
    if str(admin_contact or "").strip():
        payload["m"] = str(admin_contact).strip()
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _SHARE_PREFIX + _b64url_encode(raw)


def parse_share_code(code) -> dict:
    """Decode a :func:`make_share_code` string back to identity fields.

    Returns ``{tenant_id, client_id, audience, scopes}`` (audience/scopes as the raw
    comma-separated strings). Raises ``ValueError`` if the code is malformed, so the
    caller can show a friendly "that doesn't look like a valid code" message.
    """
    import json

    if not code:
        raise ValueError("empty share code")
    code = str(code).strip()
    if not code.startswith(_SHARE_PREFIX):
        raise ValueError("not a Copilot Bridge sign-in code")
    try:
        data = json.loads(_b64url_decode(code[len(_SHARE_PREFIX):]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"corrupt share code: {exc}") from exc
    tenant_id = str(data.get("t", "")).strip()
    client_id = str(data.get("c", "")).strip()
    if not tenant_id or not client_id:
        raise ValueError("share code is missing the tenant or client id")
    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "audience": str(data.get("a", "")).strip(),
        "scopes": str(data.get("s", "")).strip(),
        "admin_contact": str(data.get("m", "")).strip(),
    }


# Access-request code: a colleague packs WHO they are + their redirect URL so the
# admin can approve (guest invite + register redirect URI) in one command.
_REQUEST_PREFIX = "CBREQ1."


def make_request_code(account, public_url, tenant_id="", allowed_users="") -> str:
    """Pack a colleague's access request into a code.

    Carries the requester's account, the FULL allow-list they want granted, and
    their Dev Tunnel redirect URL. The admin runs ``setup-entra.ps1 -ApproveRequest
    <code>`` (or pastes it) to invite those accounts as guests and register the
    redirect URI. Carries no secret. ``allowed_users`` may be a list or a
    comma-separated string. Returns ``''`` if ``account`` is empty.
    """
    import json

    def _csv(v):
        if isinstance(v, (list, tuple)):
            return ",".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()

    account = str(account or "").strip()
    if not account:
        return ""
    url = str(public_url or "").strip()
    if url:
        url = url.rstrip("/") + "/"
    payload = {"v": 1, "u": account, "r": url, "t": str(tenant_id or "").strip()}
    users = _csv(allowed_users)
    if users:
        payload["al"] = users
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _REQUEST_PREFIX + _b64url_encode(raw)


def parse_request_code(code) -> dict:
    """Decode a :func:`make_request_code` string.

    Returns ``{account, public_url, tenant_id, allowed_users}`` (the last as the raw
    comma-separated string). Raises ``ValueError`` if malformed so the caller can
    show a friendly message.
    """
    import json

    if not code:
        raise ValueError("empty request code")
    code = str(code).strip()
    if not code.startswith(_REQUEST_PREFIX):
        raise ValueError("not a Copilot Bridge access-request code")
    try:
        data = json.loads(_b64url_decode(code[len(_REQUEST_PREFIX):]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"corrupt request code: {exc}") from exc
    account = str(data.get("u", "")).strip()
    if not account:
        raise ValueError("request code is missing the account")
    return {
        "account": account,
        "public_url": str(data.get("r", "")).strip(),
        "tenant_id": str(data.get("t", "")).strip(),
        "allowed_users": str(data.get("al", "")).strip(),
    }


def build_access_request_mailto(admin_email, account, public_url,
                                request_code="", app_name="Copilot Bridge",
                                allowed_users="") -> str:
    """Return a ``mailto:`` URL that opens a pre-filled access-request email.

    The body lists the colleague's FULL allow-list (every account to invite as a
    guest + add to ``ENTRA_ALLOWED_USERS``) and their Dev Tunnel redirect URL, plus
    the machine-readable request code. Opening it (e.g. via ``webbrowser.open``)
    launches the user's mail client with the admin addressed — no SMTP/credentials
    needed. ``allowed_users`` may be a list or a comma-separated string.
    """
    from urllib.parse import quote

    def _as_users(v):
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return [x.strip() for x in str(v or "").split(",") if x.strip()]

    account = str(account or "").strip()
    public_url = str(public_url or "").strip()
    users = _as_users(allowed_users) or ([account] if account else [])
    who = account or (users[0] if users else "a colleague")
    if users:
        users_block = "\n".join(f"    - {u}" for u in users)
    else:
        users_block = "    (none yet — add yourself under \u201cAllow users\u201d first)"
    subject = f"[{app_name}] Access request from {who}"
    lines = [
        "Hi,",
        "",
        f"Please grant access to {app_name} (Microsoft Entra sign-in).",
        "",
        "  Accounts to invite as guests + add to the allow-list:",
        users_block,
        "",
        f"  Redirect URI (Dev Tunnel): {public_url or '(starting the server will create it)'}",
        "",
        "To approve in one command (you need rights to the app registration):",
        f"  .\\scripts\\setup-entra.ps1 -ApproveRequest {request_code or '<request-code>'}",
        "",
        "Or do it manually: invite the account(s) above as guests in Entra, and add",
        "the redirect URI to the app\u2019s Single-page application platform.",
    ]
    if request_code:
        lines += ["", "Request code:", request_code]
    body = "\n".join(lines)
    to = quote(str(admin_email or "").strip())
    return f"mailto:{to}?subject={quote(subject)}&body={quote(body)}"


def write_connection_card(
    public_url: str | None,
    local_url: str,
    api_key: str,
    provider: str = "copilot",
    path: Path | str = CONNECTION_PATH,
    *,
    auth_mode: str = "tunnel",
    tunnel_auth: str = "private",
) -> str:
    """Persist the client connection details and return a printable block.

    The host control key remains in the local JSON for desktop helpers, but is
    shown to users only when legacy API-key browser authentication is active.
    """
    server_url = public_url or local_url
    auth_mode = (auth_mode or "tunnel").strip().lower()
    tunnel_auth = (tunnel_auth or "private").strip().lower()
    card = {
        "serverUrl": server_url,
        "localUrl": local_url,
        "publicUrl": public_url,
        "apiKey": api_key,
        "authMode": auth_mode,
        "tunnelAuth": tunnel_auth,
        "provider": provider,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        Path(path).write_text(json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    bar = "-" * 62
    lines = [
        bar,
        "  COPILOT BRIDGE CONNECTION",
        f"    Server URL : {server_url}",
    ]
    if public_url and public_url != local_url:
        lines.append(f"    Local URL  : {local_url}   (same machine only)")
    if auth_mode == "tunnel":
        remote_auth = (
            "Microsoft tenant sign-in" if tunnel_auth == "tenant"
            else "Microsoft owner sign-in"
        )
        lines.append(f"    Browser auth: localhost trusted; remote {remote_auth}")
    elif auth_mode == "entra":
        lines.append("    Browser auth: Microsoft Entra sign-in")
    elif auth_mode == "both":
        lines.append("    Browser auth: Microsoft Entra sign-in or API key")
        lines.append(f"    API Key    : {api_key or '(not configured)'}")
    else:
        lines.append("    Browser auth: API key")
        lines.append(f"    API Key    : {api_key or '(not configured)'}")
    lines += [
        f"    AI Provider: {provider}",
        f"    Saved to   : {Path(path)}",
        bar,
    ]
    return "\n".join(lines)


def read_connection_card(path: Path | str = CONNECTION_PATH) -> dict | None:
    """Return the saved ``connection.json`` as a dict, or ``None`` if missing.

    Written by :func:`write_connection_card` on each successful run, so it holds
    the exact endpoint + key this machine's clients should use.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
