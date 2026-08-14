"""Microsoft Graph delegated authentication with a DPAPI-protected MSAL cache."""

from __future__ import annotations

import asyncio
import ctypes
import os
import uuid
from ctypes import wintypes
from pathlib import Path

import msal

from paths import app_base_dir

GRAPH_SCOPES = ("Files.ReadWrite.AppFolder",)


class OneDriveAuthError(RuntimeError):
    pass


class OneDriveNotConnected(OneDriveAuthError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _input_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    blob = _DataBlob(
        len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return blob, buffer


def _dpapi_protect(value: bytes) -> bytes:
    if os.name != "nt":
        raise OneDriveAuthError("OneDrive token persistence requires Windows DPAPI")
    source, _buffer = _input_blob(value)
    protected = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "Copilot Bridge OneDrive token cache",
        None, None, None, 0x1, ctypes.byref(protected),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        kernel32.LocalFree(protected.pbData)


def _dpapi_unprotect(value: bytes) -> bytes:
    if os.name != "nt":
        raise OneDriveAuthError("OneDrive token persistence requires Windows DPAPI")
    source, _buffer = _input_blob(value)
    clear = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(clear),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(clear.pbData, clear.cbData)
    finally:
        kernel32.LocalFree(clear.pbData)


class DpapiFileStore:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or (app_base_dir() / "secrets" / "onedrive-token.cache"))

    def load(self) -> bytes:
        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError:
            return b""
        return _dpapi_unprotect(encrypted)

    def save(self, value: bytes) -> None:
        encrypted = _dpapi_protect(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp-{uuid.uuid4().hex}")
        try:
            with open(temporary, "xb") as file:
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class OneDriveAuth:
    def __init__(
        self,
        client_id: str,
        *,
        tenant_id: str = "common",
        secure_store=None,
        app_factory=None,
    ):
        self.client_id = str(client_id or "").strip()
        self.tenant_id = str(tenant_id or "common").strip()
        self.secure_store = secure_store or DpapiFileStore()
        self._lock = asyncio.Lock()
        self._cache = msal.SerializableTokenCache()
        self.cache_error = ""
        try:
            serialized = self.secure_store.load()
            if serialized:
                self._cache.deserialize(serialized.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.cache_error = str(exc)
        factory = app_factory or msal.PublicClientApplication
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self._app = factory(
            self.client_id, authority=authority, token_cache=self._cache
        )

    def _persist(self, force: bool = False) -> None:
        if force or self._cache.has_state_changed:
            self.secure_store.save(self._cache.serialize().encode("utf-8"))

    @staticmethod
    def _result_error(result: dict | None) -> str:
        if not result:
            return "Microsoft sign-in did not return a token"
        return str(
            result.get("error_description")
            or result.get("error")
            or "Microsoft sign-in did not return a token"
        )

    def describe(self) -> dict:
        accounts = self._app.get_accounts() if self.client_id else []
        account = accounts[0] if accounts else {}
        return {
            "configured": bool(self.client_id),
            "connected": bool(account),
            "username": str(account.get("username") or ""),
            "accountId": str(account.get("home_account_id") or ""),
            "scope": GRAPH_SCOPES[0],
            "cacheError": self.cache_error,
        }

    def _silent_token(self) -> str:
        if not self.client_id:
            raise OneDriveAuthError("OneDrive client id is not configured")
        for account in self._app.get_accounts():
            result = self._app.acquire_token_silent(list(GRAPH_SCOPES), account=account)
            if result and result.get("access_token"):
                self._persist()
                return str(result["access_token"])
        raise OneDriveNotConnected("OneDrive is not connected")

    async def get_access_token(self) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._silent_token)

    def _connect(self) -> dict:
        if not self.client_id:
            raise OneDriveAuthError("OneDrive client id is not configured")
        result = self._app.acquire_token_interactive(
            list(GRAPH_SCOPES), prompt="select_account", port=0, timeout=300
        )
        if not result or not result.get("access_token"):
            raise OneDriveAuthError(self._result_error(result))
        self._persist(force=True)
        return self.describe()

    async def connect(self) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._connect)

    def _disconnect(self) -> dict:
        for account in list(self._app.get_accounts()):
            self._app.remove_account(account)
        self.secure_store.clear()
        return self.describe()

    async def disconnect(self) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._disconnect)
