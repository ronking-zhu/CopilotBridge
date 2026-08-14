"""Local OneDrive desktop-folder fallback for immutable sync packages."""

from __future__ import annotations

import os
from pathlib import Path

from paths import app_base_dir


def _existing_path(value: str | os.PathLike | None) -> Path | None:
    if not value:
        return None
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    return path.resolve() if path.is_dir() else None


def detect_onedrive_root(preferred: str = "") -> Path | None:
    """Find a signed-in local OneDrive root, preferring work/school accounts."""
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    for name in ("OneDriveCommercial", "OneDrive", "OneDriveConsumer"):
        value = os.environ.get(name)
        if value:
            candidates.append(value)

    if os.name == "nt":
        try:
            import winreg

            base = r"Software\Microsoft\OneDrive\Accounts"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as accounts:
                names: list[str] = []
                index = 0
                while True:
                    try:
                        names.append(winreg.EnumKey(accounts, index))
                        index += 1
                    except OSError:
                        break
                names.sort(key=lambda name: (not name.lower().startswith("business"), name))
                for name in names:
                    try:
                        with winreg.OpenKey(accounts, name) as account:
                            folder, _kind = winreg.QueryValueEx(account, "UserFolder")
                            candidates.append(folder)
                    except OSError:
                        continue
        except OSError:
            pass

    seen: set[str] = set()
    for candidate in candidates:
        path = _existing_path(candidate)
        if path is None:
            continue
        key = os.path.normcase(str(path))
        if key not in seen:
            return path
        seen.add(key)
    return None


class LocalOneDriveConnection:
    """Persist explicit consent to use an already signed-in OneDrive folder."""

    def __init__(
        self,
        onedrive_root: str | os.PathLike,
        *,
        marker_path: str | os.PathLike | None = None,
    ):
        root = _existing_path(onedrive_root)
        if root is None:
            raise ValueError("local OneDrive folder does not exist")
        self.onedrive_root = root
        self.sync_root = root / "Apps" / "Copilot Bridge"
        self.marker_path = Path(
            marker_path or (app_base_dir() / "sync" / "onedrive-local.enabled")
        )

    def _marker_matches(self) -> bool:
        try:
            stored = self.marker_path.read_text(encoding="utf-8").strip()
            return os.path.normcase(stored) == os.path.normcase(str(self.onedrive_root))
        except OSError:
            return False

    def describe(self) -> dict:
        return {
            "configured": True,
            "connected": self._marker_matches(),
            "username": self.onedrive_root.name,
            "accountId": "",
            "scope": "Local OneDrive folder",
            "cacheError": "",
            "mode": "local-folder",
            "root": str(self.sync_root),
        }

    async def connect(self) -> dict:
        self.sync_root.mkdir(parents=True, exist_ok=True)
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(str(self.onedrive_root), encoding="utf-8")
        return self.describe()

    async def disconnect(self) -> dict:
        try:
            self.marker_path.unlink()
        except FileNotFoundError:
            pass
        return self.describe()
