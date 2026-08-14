"""Focused tests for the SQLite-backed session store foundation.

Run:  python tests/session_store_test.py   (from the server/ directory)
"""

import json
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from session_store import SessionStore


def test_json_migration_and_compatibility(tmp: str) -> None:
    sessions_dir = os.path.join(tmp, "sessions")
    os.makedirs(sessions_dir)
    legacy = {
        "id": "legacy-session",
        "title": "Legacy title",
        "createdAt": 10.0,
        "updatedAt": 12.0,
        "messageCount": 2,
        "messages": [
            {"role": "user", "text": "legacy question", "ts": 10.0},
            {"role": "assistant", "text": "legacy answer", "ts": 12.0, "ok": True},
        ],
    }
    with open(os.path.join(sessions_dir, "legacy-session.json"), "w", encoding="utf-8") as fh:
        json.dump(legacy, fh)

    store = SessionStore(sessions_dir)
    migrated = store.get("legacy-session")
    assert migrated is not None
    assert migrated["title"] == "Legacy title"
    assert [m["text"] for m in migrated["messages"]] == ["legacy question", "legacy answer"]
    assert all(m.get("id") for m in migrated["messages"])
    assert migrated["messages"][0]["turnId"] == migrated["messages"][1]["turnId"]
    assert os.path.isfile(store.database_path)
    assert [(event["entityType"], event["operation"]) for event in store.pending_sync_events(20)] == [
        ("device", "updated"),
        ("conversation", "created"),
        ("message", "created"),
        ("message", "created"),
        ("external_ref", "created"),
    ]

    # Reopening is idempotent: the legacy JSON is not imported twice.
    store.close()
    reopened = SessionStore(sessions_dir)
    assert reopened.get("legacy-session")["messageCount"] == 2
    reopened.close()


def test_turn_timeline_and_settings(tmp: str) -> None:
    store = SessionStore(os.path.join(tmp, "sessions"))
    session = store.create(title="Timeline")
    sid = session["id"]
    question = "A  question\nwith   deliberately repeated whitespace and a long suffix"
    store.append_message(sid, "user", question)
    store.append_message(sid, "assistant", "answer")
    store.append_message(sid, "user", "follow-up")
    store.append_message(sid, "assistant", "second answer")

    assert store.get(sid)["promptCount"] == 2
    assert store.list()[0]["promptCount"] == 2
    assert store.summarize(store.get(sid))["promptCount"] == 2

    turns = store.list_turns(sid, preview_length=20)
    assert len(turns) == 2
    assert turns[0]["previewText"] == "A question with d..."
    assert turns[0]["messageCount"] == 2

    defaults = store.get_settings()
    assert defaults["promptPreviewLength"] == 200
    updated = store.update_settings({"promptPreviewLength": 300})
    assert updated["promptPreviewLength"] == 300

    # The full message is retained; changing the setting only changes projection.
    assert store.get(sid)["messages"][0]["text"] == question
    assert store.list_turns(sid)[0]["previewText"] == "A question with deliberately repeated whitespace and a long suffix"

    try:
        store.update_settings({"promptPreviewLength": 5})
    except ValueError:
        pass
    else:
        raise AssertionError("preview lengths below the supported range must be rejected")
    store.close()


def test_outbox_records_local_mutations(tmp: str) -> None:
    store = SessionStore(os.path.join(tmp, "sessions"))
    sid = store.create(title="Sync me")["id"]
    store.append_message(sid, "user", "hello")
    store.append_message(sid, "assistant", "world")
    store.rename(sid, "Renamed")

    events = store.pending_sync_events(limit=100)
    operations = [(e["entityType"], e["operation"]) for e in events]
    assert operations == [
        ("device", "updated"),
        ("conversation", "created"),
        ("message", "created"),
        ("message", "created"),
        ("conversation", "updated"),
    ]
    assert len({e["eventId"] for e in events}) == len(events)
    assert all(e["deviceId"] == store.device_id for e in events)

    store.mark_sync_events_published([e["eventId"] for e in events[:2]], "test-package")
    assert [e["eventId"] for e in store.pending_sync_events(100)] == [
        e["eventId"] for e in events[2:]
    ]
    store.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        test_json_migration_and_compatibility(os.path.join(tmp, "migration"))
        test_turn_timeline_and_settings(os.path.join(tmp, "timeline"))
        test_outbox_records_local_mutations(os.path.join(tmp, "outbox"))
    print("ALL SQLITE SESSION STORE TESTS PASSED")


if __name__ == "__main__":
    main()