"""Tests persistent inbox notifications from native AI session changes.

Run:  python tests/session_watcher_test.py   (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile
import threading

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from session_store import SessionStore
from session_watcher import SessionWatcher


class FakeDiscovery:
    def __init__(self):
        self.sessions: dict[str, dict] = {}

    def list_sessions(self):
        return [
            {
                "id": item["nativeId"],
                "nativeId": item["nativeId"],
                "sourceKey": key,
                "source": item["source"],
                "title": item["title"],
                "updatedAt": item.get("updatedAt", 1.0),
                "messageCount": len(item["messages"]),
            }
            for key, item in self.sessions.items()
        ]

    def read_session_key(self, source_key: str):
        item = self.sessions.get(source_key)
        if item is None:
            return None
        messages = list(item["messages"])
        return {
            "id": item["nativeId"],
            "nativeId": item["nativeId"],
            "sourceKey": source_key,
            "source": item["source"],
            "title": item["title"],
            "updatedAt": item.get("updatedAt", 1.0),
            "messageCount": len(messages),
            "messages": messages,
            "runState": item.get("runState") or (
                "completed" if messages and messages[-1].get("role") == "assistant"
                else "generating"
            ),
        }


def assistant(text: str, ts: float) -> dict:
    return {"role": "assistant", "text": text, "ts": ts}


async def assert_responsive_discovery(store) -> None:
    loop = asyncio.get_running_loop()
    event_loop_thread = threading.get_ident()

    async def health(_request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/health", health)
    async with TestClient(TestServer(app)) as client:
        for blocked_method in ("list", "read"):
            entered = asyncio.Event()
            release = threading.Event()

            class BlockingDiscovery(FakeDiscovery):
                blocked_calls = 0

                def block(self, method):
                    if method != blocked_method:
                        return
                    self.blocked_calls += 1
                    loop.call_soon_threadsafe(entered.set)
                    assert threading.get_ident() != event_loop_thread, "Native discovery blocked the HTTP event loop"
                    assert release.wait(3), "Slow-discovery fixture was not released"

                def list_sessions(self):
                    self.block("list")
                    return super().list_sessions()

                def read_session_key(self, source_key):
                    self.block("read")
                    return super().read_session_key(source_key)

            discovery = BlockingDiscovery()
            discovery.sessions["cli:slow-fixture"] = {
                "nativeId": "slow-fixture", "source": "cli",
                "title": "Synthetic slow discovery", "messages": [],
            }
            watcher = SessionWatcher(store, discovery=discovery)
            scans = [asyncio.create_task(watcher.scan_once())]
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                assert not scans[0].done(), "Discovery must remain pending without blocking HTTP"
                scans.append(asyncio.create_task(watcher.scan_once()))
                response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
                assert response.status == 200
                assert await response.json() == {"ok": True}
                assert discovery.blocked_calls == 1, "Overlapping scans must be serialized"
                assert not any(scan.done() for scan in scans)
            finally:
                release.set()
                results = await asyncio.gather(*scans)
            assert all(result.errors == 0 for result in results)
            assert discovery.blocked_calls == 2


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(os.path.join(tmp, "sessions"))
        discovery = FakeDiscovery()
        shared_id = "same-native-id"
        discovery.sessions[f"cli:{shared_id}"] = {
            "nativeId": shared_id,
            "source": "cli",
            "title": "CLI task",
            "messages": [
                {"role": "user", "text": "old CLI question", "ts": 1.0},
                assistant("old CLI answer", 2.0),
            ],
        }
        discovery.sessions[f"vscode-insiders:{shared_id}"] = {
            "nativeId": shared_id,
            "source": "vscode-insiders",
            "title": "Editor task",
            "messages": [
                {"role": "user", "text": "old editor question", "ts": 1.0},
                assistant("old editor answer", 2.0),
            ],
        }
        watcher = SessionWatcher(store, discovery=discovery, settle_scans=1)
        try:
            # First observation establishes a baseline and must not flood the inbox.
            first = await watcher.scan_once()
            assert first.baselined == 2
            assert first.notifications_created == 0
            assert store.list_inbox_items() == []

            discovery.sessions[f"cli:{shared_id}"]["messages"].extend([
                {"role": "user", "text": "new CLI question", "ts": 3.0},
                assistant("new CLI answer", 4.0),
            ])
            second = await watcher.scan_once()
            assert second.notifications_created == 0
            assert (await watcher.scan_once()).notifications_created == 1
            items = store.list_inbox_items()
            assert len(items) == 1
            assert items[0]["source"] == "cli"
            assert items[0]["sourceKey"] == f"cli:{shared_id}"
            assert items[0]["title"] == "CLI task"
            assert items[0]["summary"] == "new CLI answer"
            assert items[0]["status"] == "unread"

            # Re-scanning unchanged data is idempotent.
            assert (await watcher.scan_once()).notifications_created == 0
            assert len(store.list_inbox_items()) == 1

            # A user turn alone is not a completed AI response.
            discovery.sessions[f"vscode-insiders:{shared_id}"]["messages"].append(
                {"role": "user", "text": "waiting for editor", "ts": 3.0}
            )
            assert (await watcher.scan_once()).notifications_created == 0
            assert (await watcher.scan_once()).notifications_created == 0

            discovery.sessions[f"vscode-insiders:{shared_id}"]["messages"].append(
                assistant("new editor answer", 4.0)
            )
            assert (await watcher.scan_once()).notifications_created == 0
            assert (await watcher.scan_once()).notifications_created == 1
            items = store.list_inbox_items()
            assert len(items) == 2
            assert {item["sourceKey"] for item in items} == {
                f"cli:{shared_id}", f"vscode-insiders:{shared_id}",
            }

            unread = store.inbox_unread_count()
            assert unread == 2
            updated = store.update_inbox_item(items[0]["id"], "seen")
            assert updated["status"] == "seen"
            assert store.inbox_unread_count() == 1
            store.mark_all_inbox_seen()
            assert store.inbox_unread_count() == 0
            await assert_responsive_discovery(store)
        finally:
            store.close()
    print("ALL SESSION WATCHER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())