"""Push and pull immutable event packages through a provider-neutral transport."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .packages import decode_events, encode_events

_PACKAGE_RE = re.compile(
    r"^spaces/([^/]+)/v1/devices/([^/]+)/events/(\d+)-(\d+)-([0-9a-f]{20})\.cbe$"
)
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class SyncResult:
    package_count: int = 0
    event_count: int = 0
    package_key: str = ""


class SyncEngine:
    def __init__(self, store, transport, space_id: str):
        if not _SAFE_SEGMENT_RE.fullmatch(space_id or ""):
            raise ValueError("sync space id contains unsupported characters")
        self.store = store
        self.transport = transport
        self.space_id = space_id
        self.base_prefix = f"spaces/{space_id}/v1"

    async def push(self, limit: int = 200) -> SyncResult:
        events = self.store.pending_sync_events(limit=limit)
        if not events:
            return SyncResult()
        package = encode_events(events)
        key = (
            f"{self.base_prefix}/devices/{package.source_device_id}/events/"
            f"{package.first_seq}-{package.last_seq}-{package.sha256[:20]}.cbe"
        )
        await self.transport.put_if_absent(key, package.content, package.sha256)
        self.store.mark_sync_events_published(
            [event["eventId"] for event in events], key
        )
        return SyncResult(package_count=1, event_count=len(events), package_key=key)

    async def pull(self) -> SyncResult:
        prefix = f"{self.base_prefix}/devices"
        objects = await self.transport.list(prefix)
        candidates: list[tuple[str, int, int, str, str]] = []
        for item in objects:
            match = _PACKAGE_RE.fullmatch(item.key)
            if not match or match.group(1) != self.space_id:
                continue
            device_id = match.group(2)
            if device_id == self.store.device_id:
                continue
            candidates.append((
                device_id, int(match.group(3)), int(match.group(4)),
                match.group(5), item.key,
            ))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))

        package_count = 0
        event_count = 0
        blocked_devices: set[str] = set()
        for device_id, first_seq, last_seq, expected_hash, key in candidates:
            if device_id in blocked_devices:
                continue
            cursor = self.store.sync_cursor(device_id)
            if last_seq <= cursor:
                continue
            if first_seq > cursor + 1:
                blocked_devices.add(device_id)
                continue
            document = decode_events(await self.transport.get(key), expected_hash)
            if document["sourceDeviceId"] != device_id:
                raise ValueError("sync package path and source device disagree")
            applied = self.store.apply_remote_events(
                document["events"], device_id, int(document["lastSeq"])
            )
            package_count += 1
            event_count += applied
        return SyncResult(package_count=package_count, event_count=event_count)

    async def sync_once(self) -> SyncResult:
        pushed = await self.push()
        pulled = await self.pull()
        return SyncResult(
            package_count=pushed.package_count + pulled.package_count,
            event_count=pushed.event_count + pulled.event_count,
            package_key=pushed.package_key,
        )