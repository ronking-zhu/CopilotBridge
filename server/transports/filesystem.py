"""Filesystem transport for local folders and mounted SMB/Samba shares."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from .base import ObjectConflictError, RemoteObject, TransportHealth


class FileSystemTransport:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._executor = self._new_executor()
        self._executor_lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _new_executor() -> ThreadPoolExecutor:
        return ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="copilot-bridge-filesystem",
        )

    @staticmethod
    def _executor_unavailable(exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "cannot schedule new futures" in message and "shutdown" in message

    def _reset_executor(self, failed_executor: ThreadPoolExecutor) -> None:
        with self._executor_lock:
            if self._closed or self._executor is not failed_executor:
                return
            failed_executor.shutdown(wait=True, cancel_futures=True)
            self._executor = self._new_executor()

    async def _run(self, func, *args):
        if self._closed:
            raise RuntimeError("filesystem transport is closed")
        loop = asyncio.get_running_loop()
        executor = self._executor
        try:
            future = loop.run_in_executor(executor, func, *args)
        except RuntimeError as exc:
            if self._closed or not self._executor_unavailable(exc):
                raise
            self._reset_executor(executor)
            if self._closed:
                raise RuntimeError("filesystem transport is closed") from exc
            future = loop.run_in_executor(self._executor, func, *args)
        return await future

    def _path(self, key: str) -> str:
        normalized = str(key or "").replace("\\", "/").strip("/")
        parts = normalized.split("/") if normalized else []
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError(f"unsafe remote object key: {key!r}")
        path = os.path.abspath(os.path.join(self.root, *parts))
        if os.path.commonpath([self.root, path]) != self.root:
            raise ValueError(f"unsafe remote object key: {key!r}")
        return path

    @staticmethod
    def _hash_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _list(self, prefix: str) -> list[RemoteObject]:
        root = self._path(prefix) if prefix else self.root
        if not os.path.isdir(root):
            return []
        result: list[RemoteObject] = []
        for directory, _folders, files in os.walk(root):
            for name in files:
                if ".partial-" in name:
                    continue
                path = os.path.join(directory, name)
                stat = os.stat(path)
                key = os.path.relpath(path, self.root).replace(os.sep, "/")
                result.append(RemoteObject(key=key, size=stat.st_size, etag=str(stat.st_mtime_ns)))
        result.sort(key=lambda item: item.key)
        return result

    async def list(self, prefix: str) -> list[RemoteObject]:
        return await self._run(self._list, prefix)

    def _put_if_absent(self, key: str, content: bytes, expected_sha256: str) -> None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_sha256:
            raise ValueError("content hash does not match the requested SHA-256")
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            if self._hash_file(path) != expected_sha256:
                raise ObjectConflictError(f"remote object has different content: {key}")
            return
        temporary = f"{path}.partial-{uuid.uuid4().hex}"
        try:
            with open(temporary, "xb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if self._hash_file(path) != expected_sha256:
                    raise ObjectConflictError(f"remote object has different content: {key}")
            except OSError:
                if os.path.exists(path):
                    if self._hash_file(path) != expected_sha256:
                        raise ObjectConflictError(f"remote object has different content: {key}")
                else:
                    os.rename(temporary, path)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass

    async def put_if_absent(self, key: str, content: bytes, sha256: str) -> None:
        await self._run(self._put_if_absent, key, content, sha256)

    async def get(self, key: str) -> bytes:
        return await self._run(lambda: open(self._path(key), "rb").read())

    async def exists(self, key: str) -> bool:
        return await self._run(os.path.isfile, self._path(key))

    async def diagnose(self) -> TransportHealth:
        def check() -> TransportHealth:
            try:
                os.makedirs(self.root, exist_ok=True)
                probe = os.path.join(self.root, f".probe-{uuid.uuid4().hex}")
                with open(probe, "xb") as fh:
                    fh.write(b"ok")
                os.remove(probe)
                return TransportHealth(ok=True, detail=self.root)
            except OSError as exc:
                return TransportHealth(ok=False, detail=str(exc))
        return await self._run(check)

    def close(self) -> None:
        with self._executor_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
        executor.shutdown(wait=True, cancel_futures=True)