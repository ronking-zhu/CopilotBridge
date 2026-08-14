"""Versioned encoding for immutable synchronization event packages."""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass

FORMAT_NAME = "copilotbridge-sync-events"
FORMAT_VERSION = 1


@dataclass(frozen=True)
class EncodedPackage:
    content: bytes
    sha256: str
    source_device_id: str
    first_seq: int
    last_seq: int
    event_count: int


def encode_events(events: list[dict]) -> EncodedPackage:
    if not events:
        raise ValueError("cannot encode an empty sync package")
    ordered = sorted(events, key=lambda event: int(event["deviceSeq"]))
    source = str(ordered[0]["deviceId"])
    if any(event.get("deviceId") != source for event in ordered):
        raise ValueError("all events in a package must come from one device")
    first_seq = int(ordered[0]["deviceSeq"])
    last_seq = int(ordered[-1]["deviceSeq"])
    document = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "sourceDeviceId": source,
        "firstSeq": first_seq,
        "lastSeq": last_seq,
        "events": ordered,
    }
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    document["payloadSha256"] = hashlib.sha256(canonical).hexdigest()
    raw = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    content = gzip.compress(raw, compresslevel=6, mtime=0)
    return EncodedPackage(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        source_device_id=source,
        first_seq=first_seq,
        last_seq=last_seq,
        event_count=len(ordered),
    )


def decode_events(content: bytes, expected_sha256: str = "") -> dict:
    actual = hashlib.sha256(content).hexdigest()
    if expected_sha256 and not actual.startswith(expected_sha256):
        raise ValueError("sync package SHA-256 mismatch")
    try:
        document = json.loads(gzip.decompress(content).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid sync package") from exc
    if document.get("format") != FORMAT_NAME or document.get("version") != FORMAT_VERSION:
        raise ValueError("unsupported sync package format or version")
    payload_hash = document.pop("payloadSha256", "")
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if not payload_hash or hashlib.sha256(canonical).hexdigest() != payload_hash:
        raise ValueError("sync package payload SHA-256 mismatch")
    events = document.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("sync package has no events")
    source = document.get("sourceDeviceId")
    if any(event.get("deviceId") != source for event in events):
        raise ValueError("sync package mixes source devices")
    sequences = [int(event["deviceSeq"]) for event in events]
    if min(sequences) != int(document.get("firstSeq")) or max(sequences) != int(document.get("lastSeq")):
        raise ValueError("sync package sequence metadata is inconsistent")
    if len({event.get("eventId") for event in events}) != len(events):
        raise ValueError("sync package contains duplicate event ids")
    document["payloadSha256"] = payload_hash
    return document