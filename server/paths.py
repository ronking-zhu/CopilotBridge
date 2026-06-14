"""Filesystem base paths that work both from source and from a frozen ``.exe``.

When the server runs from source, everything lives next to ``server/``. When it's
packaged with PyInstaller (``sys.frozen``):

* writable state (``.env``, ``connection.json``, ``sessions/``, ``workspace/``)
  lives in a **user-writable data dir** (``%LOCALAPPDATA%\\CopilotBridge``) so it
  works even when the program is installed read-only under ``Program Files`` and
  survives in-place upgrades. A portable build can override this by placing a
  ``.env`` (or a ``portable`` marker) next to the exe — then state stays beside
  the exe, which is handy for a USB-stick style deploy.
* bundled read-only resources (the ``webapp/`` assets) are extracted to
  ``sys._MEIPASS`` and must be read from there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _exe_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _portable_mode() -> bool:
    """Keep writable state next to the exe when a portable hint is present."""
    d = _exe_dir()
    return (d / "portable").exists() or (d / ".env").is_file()


def app_base_dir() -> Path:
    """Directory for writable state (.env, connection.json, sessions, workspace).

    Source run -> the ``server/`` dir. Frozen -> ``%LOCALAPPDATA%\\CopilotBridge``
    (user-writable, Program-Files-safe), unless portable mode keeps it next to
    the exe. The directory is created if missing.
    """
    if not is_frozen():
        return _SERVER_DIR
    if _portable_mode():
        base = _exe_dir()
    else:
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        base = Path(root) / "CopilotBridge"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def resource_dir() -> Path:
    """Directory holding bundled read-only resources (e.g. ``webapp/``).

    ``sys._MEIPASS`` when frozen (PyInstaller onefile), else ``server/``.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return _SERVER_DIR
