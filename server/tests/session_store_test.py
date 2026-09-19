"""Focused tests for the SQLite-backed session store foundation.

Run:  python tests/session_store_test.py   (from the server/ directory)
"""

import json
import os
import sqlite3
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
    assert store.get(sid)["isFavorite"] is False
    assert store.get(sid)["isPinned"] is False
    assert store.get(sid)["project"] == ""
    assert store.get(sid)["labels"] == []

    turns = store.list_turns(sid, preview_length=20)
    assert len(turns) == 2
    assert turns[0]["previewText"] == "A question with d..."
    assert turns[0]["messageCount"] == 2

    defaults = store.get_settings()
    assert defaults["promptPreviewLength"] == 200
    assert defaults["uiLanguage"] == "zh"
    updated = store.update_settings({"promptPreviewLength": 300, "uiLanguage": "en"})
    assert updated["promptPreviewLength"] == 300
    assert updated["uiLanguage"] == "en"

    # The full message is retained; changing the setting only changes projection.
    assert store.get(sid)["messages"][0]["text"] == question
    assert store.list_turns(sid)[0]["previewText"] == "A question with deliberately repeated whitespace and a long suffix"

    try:
        store.update_settings({"promptPreviewLength": 5})
    except ValueError:
        pass
    else:
        raise AssertionError("preview lengths below the supported range must be rejected")
    try:
        store.update_settings({"uiLanguage": "fr"})
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported UI languages must be rejected")
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


def test_conversation_organization(tmp: str) -> None:
    store = SessionStore(os.path.join(tmp, "sessions"))
    sid = store.create(title="Organize me")["id"]
    updated = store.update_organization(sid, {
        "isFavorite": True,
        "isPinned": True,
        "project": "  Copilot   Bridge  ",
        "labels": ["Release", " release ", "Windows"],
    })
    assert updated["isFavorite"] is True
    assert updated["isPinned"] is True
    assert updated["project"] == "Copilot Bridge"
    assert updated["labels"] == ["Release", "Windows"]
    assert store.list()[0]["isPinned"] is True
    event = store.pending_sync_events(100)[-1]
    assert (event["entityType"], event["operation"]) == ("conversation", "organized")
    assert event["payload"]["labels"] == ["Release", "Windows"]

    before = store.get(sid)
    before_event_count = len(store.pending_sync_events(100))
    original_record_event = store._record_event
    calls = 0

    def fail_second_event(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected metadata event failure")
        return original_record_event(*args, **kwargs)

    store._record_event = fail_second_event
    try:
        store.update_metadata(sid, {
            "title": "Must roll back", "project": "Must roll back",
        })
        raise AssertionError("metadata fault did not abort the transaction")
    except RuntimeError as exc:
        assert "injected metadata event failure" in str(exc)
    finally:
        store._record_event = original_record_event
    after = store.get(sid)
    assert after["title"] == before["title"]
    assert after["project"] == before["project"]
    assert len(store.pending_sync_events(100)) == before_event_count
    store.close()


def test_knowledge_foundation(tmp: str) -> None:
    store = SessionStore(os.path.join(tmp, "sessions"))
    session = store.create(title="Knowledge source")
    store.append_message(session["id"], "user", "Use immutable event packages")
    store.append_message(
        session["id"], "assistant", "Each device writes its own package"
    )
    messages = store.get(session["id"])["messages"]

    item = store.create_knowledge_item(
        knowledge_type="decision",
        title="Use immutable sync packages",
        body_markdown="Each device writes **immutable packages** in its own namespace.",
        evidence_message_ids=[message["id"] for message in messages],
        project="Copilot Bridge",
        labels=["Sync", "Architecture"],
        confidence="high",
    )
    assert item["type"] == "decision"
    assert item["status"] == "draft"
    assert item["project"] == "Copilot Bridge"
    assert item["labels"] == ["Sync", "Architecture"]
    assert item["versionNumber"] == 1
    assert len(item["evidence"]) == 2
    assert all(value["messageId"] for value in item["evidence"])
    assert all("&message=" in value["deepLink"] for value in item["evidence"])

    prepared = store.prepare_knowledge_extraction(session["id"], "reconcile-v1")
    store.apply_knowledge_extraction(
        session["id"], "reconcile-v1", prepared["inputDigest"],
        prepared["sourceMessageIds"], [],
    )
    store.replace_messages(session["id"], messages, title=session["title"])
    reconciled = store.get_knowledge_item(item["id"])
    assert {value["messageId"] for value in reconciled["evidence"]} == {
        message["id"] for message in messages
    }
    after_reconcile = store.prepare_knowledge_extraction(
        session["id"], "reconcile-v1"
    )
    assert after_reconcile["cached"] is True
    assert after_reconcile["noNewMessages"] is True

    event_count = len(store.pending_sync_events(100))
    duplicate = store.add_knowledge_version(
        item["id"],
        body_markdown=item["bodyMarkdown"],
        evidence_message_ids=[message["id"] for message in messages],
        confidence="high",
    )
    assert len(duplicate["versions"]) == 1
    assert len(store.pending_sync_events(100)) == event_count

    updated = store.add_knowledge_version(
        item["id"],
        body_markdown=item["bodyMarkdown"] + "\n\nNever overwrite another device.",
        evidence_message_ids=[messages[1]["id"]],
        confidence="high",
    )
    assert updated["versionNumber"] == 2
    assert len(updated["versions"]) == 2
    assert len(updated["evidence"]) == 1
    verified = store.update_knowledge_item(item["id"], {"status": "verified"})
    assert verified["status"] == "verified"
    assert store.list_knowledge_items(
        query="immutable", status="verified", conversation_id=session["id"]
    )[0]["id"] == item["id"]
    assert store.delete_knowledge_item(item["id"]) is True
    assert store.get_knowledge_item(item["id"]) is None
    store.close()


def test_old_schema_migrates_to_v6(tmp: str) -> None:
    sessions_dir = os.path.join(tmp, "sessions")
    os.makedirs(sessions_dir)
    database = os.path.join(sessions_dir, SessionStore.DATABASE_NAME)
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            deleted_at REAL
        );
        CREATE TABLE inbox_items (
            id TEXT PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            source_key TEXT NOT NULL,
            native_session_id TEXT,
            conversation_id TEXT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unread',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            seen_at REAL,
            metadata_json TEXT
        );
        PRAGMA user_version=1;
        """
    )
    connection.close()

    store = SessionStore(sessions_dir)
    conversation_columns = {
        row[1] for row in store._conn.execute(
            "PRAGMA table_info(conversations)"
        ).fetchall()
    }
    inbox_columns = {
        row[1] for row in store._conn.execute(
            "PRAGMA table_info(inbox_items)"
        ).fetchall()
    }
    assert {
        "is_favorite", "is_pinned", "project", "labels_json",
        "organization_updated_at",
    } <= conversation_columns
    assert "remind_at" in inbox_columns
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 6
    tables = {
        row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "knowledge_items", "knowledge_versions", "knowledge_evidence",
        "knowledge_extraction_runs", "knowledge_maps", "knowledge_generation_jobs",
    }.issubset(tables)
    session = store.create(title="Migrated")
    assert store.update_organization(
        session["id"], {"isFavorite": True}
    )["isFavorite"] is True
    store.close()


def test_knowledge_generation_job_state(tmp: str) -> None:
    sessions_dir = os.path.join(tmp, "sessions")
    store = SessionStore(sessions_dir)
    session = store.create(title="Generate knowledge")
    sid = session["id"]

    queued, created = store.begin_knowledge_generation_job(
        sid, max_conversations=24
    )
    assert created is True
    assert queued["status"] == "queued"
    assert queued["maxConversations"] == 24
    duplicate, created = store.begin_knowledge_generation_job(sid)
    assert created is False
    assert duplicate["startedAt"] == queued["startedAt"]

    running = store.update_knowledge_generation_job(
        sid, status="running", phase="extracting",
        processed_message_count=3, remaining_message_count=1,
        generated_item_count=2,
    )
    assert running["progressPercent"] == 60
    assert store.list_knowledge_generation_jobs()[0]["conversationTitle"] == (
        "Generate knowledge"
    )
    completed = store.update_knowledge_generation_job(
        sid, status="completed", phase="completed", result_map_id="history"
    )
    assert completed["progressPercent"] == 100
    assert completed["completedAt"] is not None

    restarted, created = store.begin_knowledge_generation_job(sid)
    assert created is True
    assert restarted["status"] == "queued"
    store.close()

    reopened = SessionStore(sessions_dir)
    interrupted = reopened.get_knowledge_generation_job(sid)
    assert interrupted["status"] == "failed"
    assert "interrupted" in interrupted["error"].lower()
    reopened.close()


def test_knowledge_map_history_and_cache(tmp: str) -> None:
    store = SessionStore(os.path.join(tmp, "sessions"))
    try:
        first = store.create(title="SQLite migration")
        store.update_organization(first["id"], {"project": "Copilot Bridge"})
        store.append_message(
            first["id"], "user", "How should schema migrations preserve user data?"
        )
        store.append_message(
            first["id"], "assistant", "Use additive migrations and verify row counts."
        )
        second = store.create(title="Knowledge evidence")
        store.update_organization(second["id"], {"project": "Copilot Bridge"})
        store.append_message(
            second["id"], "user", "Every knowledge claim needs message evidence."
        )
        store.append_message(
            second["id"], "assistant", "Store stable conversation and message ids."
        )
        noise = store.create(title="Reply with exactly: TEST_NOISE")
        store.append_message(noise["id"], "user", "Reply with exactly: TEST_NOISE")
        store.append_message(noise["id"], "assistant", "TEST_NOISE")
        focused = store.create(title="Focused short conversation")
        for index in range(2):
            store.append_message(
                focused["id"], "user", f"Focused question {index + 1}"
            )
            store.append_message(
                focused["id"], "assistant", f"Focused answer {index + 1}"
            )

        prepared = store.prepare_knowledge_map(
            "knowledge-map-test-v1", max_chars=8000, max_conversations=8
        )
        assert prepared["cached"] is False
        assert prepared["eligibleConversationCount"] == 2
        assert prepared["selectedConversationCount"] == 2
        assert noise["id"] not in {
            item["conversationId"] for item in prepared["conversations"]
        }
        assert focused["id"] not in {
            item["conversationId"] for item in prepared["conversations"]
        }
        focused_prepared = store.prepare_knowledge_map(
            "knowledge-map-test-v1", max_chars=8000, max_conversations=8,
            focus_conversation_id=focused["id"],
        )
        assert focused_prepared["conversations"][0]["conversationId"] == focused["id"]
        assert focused_prepared["conversations"][0]["focus"] is True
        assert len(focused_prepared["conversations"][0]["messages"]) == 4
        assert focused_prepared["eligibleConversationCount"] == 3
        by_conversation = {
            item["conversationId"]: item for item in prepared["conversations"]
        }
        first_user_id = by_conversation[first["id"]]["messages"][0]["messageId"]
        second_user_id = by_conversation[second["id"]]["messages"][0]["messageId"]
        assert {first_user_id, second_user_id} <= set(prepared["sourceMessageIds"])
        map_payload = {
            "title": "Copilot Bridge knowledge",
            "summary": "Migration and evidence practices.",
            "nodes": [{
                "id": "migration", "parentId": None, "label": "Migrations",
                "summary": "Preserve rows during additive schema changes.",
                "kind": "topic", "evidenceMessageIds": [first_user_id],
            }],
        }
        saved = store.save_knowledge_map(map_payload,
            input_digest=prepared["inputDigest"],
            extractor_version=prepared["extractorVersion"],
            source_message_ids=prepared["sourceMessageIds"],
            source_conversation_count=prepared["selectedConversationCount"])
        assert saved["nodes"][0]["evidence"][0]["messageId"] == first_user_id
        assert saved["nodes"][0]["missingEvidenceCount"] == 0
        assert saved["sourceConversationCount"] == 2
        stale_payload = json.loads(store._conn.execute(
            "SELECT map_json FROM knowledge_maps WHERE id='history'"
        ).fetchone()["map_json"])
        stale_payload["nodes"][0]["evidenceMessageIds"].append("missing-message")
        with store._conn:
            store._conn.execute(
                "UPDATE knowledge_maps SET map_json=? WHERE id='history'",
                (json.dumps(stale_payload),),
            )
        stale = store.get_knowledge_map()
        assert stale["nodes"][0]["evidence"][0]["messageId"] == first_user_id
        assert stale["nodes"][0]["missingEvidenceCount"] == 1
        assert store.prepare_knowledge_map(
            "knowledge-map-test-v1", max_chars=8000, max_conversations=8
        )["cached"] is True
        version_changed = store.prepare_knowledge_map(
            "knowledge-map-test-v2", max_chars=8000, max_conversations=8
        )
        assert version_changed["cached"] is False
        assert version_changed["inputDigest"] == prepared["inputDigest"]
        version_saved = store.save_knowledge_map(map_payload,
            input_digest=version_changed["inputDigest"],
            extractor_version=version_changed["extractorVersion"],
            source_message_ids=version_changed["sourceMessageIds"],
            source_conversation_count=version_changed["selectedConversationCount"])
        assert version_saved["extractorVersion"] == "knowledge-map-test-v2"
        assert store.prepare_knowledge_map(
            "knowledge-map-test-v2", max_chars=8000, max_conversations=8
        )["cached"] is True
        store.append_message(second["id"], "user", "A new historical decision")
        assert store.prepare_knowledge_map(
            "knowledge-map-test-v2", max_chars=8000, max_conversations=8
        )["cached"] is False

        empty_focus = store.create(title="Empty focused conversation")
        try:
            store.prepare_knowledge_map(
                "knowledge-map-test-v2", max_chars=4000, max_conversations=8,
                focus_conversation_id=empty_focus["id"],
            )
            raise AssertionError("empty focused conversation was accepted")
        except ValueError as exc:
            assert str(exc) == "focused conversation has no eligible messages"

        oversized = store.create(title="T" * 10000)
        for index in range(60):
            store.append_message(
                oversized["id"], "user" if index % 2 == 0 else "assistant",
                f"Bounded message {index}: " + ("content " * 200),
            )
        bounded = store.prepare_knowledge_map(
            "knowledge-map-test-v2", max_chars=4000, max_conversations=8,
            focus_conversation_id=oversized["id"],
        )
        bounded_json = json.dumps(
            bounded["conversations"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        assert len(bounded_json) <= 4000
        assert bounded["conversations"][0]["conversationId"] == oversized["id"]
        assert bounded["conversations"][0]["messages"][0]["messageId"] in (
            bounded["sourceMessageIds"]
        )
    finally:
        store.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        test_json_migration_and_compatibility(os.path.join(tmp, "migration"))
        test_turn_timeline_and_settings(os.path.join(tmp, "timeline"))
        test_outbox_records_local_mutations(os.path.join(tmp, "outbox"))
        test_conversation_organization(os.path.join(tmp, "organization"))
        test_knowledge_foundation(os.path.join(tmp, "knowledge"))
        test_old_schema_migrates_to_v6(os.path.join(tmp, "schema-migration"))
        test_knowledge_generation_job_state(os.path.join(tmp, "knowledge-job"))
        test_knowledge_map_history_and_cache(os.path.join(tmp, "knowledge-map"))
    print("ALL SQLITE SESSION STORE TESTS PASSED")


if __name__ == "__main__":
    main()