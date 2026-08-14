"""Tests persistent inbox notifications from native AI session changes.

Run:  python tests/session_watcher_test.py   (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile

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
        finally:
            store.close()
    print("ALL SESSION WATCHER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())