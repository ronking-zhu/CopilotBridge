"""Provider-neutral object transport contract for synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ObjectConflictError(RuntimeError):
    """The remote key already exists with different content."""


@dataclass(frozen=True)
class RemoteObject:
    key: str
    size: int
    etag: str = ""


@dataclass(frozen=True)
class TransportHealth:
    ok: bool
    detail: str = ""


class SyncTransport(Protocol):
    """Minimal capabilities required from OneDrive, S3, SMB, or SFTP."""

    async def list(self, prefix: str) -> list[RemoteObject]: ...

    async def put_if_absent(self, key: str, content: bytes, sha256: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def diagnose(self) -> TransportHealth: ...