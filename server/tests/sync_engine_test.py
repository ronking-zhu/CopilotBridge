"""End-to-end tests for immutable package synchronization.

Run:  python tests/sync_engine_test.py   (from the server/ directory)
"""

import asyncio
import os
import sqlite3
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import session_store as session_store_module
from session_store import SessionStore
from sync.coordinator import SyncCoordinator
from sync.engine import SyncEngine
from transports.filesystem import FileSystemTransport


def add_conversation(store: SessionStore, title: str, question: str, answer: str) -> str:
    session_id = store.create(title=title)["id"]
    store.append_message(session_id, "user", question)
    store.append_message(session_id, "assistant", answer, ok=True, exit_code=0)
    return session_id


def fixed_device_store(path: str, device_id: str, device_name: str) -> SessionStore:
    sessions_dir = os.path.join(path, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    database = os.path.join(sessions_dir, SessionStore.DATABASE_NAME)
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('device_id', ?)", (device_id,)
        )
        connection.commit()
    finally:
        connection.close()
    return SessionStore(sessions_dir, device_name=device_name)


class NativeCatalog:
    def __init__(self):
        self.sessions = {
            "vscode-insiders:native-b": {
                "id": "native-b",
                "nativeId": "native-b",
                "sourceKey": "vscode-insiders:native-b",
                "source": "vscode-insiders",
                "title": "Existing on B",
                "updatedAt": 200,
                "messages": [
                    {"role": "user", "text": "question B", "ts": 100},
                    {"role": "assistant", "text": "answer B", "ts": 200},
                ],
            }
        }

    def list(self):
        return [
            {"sourceKey": item["sourceKey"], "source": item["source"]}
            for item in self.sessions.values()
        ]

    def read(self, source_key):
        return self.sessions.get(source_key)


async def test_concurrent_writes_to_synced_branch(tmp: str) -> None:
    cloud = os.path.join(tmp, "concurrent-cloud")
    store_a = SessionStore(
        os.path.join(tmp, "concurrent-a", "sessions"), device_name="Laptop A"
    )
    store_b = SessionStore(
        os.path.join(tmp, "concurrent-b", "sessions"), device_name="Laptop B"
    )
    transport_a = FileSystemTransport(cloud)
    transport_b = FileSystemTransport(cloud)
    try:
        session_id = add_conversation(
            store_a, "Shared conversation", "initial question", "initial answer"
        )
        engine_a = SyncEngine(store_a, transport_a, space_id="concurrent-space")
        engine_b = SyncEngine(store_b, transport_b, space_id="concurrent-space")
        assert (await engine_a.push()).event_count == 4
        assert (await engine_b.pull()).event_count == 4
        assert store_b.get(session_id)["machineName"] == "Laptop A"

        store_a.append_message(session_id, "user", "question from A")
        store_a.append_message(session_id, "assistant", "answer from A", ok=True)
        store_b.append_message(session_id, "user", "question from B")
        store_b.append_message(session_id, "assistant", "answer from B", ok=True)
        assert (await engine_a.push()).event_count == 2
        assert (await engine_b.push()).event_count == 3

        assert (await engine_a.pull()).event_count == 3
        assert (await engine_b.pull()).event_count == 2
        assert {item["name"] for item in store_a.list_devices()} == {"Laptop A", "Laptop B"}
        assert {item["name"] for item in store_b.list_devices()} == {"Laptop A", "Laptop B"}
        expected = {
            "initial question", "initial answer",
            "question from A", "answer from A",
            "question from B", "answer from B",
        }
        assert {item["text"] for item in store_a.get(session_id)["messages"]} == expected
        assert {item["text"] for item in store_b.get(session_id)["messages"]} == expected
        assert len(store_a.list_turns(session_id)) == 3
        assert len(store_b.list_turns(session_id)) == 3
        assert all(turn["messageCount"] == 2 for turn in store_a.list_turns(session_id))
        assert all(turn["messageCount"] == 2 for turn in store_b.list_turns(session_id))
    finally:
        transport_a.close()
        transport_b.close()
        store_a.close()
        store_b.close()


async def test_organization_sync(tmp: str) -> None:
    cloud = os.path.join(tmp, "organization-cloud")
    store_a = SessionStore(
        os.path.join(tmp, "organization-a", "sessions"), device_name="Laptop A"
    )
    store_b = SessionStore(
        os.path.join(tmp, "organization-b", "sessions"), device_name="Laptop B"
    )
    transport_a = FileSystemTransport(cloud)
    transport_b = FileSystemTransport(cloud)
    try:
        session_id = add_conversation(
            store_a, "Organized conversation", "question", "answer"
        )
        engine_a = SyncEngine(store_a, transport_a, space_id="organization-space")
        engine_b = SyncEngine(store_b, transport_b, space_id="organization-space")
        await engine_a.push()
        await engine_b.pull()

        store_b.update_organization(session_id, {
            "isFavorite": True,
            "isPinned": True,
            "project": "Copilot Bridge",
            "labels": ["Release", "Windows"],
        })
        assert (await engine_b.push()).event_count == 2
        assert (await engine_a.pull()).event_count == 2
        organized = store_a.get(session_id)
        assert organized["isFavorite"] is True
        assert organized["isPinned"] is True
        assert organized["project"] == "Copilot Bridge"
        assert organized["labels"] == ["Release", "Windows"]
        assert store_a.pending_sync_events(100) == []
    finally:
        transport_a.close()
        transport_b.close()
        store_a.close()
        store_b.close()


async def test_knowledge_sync(tmp: str) -> None:
    cloud = os.path.join(tmp, "knowledge-cloud")
    store_a = SessionStore(
        os.path.join(tmp, "knowledge-a", "sessions"), device_name="Laptop A"
    )
    store_b = SessionStore(
        os.path.join(tmp, "knowledge-b", "sessions"), device_name="Laptop B"
    )
    transport_a = FileSystemTransport(cloud)
    transport_b = FileSystemTransport(cloud)
    try:
        session_id = add_conversation(
            store_a, "Knowledge source", "Why immutable packages?",
            "They avoid multi-device overwrite conflicts.",
        )
        engine_a = SyncEngine(store_a, transport_a, space_id="knowledge-space")
        engine_b = SyncEngine(store_b, transport_b, space_id="knowledge-space")
        await engine_a.push()
        await engine_b.pull()

        messages = store_a.get(session_id)["messages"]
        created = store_a.create_knowledge_item(
            knowledge_type="decision",
            title="Use immutable sync packages",
            body_markdown="Each device writes immutable packages.",
            evidence_message_ids=[message["id"] for message in messages],
            project="Copilot Bridge",
        )
        assert (await engine_a.push()).event_count == 4
        assert (await engine_b.pull()).event_count == 4
        mirrored = store_b.get_knowledge_item(created["id"])
        assert mirrored["bodyMarkdown"] == created["bodyMarkdown"]
        assert {value["messageId"] for value in mirrored["evidence"]} == {
            message["id"] for message in messages
        }
        wrong_session_id = add_conversation(
            store_b, "Wrong evidence source", "wrong question", "wrong answer"
        )
        source = messages[0]
        corrupt_evidence = {
            "eventId": "corrupt-evidence-event",
            "deviceId": "corrupt-device",
            "deviceSeq": 1,
            "entityType": "knowledge_evidence",
            "entityId": "corrupt-evidence",
            "operation": "created",
            "createdAt": 1,
            "payload": {
                "id": "corrupt-evidence",
                "knowledgeId": created["id"],
                "versionId": mirrored["currentVersionId"],
                "conversationId": wrong_session_id,
                "branchId": source["branchId"],
                "turnId": source["turnId"],
                "messageId": source["id"],
                "snippet": source["text"],
            },
        }
        try:
            store_b.apply_remote_events(
                [corrupt_evidence], "corrupt-device", through_seq=1
            )
            raise AssertionError("corrupt knowledge evidence was accepted")
        except ValueError as exc:
            assert "source provenance is inconsistent" in str(exc)

        store_b.add_knowledge_version(
            created["id"],
            body_markdown=created["bodyMarkdown"] + "\n\nNever overwrite another device.",
            evidence_message_ids=[messages[1]["id"]],
            confidence="high",
        )
        store_b.update_knowledge_item(created["id"], {"status": "verified"})
        assert (await engine_b.push()).event_count > 0
        assert (await engine_a.pull()).event_count > 0
        verified = store_a.get_knowledge_item(created["id"])
        assert verified["status"] == "verified"
        assert verified["versionNumber"] == 2
        assert len(verified["versions"]) == 2
        assert store_a.pending_sync_events(100) == []

        assert store_b.delete_knowledge_item(created["id"]) is True
        await engine_b.push()
        await engine_a.pull()
        assert store_a.get_knowledge_item(created["id"]) is None
        assert store_a.pending_sync_events(100) == []
    finally:
        transport_a.close()
        transport_b.close()
        store_a.close()
        store_b.close()


async def test_concurrent_knowledge_versions(tmp: str) -> None:
    cloud = os.path.join(tmp, "concurrent-knowledge-cloud")
    store_a = SessionStore(
        os.path.join(tmp, "concurrent-knowledge-a", "sessions"),
        device_name="Laptop A",
    )
    store_b = SessionStore(
        os.path.join(tmp, "concurrent-knowledge-b", "sessions"),
        device_name="Laptop B",
    )
    transport_a = FileSystemTransport(cloud)
    transport_b = FileSystemTransport(cloud)
    try:
        session_id = add_conversation(
            store_a, "Concurrent knowledge", "Which package model?",
            "Use immutable packages.",
        )
        engine_a = SyncEngine(store_a, transport_a, space_id="knowledge-race")
        engine_b = SyncEngine(store_b, transport_b, space_id="knowledge-race")
        await engine_a.push()
        await engine_b.pull()
        messages = store_a.get(session_id)["messages"]
        created = store_a.create_knowledge_item(
            knowledge_type="decision",
            title="Use immutable packages",
            body_markdown="Each device writes immutable packages.",
            evidence_message_ids=[message["id"] for message in messages],
        )
        await engine_a.push()
        await engine_b.pull()

        concurrent_body = "Both devices record the same conclusion."
        original_now = session_store_module._now
        session_store_module._now = lambda: 4_000_000_000.0
        try:
            store_a.add_knowledge_version(
                created["id"], body_markdown=concurrent_body,
                evidence_message_ids=[messages[0]["id"]],
            )
            store_b.add_knowledge_version(
                created["id"], body_markdown=concurrent_body,
                evidence_message_ids=[messages[0]["id"]],
            )
        finally:
            session_store_module._now = original_now
        await engine_a.push()
        await engine_b.push()
        await engine_a.pull()
        await engine_b.pull()

        versions_a = store_a.get_knowledge_item(created["id"])["versions"]
        versions_b = store_b.get_knowledge_item(created["id"])["versions"]
        assert len(versions_a) == len(versions_b) == 3
        assert {
            (version["id"], version["versionNumber"]) for version in versions_a
        } == {
            (version["id"], version["versionNumber"]) for version in versions_b
        }
        assert {
            version["id"]: version["inputDigest"] for version in versions_a
        } == {
            version["id"]: version["inputDigest"] for version in versions_b
        }
        item_a = store_a.get_knowledge_item(created["id"])
        item_b = store_b.get_knowledge_item(created["id"])
        assert item_a["currentVersionId"] == item_b["currentVersionId"]
        assert item_a["bodyMarkdown"] == item_b["bodyMarkdown"]
        assert (await engine_a.pull()).event_count == 0
        assert (await engine_b.pull()).event_count == 0
    finally:
        transport_a.close()
        transport_b.close()
        store_a.close()
        store_b.close()


async def test_third_device_defers_knowledge_dependencies(tmp: str) -> None:
    cloud = os.path.join(tmp, "third-device-cloud")
    source = fixed_device_store(
        os.path.join(tmp, "third-source"), "z-source", "Source"
    )
    knowledge = fixed_device_store(
        os.path.join(tmp, "third-knowledge"), "a-knowledge", "Knowledge"
    )
    third = fixed_device_store(
        os.path.join(tmp, "third-reader"), "m-reader", "Reader"
    )
    transports = [FileSystemTransport(cloud) for _ in range(3)]
    try:
        session_id = add_conversation(
            source, "Source conversation", "What is the decision?",
            "Use immutable packages.",
        )
        source_engine = SyncEngine(source, transports[0], space_id="third-device")
        knowledge_engine = SyncEngine(
            knowledge, transports[1], space_id="third-device"
        )
        third_engine = SyncEngine(third, transports[2], space_id="third-device")
        await source_engine.push()
        await knowledge_engine.pull()
        messages = knowledge.get(session_id)["messages"]
        created = knowledge.create_knowledge_item(
            knowledge_type="decision", title="Use immutable packages",
            body_markdown="Each device writes immutable packages.",
            evidence_message_ids=[message["id"] for message in messages],
        )
        await knowledge_engine.push()

        result = await third_engine.pull()
        assert result.package_count == 2
        assert third.get(session_id) is not None
        mirrored = third.get_knowledge_item(created["id"])
        assert mirrored is not None
        assert {value["messageId"] for value in mirrored["evidence"]} == {
            message["id"] for message in messages
        }
    finally:
        for transport in transports:
            transport.close()
        source.close()
        knowledge.close()
        third.close()


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cloud = os.path.join(tmp, "cloud")
        store_a = SessionStore(
            os.path.join(tmp, "laptop-a", "sessions"), device_name="Laptop A"
        )
        store_b = SessionStore(
            os.path.join(tmp, "laptop-b", "sessions"), device_name="Laptop B"
        )
        try:
            session_a = add_conversation(store_a, "From A", "question A", "answer A")
            session_b = add_conversation(store_b, "From B", "question B", "answer B")

            engine_a = SyncEngine(store_a, FileSystemTransport(cloud), space_id="test-space")
            engine_b = SyncEngine(store_b, FileSystemTransport(cloud), space_id="test-space")

            pushed_a = await engine_a.push()
            pushed_b = await engine_b.push()
            assert pushed_a.event_count == 4 and pushed_b.event_count == 4
            assert pushed_a.package_key != pushed_b.package_key

            pulled_a = await engine_a.pull()
            pulled_b = await engine_b.pull()
            assert pulled_a.event_count == 4 and pulled_b.event_count == 4
            assert store_a.get(session_b)["messages"][0]["text"] == "question B"
            assert store_b.get(session_a)["messages"][1]["text"] == "answer A"
            assert {item["title"] for item in store_a.list()} == {"From A", "From B"}
            assert {item["title"] for item in store_b.list()} == {"From A", "From B"}
            assert store_a.get(session_b)["machineName"] == "Laptop B"
            assert store_b.get(session_a)["machineName"] == "Laptop A"

            # Applying downloaded events must not create a local upload loop.
            assert store_a.pending_sync_events(100) == []
            assert store_b.pending_sync_events(100) == []

            # Repeating pull is a no-op because event ids and per-device cursors are durable.
            assert (await engine_a.pull()).event_count == 0
            assert (await engine_b.pull()).event_count == 0

            objects = await FileSystemTransport(cloud).list("spaces/test-space/v1/devices")
            assert len([item for item in objects if item.key.endswith(".cbe")]) == 2

            # First install on B: cloud already contains A while B already has
            # native VS Code history. Import/push B before pulling A so neither
            # side can overwrite or hide the other.
            first_cloud = os.path.join(tmp, "first-sync-cloud")
            first_a = SessionStore(
                os.path.join(tmp, "first-a", "sessions"), device_name="Laptop A"
            )
            first_b = SessionStore(
                os.path.join(tmp, "first-b", "sessions"), device_name="Laptop B"
            )
            try:
                first_a_id = add_conversation(
                    first_a, "Uploaded from A", "question A", "answer A"
                )
                first_a_engine = SyncEngine(
                    first_a, FileSystemTransport(first_cloud), space_id="first-space"
                )
                assert (await first_a_engine.push()).event_count == 4

                catalog = NativeCatalog()
                first_b_engine = SyncEngine(
                    first_b, FileSystemTransport(first_cloud), space_id="first-space"
                )
                coordinator = SyncCoordinator(
                    first_b, first_b_engine,
                    native_list=catalog.list, native_read=catalog.read,
                )
                first = await coordinator.first_sync()
                assert first.imported_native_sessions == 1
                assert first.pushed_event_count > 0
                assert first.pulled_event_count == 4
                assert {item["title"] for item in first_b.list()} == {
                    "Uploaded from A", "Existing on B",
                }
                assert first_b.pending_sync_events(100) == []

                local_b = first_b.find_by_external_ref(
                    "vscode-insiders-copilot-chat", "native-b"
                )
                assert local_b is not None
                assert local_b["messages"][1]["text"] == "answer B"
                assert first_b.get(first_a_id)["messages"][1]["text"] == "answer A"

                # A subsequently receives B, and rerunning B's first-sync path
                # remains idempotent rather than creating another import.
                assert (await first_a_engine.pull()).event_count > 0
                assert {item["title"] for item in first_a.list()} == {
                    "Uploaded from A", "Existing on B",
                }
                repeated = await coordinator.first_sync()
                assert repeated.imported_native_sessions == 0
                assert repeated.pushed_event_count == 0
                assert repeated.pulled_event_count == 0
                assert len(first_b.list()) == 2
            finally:
                first_a.close()
                first_b.close()
        finally:
            store_a.close()
            store_b.close()
        await test_concurrent_writes_to_synced_branch(tmp)
        await test_organization_sync(tmp)
        await test_knowledge_sync(tmp)
        await test_concurrent_knowledge_versions(tmp)
        await test_third_device_defers_knowledge_dependencies(tmp)
    print("ALL MODULAR SYNC ENGINE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())