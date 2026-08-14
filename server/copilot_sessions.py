"""Discover and read GitHub Copilot CLI's *native* on-disk sessions.

The Copilot CLI stores every conversation under ``~/.copilot/session-state/<uuid>/``:

* ``workspace.yaml`` - flat metadata (id, name, cwd, repository, created/updated).
* ``events.jsonl``   - the conversation as JSON-lines; ``user.message`` and
                       ``assistant.message`` events carry the actual text in
                       ``data.content``.

These are the sessions the user created with ``copilot`` directly (not only the
ones this bridge made). The Bridge stores native ids as device-scoped external
references. Importing a CLI session creates a separate Bridge conversation id
plus an execution binding to the original ``--session-id``, so the real
conversation can resume without sharing an id namespace with VS Code sessions.

This module also surfaces the **VS Code Copilot Chat** extension's sessions,
which live in a SEPARATE SQLite database
``%APPDATA%/Code[ - Insiders]/User/globalStorage/github.copilot-chat/session-store.db``
(tables ``sessions`` + ``turns``). User-visible pinned titles and file-change
statistics come from VS Code's sibling ``globalStorage/state.vscdb``. CLI and VS
Code source ids are namespaced because the two products can reuse one raw uuid
for distinct entries in the Sessions UI.

This module is read-only: it never modifies anything under ``~/.copilot`` or VS Code.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime

logger = logging.getLogger("copilot_bridge.copilot_sessions")


def copilot_home() -> str:
    """Root of the Copilot CLI state dir (override with the COPILOT_HOME env var)."""
    return os.environ.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")


def _session_state_dir() -> str:
    return os.path.join(copilot_home(), "session-state")


def _safe_id(session_id: str) -> str:
    """Return a path-safe uuid-like id or raise ValueError (no traversal)."""
    if not isinstance(session_id, str):
        raise ValueError("session id must be a string")
    sid = session_id.strip()
    if not sid:
        raise ValueError("session id must be non-empty")
    if "\x00" in sid or ".." in sid or "/" in sid or "\\" in sid:
        raise ValueError(f"unsafe session id: {session_id!r}")
    if os.sep in sid or (os.altsep and os.altsep in sid):
        raise ValueError(f"unsafe session id: {session_id!r}")
    return sid


def _to_epoch(value) -> float | None:
    """Parse an ISO-8601 timestamp (e.g. '2026-06-10T15:26:02.291Z') to epoch secs."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_workspace_yaml(path: str) -> dict:
    """Parse the (mostly flat) ``key: value`` workspace.yaml without a YAML dep.

    Handles block scalars too: ``name: |-`` followed by indented lines means the
    value is those following lines (Copilot stores the first prompt there, which
    can be multi-line Markdown).
    """
    meta: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return meta

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            i += 1
            continue
        # Only treat top-level (unindented) "key: value" lines as keys.
        if line[:1] in (" ", "\t"):
            i += 1
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        val = raw.strip()
        if val and val[0] in "|>":
            # Block scalar: gather the following more-indented lines.
            block: list[str] = []
            i += 1
            while i < len(lines) and (lines[i][:1] in (" ", "\t") or lines[i] == ""):
                block.append(lines[i])
                i += 1
            # Strip the common leading indent of the non-empty block lines.
            indents = [len(b) - len(b.lstrip()) for b in block if b.strip()]
            cut = min(indents) if indents else 0
            meta[key] = "\n".join(b[cut:] if len(b) >= cut else b for b in block).strip("\n")
            continue
        if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
            val = val[1:-1]
        meta[key] = val
        i += 1
    return meta


def _one_line_title(name: str) -> str:
    """Collapse a (possibly multi-line) Copilot session name to a single line."""
    for ln in (name or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln if len(ln) <= 80 else ln[:79] + "\u2026"
    return ""


# ----- VS Code Copilot Chat session store (separate SQLite DB) ---------------

def _vscode_chat_dbs() -> list[str]:
    """Existing VS Code Copilot Chat ``session-store.db`` paths (Insiders + stable),
    plus any os.pathsep-separated path(s) in the COPILOT_CHAT_DB env var."""
    candidates: list[str] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        for prod in ("Code - Insiders", "Code"):
            candidates.append(os.path.join(
                appdata, prod, "User", "globalStorage",
                "github.copilot-chat", "session-store.db"))
    for p in (os.environ.get("COPILOT_CHAT_DB", "") or "").split(os.pathsep):
        if p.strip():
            candidates.append(p.strip())
    seen: set[str] = set()
    out: list[str] = []
    for p in candidates:
        ap = os.path.abspath(p)
        if ap not in seen and os.path.isfile(ap):
            seen.add(ap)
            out.append(ap)
    return out


def _vscode_source(db_path: str) -> str:
    """Stable source id for a VS Code chat database path."""
    normalized = os.path.normcase(os.path.abspath(db_path))
    insiders = os.path.normcase(os.path.join("Code - Insiders", "User"))
    return "vscode-insiders" if insiders in normalized else "vscode"


def _source_key(source: str, native_id: str) -> str:
    return f"{source}:{native_id}"


def _vscode_state_dbs(chat_db_path: str) -> list[str]:
    """VS Code state DBs that may contain the user-visible session title.

    ``session-store.db`` stores transcript rows, while VS Code's workbench keeps
    pinned titles, timing, and file-change statistics in
    ``globalStorage/state.vscdb`` under ``chat.ChatSessionStore.index``.
    """
    candidates: list[str] = []
    for path in (os.environ.get("COPILOT_CHAT_STATE_DB", "") or "").split(os.pathsep):
        if path.strip():
            candidates.append(path.strip())
    global_storage = os.path.dirname(os.path.dirname(os.path.abspath(chat_db_path)))
    candidates.append(os.path.join(global_storage, "state.vscdb"))
    seen: set[str] = set()
    result: list[str] = []
    for path in candidates:
        absolute = os.path.abspath(path)
        if absolute not in seen and os.path.isfile(absolute):
            seen.add(absolute)
            result.append(absolute)
    return result


def _vscode_session_index(chat_db_path: str) -> dict[str, dict]:
    """Read VS Code's display metadata, keyed by its exact session resource id."""
    for state_path in _vscode_state_dbs(chat_db_path):
        try:
            con = _open_ro(state_path)
        except sqlite3.Error:
            continue
        try:
            row = con.execute(
                "select value from ItemTable where key='chat.ChatSessionStore.index'"
            ).fetchone()
            if not row:
                continue
            raw = row[0]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            document = json.loads(raw)
            entries = document.get("entries") if isinstance(document, dict) else None
            if isinstance(entries, dict):
                return {
                    str(key): value for key, value in entries.items()
                    if isinstance(value, dict)
                }
        except (sqlite3.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("vscode session index read failed (%s): %s", state_path, exc)
        finally:
            con.close()
    return {}


def _millis_to_epoch(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000.0
    return None


_VSCODE_RESPONSE_STATES = {
    0: "generating",
    1: "completed",
    2: "cancelled",
    3: "failed",
    4: "needs_input",
}


def _vscode_run_state(display: dict, has_assistant: bool) -> str:
    value = display.get("lastResponseState")
    if isinstance(value, int) and value in _VSCODE_RESPONSE_STATES:
        return _VSCODE_RESPONSE_STATES[value]
    return "completed" if has_assistant else "unknown"


def _open_ro(db_path: str) -> sqlite3.Connection:
    """Open a SQLite DB read-only (safe to read while VS Code writes via WAL)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
    try:
        con.execute("PRAGMA busy_timeout=2000")
    except sqlite3.Error:
        pass
    return con


def _clean_db_title(summary, fallback: str = "") -> str:
    """The chat 'summary' is sometimes the raw first message with <context> /
    <attachments> blocks. Strip tag-like blocks and take the first real line."""
    s = re.sub(r"<[^>]+>", " ", str(summary or ""))
    return _one_line_title(s) or _one_line_title(fallback)


def _db_list_sessions(db_path: str) -> list[dict]:
    """Message-free summaries from a VS Code Copilot Chat session-store.db."""
    out: list[dict] = []
    source = _vscode_source(db_path)
    session_index = _vscode_session_index(db_path)
    try:
        con = _open_ro(db_path)
    except sqlite3.Error:
        return out
    try:
        cur = con.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(sessions)")}
        if "id" not in cols:
            return out
        for (sid, summary, cwd, repo, branch, created, updated) in cur.execute(
            "select id, summary, cwd, repository, branch, created_at, updated_at from sessions"
        ).fetchall():
            display = session_index.get(str(sid), {})
            try:
                counts = cur.execute(
                    "select coalesce(sum(user_message is not null and user_message<>''),0),"
                    " coalesce(sum(assistant_response is not null and assistant_response<>''),0)"
                    " from turns where session_id=?", (sid,)
                ).fetchone()
                prompt_count = int(counts[0] or 0)
                mc = prompt_count + int(counts[1] or 0)
            except sqlite3.Error:
                mc = 0
                prompt_count = 0
            title = _clean_db_title(display.get("title")) or _clean_db_title(summary)
            if (not title) or (not display.get("title") and str(summary or "").lstrip().startswith("<")):
                try:
                    fr = cur.execute(
                        "select user_message from turns where session_id=? and user_message is not null"
                        " and user_message<>'' order by turn_index limit 1", (sid,)).fetchone()
                    if fr:
                        title = _clean_db_title(fr[0]) or title
                except sqlite3.Error:
                    pass
            created_at = _to_epoch(created)
            timing = display.get("timing") if isinstance(display.get("timing"), dict) else {}
            display_created = _millis_to_epoch(timing.get("created"))
            display_updated = _millis_to_epoch(display.get("lastMessageDate"))
            stats = display.get("stats") if isinstance(display.get("stats"), dict) else {}
            out.append({
                "id": sid,
                "nativeId": sid,
                "sourceKey": _source_key(source, sid),
                "title": title,
                "cwd": cwd or "",
                "repository": repo or "",
                "branch": branch or "",
                "createdAt": display_created or created_at,
                "updatedAt": max(
                    value for value in (
                        display_updated, _to_epoch(updated), created_at, display_created, 0.0
                    ) if value is not None
                ),
                "messageCount": mc,
                "promptCount": prompt_count,
                "source": source,
                "stats": stats,
                "runState": _vscode_run_state(display, mc > 0),
            })
    except sqlite3.Error as exc:  # noqa: BLE001 - a locked/odd DB must not break listing
        logger.debug("vscode chat db list failed (%s): %s", db_path, exc)
    finally:
        con.close()
    return out


def _db_read_session(db_path: str, sid: str) -> dict | None:
    """Full transcript of one VS Code Copilot Chat session, or None if absent."""
    source = _vscode_source(db_path)
    display = _vscode_session_index(db_path).get(str(sid), {})
    try:
        con = _open_ro(db_path)
    except sqlite3.Error:
        return None
    try:
        cur = con.cursor()
        srow = cur.execute(
            "select id, summary, cwd, repository, branch, created_at, updated_at"
            " from sessions where id=?", (sid,)).fetchone()
        if not srow:
            return None
        (_id, summary, cwd, repo, branch, created, updated) = srow
        messages: list[dict] = []
        for (_tidx, um, ar, ts) in cur.execute(
            "select turn_index, user_message, assistant_response, timestamp"
            " from turns where session_id=? order by turn_index", (sid,)):
            t = _to_epoch(ts)
            if um and um.strip():
                messages.append({
                    "role": "user", "text": um.strip(), "ts": t,
                    "nativeMessageId": f"turn:{_tidx}:user",
                })
            if ar and ar.strip():
                messages.append({
                    "role": "assistant", "text": ar.strip(), "ts": t,
                    "nativeMessageId": f"turn:{_tidx}:assistant",
                })
        title = _clean_db_title(display.get("title")) or _clean_db_title(summary)
        if (not title) or (not display.get("title") and str(summary or "").lstrip().startswith("<")):
            for m in messages:
                if m["role"] == "user":
                    title = _clean_db_title(m["text"]) or title
                    break
        timing = display.get("timing") if isinstance(display.get("timing"), dict) else {}
        created_at = _to_epoch(created)
        display_created = _millis_to_epoch(timing.get("created"))
        display_updated = _millis_to_epoch(display.get("lastMessageDate"))
        stats = display.get("stats") if isinstance(display.get("stats"), dict) else {}
        return {
            "id": _id,
            "nativeId": _id,
            "sourceKey": _source_key(source, _id),
            "title": title,
            "cwd": cwd or "",
            "repository": repo or "",
            "branch": branch or "",
            "createdAt": display_created or created_at,
            "updatedAt": max(
                value for value in (
                    display_updated, _to_epoch(updated), created_at, display_created, 0.0
                ) if value is not None
            ),
            "messageCount": len(messages),
            "promptCount": sum(message["role"] == "user" for message in messages),
            "messages": messages,
            "source": source,
            "stats": stats,
            "runState": _vscode_run_state(
                display, bool(messages and messages[-1].get("role") == "assistant")
            ),
        }
    except sqlite3.Error as exc:  # noqa: BLE001
        logger.debug("vscode chat db read failed (%s): %s", db_path, exc)
        return None
    finally:
        con.close()



def _count_activity(events_path: str) -> tuple[int, int]:
    """Return ``(message_count, prompt_count)`` for a CLI event stream."""
    message_count = 0
    prompt_count = 0
    try:
        with open(events_path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    event_type = json.loads(line).get("type")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if event_type == "user.message":
                    message_count += 1
                    prompt_count += 1
                elif event_type == "assistant.message":
                    message_count += 1
    except OSError:
        return 0, 0
    return message_count, prompt_count


def _summary_from_dir(entry_path: str, sid: str) -> dict | None:
    """Build a message-free summary for one session-state directory."""
    ws_path = os.path.join(entry_path, "workspace.yaml")
    if not os.path.isfile(ws_path):
        return None
    meta = _parse_workspace_yaml(ws_path)
    events_path = os.path.join(entry_path, "events.jsonl")
    created = _to_epoch(meta.get("created_at"))
    updated = _to_epoch(meta.get("updated_at")) or created
    try:
        file_updated = os.path.getmtime(events_path if os.path.isfile(events_path) else ws_path)
        updated = max(updated or 0.0, file_updated)
    except OSError:
        updated = updated or 0.0
    message_count, prompt_count = _count_activity(events_path)
    return {
        "id": meta.get("id") or sid,
        "nativeId": meta.get("id") or sid,
        "sourceKey": _source_key("cli", meta.get("id") or sid),
        "title": _one_line_title(meta.get("name") or ""),
        "cwd": meta.get("cwd") or "",
        "repository": meta.get("repository") or "",
        "branch": meta.get("branch") or "",
        "createdAt": created,
        "updatedAt": updated,
        "messageCount": message_count,
        "promptCount": prompt_count,
        "source": "cli",
    }


def list_sessions(limit: int = 200) -> list[dict]:
    """Return Copilot session summaries (CLI + VS Code chat), newest first.

    Source ids are intentionally namespaced. A VS Code session and a CLI session
    may share the same raw uuid while representing separate entries in VS Code's
    Sessions UI, so neither source is discarded. No messages are included.
    """
    out: list[dict] = []
    root = _session_state_dir()
    if os.path.isdir(root):
        try:
            entries = os.listdir(root)
        except OSError:
            entries = []
        for name in entries:
            entry_path = os.path.join(root, name)
            if not os.path.isdir(entry_path):
                continue
            try:
                summary = _summary_from_dir(entry_path, name)
            except Exception as exc:  # noqa: BLE001 - never let one bad dir break the list
                logger.debug("skip copilot session %s: %s", name, exc)
                summary = None
            if summary is not None:
                out.append(summary)
    for db in _vscode_chat_dbs():
        try:
            for s in _db_list_sessions(db):
                out.append(s)
        except Exception as exc:  # noqa: BLE001
            logger.debug("skip vscode chat db %s: %s", db, exc)
    out.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
    return out[:limit]


def _read_state_session(sid: str) -> dict | None:
    """Read a CLI session-state transcript (events.jsonl), or None if absent.

    The transcript is the ordered list of ``{role, text, ts}`` reconstructed from
    the ``user.message`` / ``assistant.message`` events (clean ``content``, not the
    system-augmented ``transformedContent``). Tool/system events are skipped.
    """
    entry_path = os.path.join(_session_state_dir(), sid)
    ws_path = os.path.join(entry_path, "workspace.yaml")
    events_path = os.path.join(entry_path, "events.jsonl")
    if not os.path.isfile(ws_path):
        return None

    meta = _parse_workspace_yaml(ws_path)
    messages: list[dict] = []
    active_turns: set[str] = set()
    pending_permissions: set[str] = set()
    last_assistant_message_id = ""
    try:
        with open(events_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                data = ev.get("data") or {}
                ts = _to_epoch(ev.get("timestamp"))
                if etype == "assistant.turn_start":
                    turn_id = str(data.get("turnId") or "")
                    if turn_id:
                        active_turns.add(turn_id)
                elif etype == "assistant.turn_end":
                    active_turns.discard(str(data.get("turnId") or ""))
                elif etype == "permission.requested":
                    request_id = str(data.get("requestId") or "")
                    if request_id:
                        pending_permissions.add(request_id)
                elif etype == "permission.completed":
                    pending_permissions.discard(str(data.get("requestId") or ""))
                elif etype == "session.shutdown":
                    active_turns.clear()
                if etype == "user.message":
                    text = (data.get("content") or "").strip()
                    if text:
                        messages.append({
                            "role": "user", "text": text, "ts": ts,
                            "nativeMessageId": str(data.get("messageId") or ""),
                        })
                elif etype == "assistant.message":
                    text = (data.get("content") or "").strip()
                    if text:
                        last_assistant_message_id = str(data.get("messageId") or "")
                        messages.append({
                            "role": "assistant", "text": text, "ts": ts,
                            "nativeMessageId": last_assistant_message_id,
                            "nativeTurnId": str(data.get("turnId") or ""),
                        })
    except OSError:
        return None

    created = _to_epoch(meta.get("created_at"))
    updated = _to_epoch(meta.get("updated_at")) or created
    try:
        updated = max(updated or 0.0, os.path.getmtime(events_path))
    except OSError:
        pass
    if pending_permissions:
        run_state = "needs_input"
    elif active_turns:
        run_state = "generating"
    elif messages and messages[-1].get("role") == "user":
        run_state = "generating"
    elif messages:
        run_state = "completed"
    else:
        run_state = "unknown"
    return {
        "id": meta.get("id") or sid,
        "nativeId": meta.get("id") or sid,
        "sourceKey": _source_key("cli", meta.get("id") or sid),
        "title": _one_line_title(meta.get("name") or ""),
        "cwd": meta.get("cwd") or "",
        "repository": meta.get("repository") or "",
        "branch": meta.get("branch") or "",
        "createdAt": created,
        "updatedAt": updated,
        "messageCount": len(messages),
        "promptCount": sum(message["role"] == "user" for message in messages),
        "messages": messages,
        "source": "cli",
        "runState": run_state,
        "completionFingerprint": last_assistant_message_id,
    }


def read_session(session_id: str, source: str = "") -> dict | None:
    """Read a native transcript, optionally constrained to one source."""
    sid = _safe_id(session_id)
    if not source or source == "cli":
        data = _read_state_session(sid)
        if data is not None:
            return data
        if source == "cli":
            return None
    for db in _vscode_chat_dbs():
        if source and _vscode_source(db) != source:
            continue
        data = _db_read_session(db, sid)
        if data is not None:
            return data
    return None


def read_session_key(source_key: str) -> dict | None:
    """Read ``<source>:<native-id>``; raw ids retain legacy lookup behavior."""
    if not isinstance(source_key, str):
        raise ValueError("session key must be a string")
    for source in ("vscode-insiders", "vscode", "cli"):
        prefix = source + ":"
        if source_key.startswith(prefix):
            return read_session(source_key[len(prefix):], source=source)
    return read_session(source_key)
