"""Coordinate native discovery with deterministic first-device synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


def adapter_for_native_source(source: str) -> str:
    return {
        "cli": "copilot-cli",
        "vscode": "vscode-copilot-chat",
        "vscode-insiders": "vscode-insiders-copilot-chat",
    }.get(source, source or "copilot-cli")


@dataclass(frozen=True)
class FirstSyncResult:
    imported_native_sessions: int = 0
    pushed_package_count: int = 0
    pushed_event_count: int = 0
    pulled_package_count: int = 0
    pulled_event_count: int = 0
    errors: tuple[str, ...] = ()


class SyncCoordinator:
    """Preserve pre-existing local history while joining a populated sync space."""

    def __init__(
        self,
        store,
        engine,
        *,
        native_list: Callable[[], Iterable[dict]] | None = None,
        native_read: Callable[[str], dict | None] | None = None,
    ):
        self.store = store
        self.engine = engine
        self.native_list = native_list or (lambda: ())
        self.native_read = native_read or (lambda _source_key: None)

    def import_native_sessions(self) -> tuple[int, tuple[str, ...]]:
        imported = 0
        errors: list[str] = []
        try:
            summaries = sorted(
                self.native_list(),
                key=lambda item: str(item.get("sourceKey") or item.get("id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return 0, (f"native discovery failed: {exc}",)

        for summary in summaries:
            source_key = str(summary.get("sourceKey") or summary.get("id") or "")
            if not source_key:
                errors.append("native session is missing sourceKey")
                continue
            try:
                data = self.native_read(source_key)
                if not data:
                    errors.append(f"native session disappeared: {source_key}")
                    continue
                source = str(data.get("source") or summary.get("source") or "cli")
                native_id = str(data.get("nativeId") or data.get("id") or "")
                if not native_id:
                    errors.append(f"native session is missing id: {source_key}")
                    continue
                adapter_id = adapter_for_native_source(source)
                if self.store.find_by_external_ref(adapter_id, native_id) is not None:
                    continue

                session = self.store.create(title=str(data.get("title") or ""))
                conversation_id = session["id"]
                self.store.replace_messages(
                    conversation_id,
                    data.get("messages") or [],
                    title=str(data.get("title") or ""),
                    updated_at=data.get("updatedAt"),
                )
                self.store.add_external_ref(
                    conversation_id,
                    adapter_id,
                    native_id,
                    capabilities={"canResume": source == "cli"},
                )
                if source == "cli":
                    self.store.get_or_create_execution_binding(
                        conversation_id, "copilot", native_session_id=native_id
                    )
                imported += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{source_key}: {exc}")
        return imported, tuple(errors)

    async def _push_all(self) -> tuple[int, int]:
        package_count = 0
        event_count = 0
        while True:
            result = await self.engine.push()
            if result.event_count == 0:
                return package_count, event_count
            package_count += result.package_count
            event_count += result.event_count

    async def first_sync(self) -> FirstSyncResult:
        imported, errors = self.import_native_sessions()
        pushed_packages, pushed_events = await self._push_all()
        pulled = await self.engine.pull()
        return FirstSyncResult(
            imported_native_sessions=imported,
            pushed_package_count=pushed_packages,
            pushed_event_count=pushed_events,
            pulled_package_count=pulled.package_count,
            pulled_event_count=pulled.event_count,
            errors=errors,
        )
