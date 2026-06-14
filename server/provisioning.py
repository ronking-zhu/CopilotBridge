"""First-run provisioning for the Copilot Bridge app server.

Makes the server self-configuring so it can be shipped as a single ``.exe`` that
"just works" on any host:

* :func:`ensure_api_token` generates a strong, **host-unique** ``CHAT_API_TOKEN``
  the first time the server runs and persists it to ``.env``. Two different
  machines therefore end up with two different keys automatically.
* :func:`write_connection_card` records the public URL + API key (the exact
  values a client needs) to ``connection.json`` and returns a printable block.

Both are idempotent and never overwrite a value the user set themselves.
"""

from __future__ import annotations

import json
import os
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
    if not env_path.is_file():
        return ""
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("CHAT_API_TOKEN=") and not s.startswith("#"):
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


def write_connection_card(
    public_url: str | None,
    local_url: str,
    api_key: str,
    provider: str = "copilot",
    path: Path | str = CONNECTION_PATH,
) -> str:
    """Persist the client connection details and return a printable block.

    The URL is host-specific (the Dev Tunnel address) and the key is host-unique,
    so the saved card is exactly what *this* machine's clients should use.
    """
    server_url = public_url or local_url
    card = {
        "serverUrl": server_url,
        "localUrl": local_url,
        "publicUrl": public_url,
        "apiKey": api_key,
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
        "  CLIENT CONNECTION (paste these into the app's Settings)",
        f"    Server URL : {server_url}",
    ]
    if public_url and public_url != local_url:
        lines.append(f"    Local URL  : {local_url}   (same machine only)")
    lines += [
        f"    API Key    : {api_key or '(none — auth disabled)'}",
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
