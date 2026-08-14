"""Poll native AI session adapters and create persistent inbox notifications.

Stable extension APIs do not expose all Copilot Chat lifecycle events. This
watcher therefore treats native stores as an eventually-consistent source:
first observation establishes a baseline, later completed assistant turns create
one deduplicated inbox item. Bridge-owned jobs use the same inbox table directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

import copilot_sessions

logger = logging.getLogger("copilot_bridge.session_watcher")


@dataclass(frozen=True)
class ScanResult:
    sessions_seen: int = 0
    baselined: int = 0
    notifications_created: int = 0
    errors: int = 0


class SessionWatcher:
    def __init__(self, store, discovery=copilot_sessions, *, interval: float = 5.0,
                 settle_scans: int = 0):
        self.store = store
        self.discovery = discovery
        self.interval = max(1.0, float(interval))
        self.settle_scans = max(0, int(settle_scans))
        self.last_scan_at: float | None = None
        self.last_error = ""
        self.sessions_seen = 0
        self._stopped = asyncio.Event()

    @staticmethod
    def _fingerprint(source_key: str, message: dict) -> str:
        payload = "\n".join((
            source_key,
            str(message.get("ts") or ""),
            str(message.get("text") or ""),
        ))
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _summary(text: str, limit: int = 240) -> str:
        collapsed = " ".join((text or "").split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit - 3].rstrip() + "..."

    @staticmethod
    def _adapter_id(source: str) -> str:
        return {
            "cli": "copilot-cli",
            "vscode-insiders": "vscode-insiders-copilot-chat",
            "vscode": "vscode-copilot-chat",
        }.get(source, f"{source}-session")

    def _linked_conversation_id(self, source: str, native_id: str) -> str | None:
        linked = self.store.find_by_external_ref(
            self._adapter_id(source), native_id
        )
        return linked.get("id") if linked else None

    @staticmethod
    def _latest_assistant(messages: list[dict]) -> dict | None:
        for message in reversed(messages):
            if message.get("role") == "assistant" and (message.get("text") or "").strip():
                return message
        return None

    async def scan_once(self) -> ScanResult:
        baselined = created = errors = 0
        try:
            summaries = list(self.discovery.list_sessions() or [])
        except Exception as exc:  # noqa: BLE001 - one adapter outage must not stop the server
            self.last_error = str(exc)
            self.last_scan_at = time.time()
            logger.warning("Native session listing failed: %s", exc)
            return ScanResult(errors=1)

        self.sessions_seen = len(summaries)
        for summary in summaries:
            source_key = str(summary.get("sourceKey") or "")
            source = str(summary.get("source") or "")
            native_id = str(summary.get("nativeId") or summary.get("id") or "")
            if not source_key or not source or not native_id:
                errors += 1
                continue
            try:
                detail = self.discovery.read_session_key(source_key)
                if detail is None:
                    continue
                messages = list(detail.get("messages") or [])
                count = len(messages)
                updated_at = detail.get("updatedAt") or summary.get("updatedAt")
                latest = self._latest_assistant(messages)
                fingerprint = str(detail.get("completionFingerprint") or "")
                if not fingerprint and latest:
                    fingerprint = self._fingerprint(source_key, latest)
                run_state = str(detail.get("runState") or "unknown")
                cursor = self.store.get_native_watch_cursor(source_key)
                if cursor is None:
                    self.store.upsert_native_watch_cursor(
                        source_key=source_key, source=source,
                        native_session_id=native_id,
                        observed_message_count=count,
                        notified_message_count=count,
                        last_assistant_fingerprint=fingerprint,
                        notified_assistant_fingerprint=fingerprint,
                        last_updated_at=updated_at,
                    )
                    baselined += 1
                    continue

                observed = int(cursor["observedMessageCount"])
                notified = int(cursor["notifiedMessageCount"])
                stable_scans = int(cursor["stableScans"])
                observed_fingerprint = cursor["lastAssistantFingerprint"]
                notified_fingerprint = cursor["notifiedAssistantFingerprint"]
                changed = count != observed or fingerprint != observed_fingerprint
                if changed:
                    stable_scans = 0
                else:
                    stable_scans += 1

                should_notify = (
                    count > notified
                    and latest is not None
                    and run_state in ("completed", "failed", "cancelled")
                    and stable_scans >= self.settle_scans
                    and fingerprint != notified_fingerprint
                )
                if should_notify:
                    _item, was_created = self.store.create_inbox_item(
                        dedupe_key=f"native-response:{source_key}:{fingerprint}",
                        source=source,
                        source_key=source_key,
                        native_session_id=native_id,
                        conversation_id=self._linked_conversation_id(source, native_id),
                        title=detail.get("title") or summary.get("title") or "Untitled conversation",
                        summary=self._summary(latest.get("text") or ""),
                        created_at=latest.get("ts") or updated_at,
                        metadata={"messageCount": count, "runState": run_state},
                    )
                    if was_created:
                        created += 1
                    notified = count
                    notified_fingerprint = fingerprint

                self.store.upsert_native_watch_cursor(
                    source_key=source_key, source=source,
                    native_session_id=native_id,
                    observed_message_count=count,
                    notified_message_count=notified,
                    last_assistant_fingerprint=fingerprint,
                    notified_assistant_fingerprint=notified_fingerprint,
                    last_updated_at=updated_at,
                    stable_scans=stable_scans,
                )
            except Exception as exc:  # noqa: BLE001 - isolate malformed native sessions
                errors += 1
                logger.debug("Native session scan failed for %s: %s", source_key, exc)

        self.last_scan_at = time.time()
        self.last_error = "" if not errors else f"{errors} session(s) failed"
        return ScanResult(
            sessions_seen=len(summaries), baselined=baselined,
            notifications_created=created, errors=errors,
        )

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.scan_once()
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stopped.set()

    def describe(self) -> dict:
        return {
            "lastScanAt": self.last_scan_at,
            "lastError": self.last_error,
            "sessionsSeen": self.sessions_seen,
            "intervalSeconds": self.interval,
        }