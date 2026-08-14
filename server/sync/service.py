"""Lifecycle service for install-time, manual, and periodic synchronization."""

from __future__ import annotations

import asyncio
import time

from .coordinator import SyncCoordinator


class SyncService:
    def __init__(
        self,
        store,
        engine,
        auth,
        *,
        native_list=None,
        native_read=None,
        interval: float = 900,
    ):
        self.store = store
        self.engine = engine
        self.auth = auth
        self.interval = max(30.0, float(interval))
        self.coordinator = SyncCoordinator(
            store, engine, native_list=native_list, native_read=native_read
        )
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._running = False
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._last_error = ""
        self._last_result = self._empty_result()

    @staticmethod
    def _empty_result() -> dict:
        return {
            "importedNativeSessions": 0,
            "pushedPackageCount": 0,
            "pushedEventCount": 0,
            "pulledPackageCount": 0,
            "pulledEventCount": 0,
            "errors": [],
        }

    def describe(self) -> dict:
        return {
            "enabled": True,
            "running": self._running,
            "intervalSeconds": self.interval,
            "lastAttempt": self._last_attempt,
            "lastSuccess": self._last_success,
            "lastError": self._last_error,
            "lastResult": dict(self._last_result),
            "pendingEventCount": len(self.store.pending_sync_events(limit=1000)),
            "auth": self.auth.describe(),
        }

    async def _sync_locked(self) -> None:
        result = await self.coordinator.first_sync()
        self._last_result = {
            "importedNativeSessions": result.imported_native_sessions,
            "pushedPackageCount": result.pushed_package_count,
            "pushedEventCount": result.pushed_event_count,
            "pulledPackageCount": result.pulled_package_count,
            "pulledEventCount": result.pulled_event_count,
            "errors": list(result.errors),
        }
        self._last_success = time.time()
        self._last_error = ""

    async def connect(self) -> dict:
        async with self._lock:
            self._running = True
            self._last_attempt = time.time()
            try:
                await self.auth.connect()
                await self._sync_locked()
            except Exception as exc:
                self._last_error = str(exc)
                raise
            finally:
                self._running = False
        return self.describe()

    async def disconnect(self) -> dict:
        async with self._lock:
            await self.auth.disconnect()
            self._last_error = ""
        return self.describe()

    async def sync_now(self) -> dict:
        async with self._lock:
            if not self.auth.describe().get("connected"):
                raise RuntimeError("OneDrive is not connected")
            self._running = True
            self._last_attempt = time.time()
            try:
                await self._sync_locked()
            except Exception as exc:
                self._last_error = str(exc)
                raise
            finally:
                self._running = False
        return self.describe()

    async def run(self) -> None:
        if self.auth.describe().get("connected"):
            try:
                await self.sync_now()
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                if self.auth.describe().get("connected"):
                    try:
                        await self.sync_now()
                    except Exception:
                        pass

    async def close(self) -> None:
        self._stop.set()
        async with self._lock:
            close = getattr(self.engine.transport, "close", None)
            if close is not None:
                result = close()
                if result is not None:
                    await result
