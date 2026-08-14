"""Tests source-qualified CLI and VS Code session discovery.

Run:  python tests/native_session_discovery_test.py   (from the server/ directory)
"""

import json
import os
import sqlite3
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import copilot_sessions


def create_vscode_store(root: str, session_id: str) -> tuple[str, str]:
    global_storage = os.path.join(
        root, "Code - Insiders", "User", "globalStorage"
    )
    chat_dir = os.path.join(global_storage, "github.copilot-chat")
    os.makedirs(chat_dir, exist_ok=True)
    chat_db = os.path.join(chat_dir, "session-store.db")
    con = sqlite3.connect(chat_db)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, summary TEXT, cwd TEXT, repository TEXT,
            branch TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY, session_id TEXT, turn_index INTEGER,
            user_message TEXT, assistant_response TEXT, timestamp TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id, "stale first prompt", "C:/repo", "owner/repo", "main",
            "2026-08-08T01:00:00Z", "2026-08-09T02:00:00Z",
        ),
    )
    con.execute(
        "INSERT INTO turns(session_id, turn_index, user_message, assistant_response, timestamp) "
        "VALUES (?, 0, 'VS Code question', 'VS Code answer', '2026-08-09T02:00:00Z')",
        (session_id,),
    )
    con.commit()
    con.close()

    state_db = os.path.join(global_storage, "state.vscdb")
    con = sqlite3.connect(state_db)
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    value = json.dumps({
        "version": 1,
        "entries": {
            session_id: {
                "sessionId": session_id,
                "title": "Copilot Bridge feature design",
                "lastMessageDate": 1786247213904,
                "timing": {"created": 1786169290003},
                "stats": {"fileCount": 25, "added": 2135, "removed": 234},
            },
            f"copilotcli:/{session_id}": {
                "sessionId": f"copilotcli:/{session_id}",
                "title": "External CLI view",
                "lastMessageDate": 1786247000000,
            },
        },
    })
    con.execute(
        "INSERT INTO ItemTable(key, value) VALUES ('chat.ChatSessionStore.index', ?)",
        (value,),
    )
    con.commit()
    con.close()
    return chat_db, state_db


def create_cli_store(root: str, session_id: str) -> None:
    session_dir = os.path.join(root, "session-state", session_id)
    os.makedirs(session_dir, exist_ok=True)
    with open(os.path.join(session_dir, "workspace.yaml"), "w", encoding="utf-8") as fh:
        fh.write(
            f"id: {session_id}\n"
            "name: Old CLI title\n"
            "cwd: C:/cli\n"
            "created_at: 2026-08-08T00:00:00Z\n"
            "updated_at: 2026-08-08T01:00:00Z\n"
        )
    with open(os.path.join(session_dir, "events.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "user.message", "timestamp": "2026-08-08T00:00:00Z",
            "data": {"content": "CLI question"},
        }) + "\n")
        fh.write(json.dumps({
            "type": "assistant.message", "timestamp": "2026-08-08T01:00:00Z",
            "data": {"content": "CLI answer"},
        }) + "\n")


def main() -> None:
    old_env = {key: os.environ.get(key) for key in (
        "APPDATA", "COPILOT_HOME", "COPILOT_CHAT_DB", "COPILOT_CHAT_STATE_DB"
    )}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            session_id = "shared-session-id"
            appdata = os.path.join(tmp, "appdata")
            copilot_home = os.path.join(tmp, "copilot")
            create_vscode_store(appdata, session_id)
            create_cli_store(copilot_home, session_id)
            os.environ["APPDATA"] = appdata
            os.environ["COPILOT_HOME"] = copilot_home
            os.environ.pop("COPILOT_CHAT_DB", None)
            os.environ.pop("COPILOT_CHAT_STATE_DB", None)

            sessions = copilot_sessions.list_sessions()
            matching = [item for item in sessions if item.get("nativeId") == session_id]
            assert len(matching) == 2, matching
            assert {item["sourceKey"] for item in matching} == {
                f"cli:{session_id}", f"vscode-insiders:{session_id}",
            }
            vscode = next(item for item in matching if item["source"] == "vscode-insiders")
            assert vscode["title"] == "Copilot Bridge feature design"
            assert vscode["messageCount"] == 2
            assert vscode["promptCount"] == 1
            assert vscode["stats"] == {"fileCount": 25, "added": 2135, "removed": 234}

            cli = next(item for item in matching if item["source"] == "cli")
            assert cli["messageCount"] == 2
            assert cli["promptCount"] == 1

            cli_detail = copilot_sessions.read_session_key(f"cli:{session_id}")
            vscode_detail = copilot_sessions.read_session_key(
                f"vscode-insiders:{session_id}"
            )
            assert cli_detail["messages"][0]["text"] == "CLI question"
            assert cli_detail["promptCount"] == 1
            assert vscode_detail["messages"][0]["text"] == "VS Code question"
            assert vscode_detail["promptCount"] == 1
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("ALL NATIVE SESSION DISCOVERY TESTS PASSED")


if __name__ == "__main__":
    main()