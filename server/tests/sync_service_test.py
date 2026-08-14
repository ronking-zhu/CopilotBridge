"""Tests the background/manual sync service and install-time first sync.

Run: python tests/sync_service_test.py (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from session_store import SessionStore
from sync.engine import SyncEngine
from sync.service import SyncService
from transports.filesystem import FileSystemTransport


class FakeAuth:
    def __init__(self):
        self.connected = False

    def describe(self):
        return {
            "configured": True, "connected": self.connected,
            "username": "person@example.com", "accountId": "account-1",
            "scope": "Files.ReadWrite.AppFolder", "cacheError": "",
        }

    async def connect(self):
        self.connected = True
        return self.describe()

    async def disconnect(self):
        self.connected = False
        return self.describe()


class NativeCatalog:
    def list(self):
        return [{"sourceKey": "cli:native-b", "source": "cli"}]

    def read(self, source_key):
        assert source_key == "cli:native-b"
        return {
            "id": "native-b", "nativeId": "native-b", "source": "cli",
            "title": "B before install", "updatedAt": 20,
            "messages": [
                {"role": "user", "text": "B question", "ts": 10},
                {"role": "assistant", "text": "B answer", "ts": 20},
            ],
        }


def add_conversation(store, title):
    conversation = store.create(title=title)
    store.append_message(conversation["id"], "user", "A question")
    store.append_message(conversation["id"], "assistant", "A answer")
    return conversation["id"]


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        cloud = os.path.join(tmp, "cloud")
        store_a = SessionStore(os.path.join(tmp, "a", "sessions"), device_name="Laptop A")
        store_b = SessionStore(os.path.join(tmp, "b", "sessions"), device_name="Laptop B")
        transport = FileSystemTransport(cloud)
        try:
            add_conversation(store_a, "A in cloud")
            engine_a = SyncEngine(store_a, transport, "default")
            assert (await engine_a.push()).event_count == 4

            catalog = NativeCatalog()
            service = SyncService(
                store_b,
                SyncEngine(store_b, transport, "default"),
                FakeAuth(),
                native_list=catalog.list,
                native_read=catalog.read,
                interval=900,
            )
            connected = await service.connect()
            assert connected["auth"]["connected"] is True
            assert connected["lastResult"]["importedNativeSessions"] == 1
            assert connected["lastResult"]["pulledEventCount"] == 4
            assert store_b.get(next(
                item["id"] for item in store_b.list() if item["title"] == "A in cloud"
            ))["machineName"] == "Laptop A"
            assert {item["title"] for item in store_b.list()} == {
                "A in cloud", "B before install",
            }
            assert store_b.pending_sync_events(100) == []

            repeated = await service.sync_now()
            assert repeated["lastResult"]["importedNativeSessions"] == 0
            assert repeated["lastResult"]["pushedEventCount"] == 0
            assert repeated["lastResult"]["pulledEventCount"] == 0
            assert repeated["lastError"] == ""
            assert len(store_b.list()) == 2

            disconnected = await service.disconnect()
            assert disconnected["auth"]["connected"] is False
            await service.close()
        finally:
            store_a.close()
            store_b.close()
    print("ALL SYNC SERVICE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())