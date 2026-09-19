"""SQLite-backed conversation, timeline, and sync-outbox storage.

The public methods retain the original session-dict API so existing clients stay
compatible. Internally, Bridge conversation ids, native provider session ids,
branches, turns, and messages are separate records. Legacy ``<id>.json`` files
are imported transactionally on startup and retained as rollback backups.

Projected session shape (timestamps are epoch seconds, ``time.time()``)::

    {
      "id": "<uuid4>",            # == conversationId == copilot --session-id
      "title": "",               # derived from the first user message
      "createdAt": <ts>,
      "updatedAt": <ts>,
      "messageCount": <int>,
      "messages": [
        {"id": "<uuid>", "turnId": "<uuid>",
         "role": "user"|"assistant", "text": "...", "ts": <ts>,
         "ok": <bool optional>, "exitCode": <int optional>}
      ]
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger("copilot_bridge.sessions")

# Titles are a single trimmed line capped at ~60 characters.
_TITLE_MAX = 60


class SyncDependencyError(ValueError):
    """A valid remote event depends on another package not applied yet."""


def _now() -> float:
    return time.time()


def _title_from_text(text: str) -> str:
    """Derive a single-line, whitespace-collapsed title (<= ~60 chars)."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= _TITLE_MAX:
        return collapsed
    return collapsed[: _TITLE_MAX - 1].rstrip() + "\u2026"


def _title_from_attachments(attachments) -> str:
    """Fallback title for an image-only turn (no text), e.g. the file name or count."""
    if not attachments:
        return ""
    if len(attachments) == 1:
        name = (attachments[0] or {}).get("name") or "image"
        return _title_from_text(f"\U0001f5bc {name}")
    return f"\U0001f5bc {len(attachments)} images"


def _norm_text(text: str) -> str:
    """Whitespace-collapsed text used to tell whether two turns are the same."""
    return " ".join((text or "").split())


def _normalize_native_ts(native_msgs: list) -> list:
    """Return native messages with monotonic non-decreasing timestamps.

    events.jsonl timestamps can be missing or out of order; carrying the last
    known value forward keeps the two-pointer merge ordering sane.
    """
    out: list = []
    last = 0.0
    for m in native_msgs or []:
        ts = m.get("ts")
        if not isinstance(ts, (int, float)) or ts < last:
            ts = last
        else:
            last = ts
        out.append({"role": m.get("role") or "user", "text": m.get("text") or "", "ts": ts})
    return out


def merge_message_lists(bridge_msgs: list, native_msgs: list) -> tuple[list, bool]:
    """Merge a Copilot CLI native transcript into the bridge transcript.

    Both lists are chronological. A two-pointer merge keeps every turn exactly
    once: when the heads are the *same* turn (same role + whitespace-collapsed
    text) the bridge copy is kept (it carries attachments / ok / exitCode); a turn
    that exists in only one side is emitted in timestamp order. Nothing is ever
    dropped, so a divergence between the two stores becomes a clean union.

    Returns ``(merged, changed)`` where ``changed`` is False when the merge equals
    the current bridge transcript (so the caller can skip a needless write).
    """
    bridge = list(bridge_msgs or [])
    native = _normalize_native_ts(native_msgs)

    def same(a: dict, b: dict) -> bool:
        return a.get("role") == b.get("role") and _norm_text(a.get("text")) == _norm_text(b.get("text"))

    def ts(m: dict) -> float:
        v = m.get("ts")
        return v if isinstance(v, (int, float)) else 0.0

    merged: list = []
    i = j = 0
    while i < len(bridge) and j < len(native):
        if same(bridge[i], native[j]):
            merged.append(bridge[i]); i += 1; j += 1
        elif ts(bridge[i]) <= ts(native[j]):
            merged.append(bridge[i]); i += 1
        else:
            merged.append(native[j]); j += 1
    merged.extend(bridge[i:])
    merged.extend(native[j:])

    def seq(lst: list) -> list:
        return [(m.get("role"), _norm_text(m.get("text"))) for m in lst]

    return merged, seq(merged) != seq(bridge)



class SessionStore:
    """Thread-safe SQLite store with the legacy session-dict API.

    SQLite is the local source of truth. Existing one-file-per-session JSON data
    is imported on startup and left untouched as a rollback backup. Every local
    mutation writes its sync event in the same transaction as the domain row.
    """

    DATABASE_NAME = "copilotbridge.db"
    DEFAULT_PROMPT_PREVIEW_LENGTH = 200
    MIN_PROMPT_PREVIEW_LENGTH = 20
    MAX_PROMPT_PREVIEW_LENGTH = 1000
    KNOWLEDGE_TYPES = {
        "summary", "fact", "decision", "procedure", "solution", "failure",
        "code_pattern", "todo", "question",
    }
    KNOWLEDGE_STATUSES = {"draft", "verified", "conflicted", "superseded"}
    KNOWLEDGE_GENERATION_STATUSES = {"queued", "running", "completed", "failed"}
    KNOWLEDGE_GENERATION_PHASES = {
        "queued", "extracting", "mapping", "completed", "failed",
    }

    def __init__(self, sessions_dir: str, *, device_name: str = ""):
        self.dir = os.path.abspath(sessions_dir)
        os.makedirs(self.dir, exist_ok=True)
        self.database_path = os.path.join(self.dir, self.DATABASE_NAME)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.database_path, timeout=5.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._configure_database()
        self._create_schema()
        self.device_id = self._get_or_create_meta("device_id", lambda: str(uuid.uuid4()))
        self.device_name = self._normalize_device_name(device_name)
        self._register_local_device()
        self._migrate_legacy_json()
        logger.info("Loaded %d session(s) from %s", len(self.list()), self.database_path)

    # -- database lifecycle ---------------------------------------------

    def _configure_database(self) -> None:
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                is_favorite INTEGER NOT NULL DEFAULT 0 CHECK (is_favorite IN (0, 1)),
                is_pinned INTEGER NOT NULL DEFAULT 0 CHECK (is_pinned IN (0, 1)),
                project TEXT NOT NULL DEFAULT '',
                labels_json TEXT NOT NULL DEFAULT '[]',
                organization_updated_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL
            );

            CREATE TABLE IF NOT EXISTS branches (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                parent_branch_id TEXT REFERENCES branches(id),
                forked_from_message_id TEXT,
                origin_device_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                is_main INTEGER NOT NULL DEFAULT 0 CHECK (is_main IN (0, 1))
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_branches_one_main
                ON branches(conversation_id) WHERE is_main = 1;

            CREATE TABLE IF NOT EXISTS turns (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                user_message_id TEXT,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(branch_id, ordinal)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                ordinal INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                ts REAL NOT NULL,
                ok INTEGER,
                exit_code INTEGER,
                attachments_json TEXT,
                UNIQUE(branch_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS ix_messages_turn ON messages(turn_id, ordinal);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                device_seq INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                published_at REAL,
                package_id TEXT,
                UNIQUE(device_id, device_seq)
            );
            CREATE INDEX IF NOT EXISTS ix_sync_events_pending
                ON sync_events(device_id, published_at, device_seq);

            CREATE TABLE IF NOT EXISTS applied_events (
                event_id TEXT PRIMARY KEY,
                source_device_id TEXT NOT NULL,
                applied_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_cursors (
                source_device_id TEXT PRIMARY KEY,
                last_device_seq INTEGER NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS legacy_json_imports (
                path TEXT PRIMARY KEY,
                imported_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_refs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                branch_id TEXT REFERENCES branches(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL,
                adapter_id TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                capabilities_json TEXT,
                created_at REAL NOT NULL,
                UNIQUE(device_id, adapter_id, native_session_id)
            );

            CREATE TABLE IF NOT EXISTS execution_bindings (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(device_id, provider, native_session_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_execution_binding_active
                ON execution_bindings(device_id, branch_id, provider) WHERE state='active';

            CREATE TABLE IF NOT EXISTS native_watch_cursors (
                source_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                native_session_id TEXT NOT NULL,
                observed_message_count INTEGER NOT NULL DEFAULT 0,
                notified_message_count INTEGER NOT NULL DEFAULT 0,
                last_assistant_fingerprint TEXT,
                notified_assistant_fingerprint TEXT,
                last_updated_at REAL,
                stable_scans INTEGER NOT NULL DEFAULT 0,
                scanned_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inbox_items (
                id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL,
                source_key TEXT NOT NULL,
                native_session_id TEXT,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unread'
                    CHECK (status IN ('unread', 'seen', 'completed', 'ignored')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                seen_at REAL,
                remind_at REAL,
                metadata_json TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_inbox_items_status_created
                ON inbox_items(status, created_at DESC);

            CREATE TABLE IF NOT EXISTS knowledge_items (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'verified', 'conflicted', 'superseded')),
                project TEXT NOT NULL DEFAULT '',
                labels_json TEXT NOT NULL DEFAULT '[]',
                source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
                current_version_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                deleted_at REAL
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_items_updated
                ON knowledge_items(status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS knowledge_versions (
                id TEXT PRIMARY KEY,
                knowledge_id TEXT NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
                version_number INTEGER NOT NULL,
                body_markdown TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                extractor_version TEXT NOT NULL DEFAULT 'manual-v1',
                confidence TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(knowledge_id, version_number),
                UNIQUE(knowledge_id, input_digest, extractor_version)
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_versions_item
                ON knowledge_versions(knowledge_id, version_number DESC);

            CREATE TABLE IF NOT EXISTS knowledge_evidence (
                id TEXT PRIMARY KEY,
                knowledge_id TEXT NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
                version_id TEXT NOT NULL REFERENCES knowledge_versions(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                branch_id TEXT NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
                turn_id TEXT REFERENCES turns(id) ON DELETE SET NULL,
                message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                snippet TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(version_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_evidence_message
                ON knowledge_evidence(message_id, knowledge_id);

            CREATE TABLE IF NOT EXISTS knowledge_extraction_runs (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                extractor_version TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                source_message_ids_json TEXT NOT NULL,
                result_item_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                UNIQUE(conversation_id, extractor_version, input_digest)
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_extraction_runs_conversation
                ON knowledge_extraction_runs(conversation_id, extractor_version, created_at);
            CREATE TABLE IF NOT EXISTS knowledge_maps (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                map_json TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                source_message_ids_json TEXT NOT NULL DEFAULT '[]',
                source_conversation_count INTEGER NOT NULL DEFAULT 0,
                source_message_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_generation_jobs (
                conversation_id TEXT PRIMARY KEY
                    REFERENCES conversations(id) ON DELETE CASCADE,
                status TEXT NOT NULL
                    CHECK (status IN ('queued', 'running', 'completed', 'failed')),
                phase TEXT NOT NULL
                    CHECK (phase IN ('queued', 'extracting', 'mapping', 'completed', 'failed')),
                processed_message_count INTEGER NOT NULL DEFAULT 0,
                remaining_message_count INTEGER NOT NULL DEFAULT 0,
                generated_item_count INTEGER NOT NULL DEFAULT 0,
                max_conversations INTEGER NOT NULL DEFAULT 48,
                result_map_id TEXT,
                error TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );
            CREATE INDEX IF NOT EXISTS ix_knowledge_generation_jobs_status
                ON knowledge_generation_jobs(status, updated_at DESC);

            PRAGMA user_version=6;
            """
        )
        watch_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(native_watch_cursors)"
            ).fetchall()
        }
        if "notified_assistant_fingerprint" not in watch_columns:
            self._conn.execute(
                "ALTER TABLE native_watch_cursors "
                "ADD COLUMN notified_assistant_fingerprint TEXT"
            )
        conversation_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(conversations)"
            ).fetchall()
        }
        for column, definition in (
            ("is_favorite", "INTEGER NOT NULL DEFAULT 0"),
            ("is_pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("project", "TEXT NOT NULL DEFAULT ''"),
            ("labels_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("organization_updated_at", "REAL NOT NULL DEFAULT 0"),
        ):
            if column not in conversation_columns:
                self._conn.execute(
                    f"ALTER TABLE conversations ADD COLUMN {column} {definition}"
                )
        inbox_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(inbox_items)"
            ).fetchall()
        }
        if "remind_at" not in inbox_columns:
            self._conn.execute("ALTER TABLE inbox_items ADD COLUMN remind_at REAL")
        interrupted_at = _now()
        self._conn.execute(
            "UPDATE knowledge_generation_jobs SET status='failed', phase='failed', "
            "error='Generation interrupted by server restart.', updated_at=?, "
            "completed_at=? WHERE status IN ('queued', 'running')",
            (interrupted_at, interrupted_at),
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- ids / metadata --------------------------------------------------

    @staticmethod
    def _norm_id(session_id) -> str:
        if not isinstance(session_id, str):
            raise ValueError("session id must be a string")
        sid = session_id.strip()
        if not sid:
            raise ValueError("session id must be non-empty")
        if "\x00" in sid or ".." in sid or "/" in sid or "\\" in sid:
            raise ValueError(f"unsafe session id: {session_id!r}")
        return sid

    def _get_or_create_meta(self, key: str, factory) -> str:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row:
                return row["value"]
            value = str(factory())
            self._conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (key, value))
            return value

    def _normalize_device_name(self, value: str = "") -> str:
        candidate = (
            value
            or os.environ.get("COPILOTBRIDGE_DEVICE_NAME", "")
            or os.environ.get("COMPUTERNAME", "")
            or platform.node()
            or f"Device {self.device_id[:8]}"
        )
        return " ".join(str(candidate).split())[:80]

    def _register_local_device(self) -> None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT name FROM devices WHERE id=?", (self.device_id,)
            ).fetchone()
            if row is not None and row["name"] == self.device_name:
                return
            updated = _now()
            self._conn.execute(
                "INSERT INTO devices(id, name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (self.device_id, self.device_name, updated),
            )
            self._record_event("device", self.device_id, "updated", {
                "id": self.device_id, "name": self.device_name, "updatedAt": updated,
            })

    def _device_name(self, device_id: str) -> str:
        row = self._conn.execute(
            "SELECT name FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        return row["name"] if row else f"Device {str(device_id)[:8]}"

    def list_devices(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, updated_at FROM devices "
                "ORDER BY (id=?) DESC, name COLLATE NOCASE, id",
                (self.device_id,),
            ).fetchall()
            return [{
                "id": row["id"], "name": row["name"],
                "updatedAt": row["updated_at"], "isLocal": row["id"] == self.device_id,
            } for row in rows]

    def _main_branch_id(self, conversation_id: str) -> str:
        row = self._conn.execute(
            "SELECT id FROM branches WHERE conversation_id=? AND is_main=1",
            (conversation_id,),
        ).fetchone()
        if not row:
            raise RuntimeError(f"conversation {conversation_id!r} has no main branch")
        return row["id"]

    def _next_device_seq(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key='next_device_seq'"
        ).fetchone()
        seq = int(row["value"]) if row else 1
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES ('next_device_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(seq + 1),),
        )
        return seq

    def _record_event(self, entity_type: str, entity_id: str, operation: str,
                      payload: dict) -> str:
        event_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO sync_events(event_id, device_id, device_seq, entity_type, "
            "entity_id, operation, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id, self.device_id, self._next_device_seq(), entity_type,
                entity_id, operation, json.dumps(payload, ensure_ascii=False), _now(),
            ),
        )
        return event_id

    # -- legacy migration ------------------------------------------------

    @staticmethod
    def _stable_legacy_id(kind: str, session_id: str, index: int, value: str = "") -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"copilotbridge:legacy:{kind}:{session_id}:{index}:{value}",
        ))

    def _migrate_legacy_json(self) -> None:
        try:
            names = os.listdir(self.dir)
        except OSError as exc:
            logger.warning("Cannot list legacy sessions dir %s: %s", self.dir, exc)
            return
        imported = 0
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.abspath(os.path.join(self.dir, name))
            if self._conn.execute(
                "SELECT 1 FROM legacy_json_imports WHERE path=?", (path,)
            ).fetchone():
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                sid = self._norm_id(data.get("id") if isinstance(data, dict) else None)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable legacy session %s: %s", name, exc)
                continue
            with self._lock, self._conn:
                exists = self._conn.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (sid,)
                ).fetchone()
                if not exists:
                    created = data.get("createdAt") or _now()
                    updated = data.get("updatedAt") or created
                    branch_id = self._stable_legacy_id("branch", sid, 0)
                    self._conn.execute(
                        "INSERT INTO conversations(id, title, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (sid, data.get("title") or "", created, updated),
                    )
                    self._conn.execute(
                        "INSERT INTO branches(id, conversation_id, origin_device_id, "
                        "created_at, is_main) VALUES (?, ?, ?, ?, 1)",
                        (branch_id, sid, self.device_id, created),
                    )
                    inserted_messages = self._insert_transcript(
                        sid, branch_id, data.get("messages") or [], legacy=True
                    )
                    native_id = sid
                    external_ref_id = self._stable_legacy_id("external-ref", sid, 0)
                    self._conn.execute(
                        "INSERT OR IGNORE INTO external_refs(id, conversation_id, branch_id, "
                        "device_id, adapter_id, native_session_id, capabilities_json, created_at) "
                        "VALUES (?, ?, ?, ?, 'copilot-cli', ?, ?, ?)",
                        (
                            external_ref_id, sid, branch_id,
                            self.device_id, native_id,
                            json.dumps({"canResume": True}), created,
                        ),
                    )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO execution_bindings(id, conversation_id, branch_id, "
                        "device_id, provider, native_session_id, state, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 'copilot', ?, 'active', ?, ?)",
                        (
                            self._stable_legacy_id("binding", sid, 0), sid, branch_id,
                            self.device_id, native_id, created, updated,
                        ),
                    )
                    self._record_event("conversation", sid, "created", {
                        "id": sid, "branchId": branch_id,
                        "title": data.get("title") or "",
                        "createdAt": created, "updatedAt": updated,
                        "originDeviceId": self.device_id,
                    })
                    for message in inserted_messages:
                        payload = dict(message)
                        if message.get("turnId"):
                            turn = self._conn.execute(
                                "SELECT ordinal FROM turns WHERE id=?", (message["turnId"],)
                            ).fetchone()
                            payload["turnOrdinal"] = turn["ordinal"] if turn else None
                        self._record_event("message", message["id"], "created", payload)
                    self._record_event("external_ref", external_ref_id, "created", {
                        "id": external_ref_id, "conversationId": sid,
                        "branchId": branch_id, "deviceId": self.device_id,
                        "adapterId": "copilot-cli", "nativeSessionId": native_id,
                        "capabilities": {"canResume": True}, "createdAt": created,
                    })
                    imported += 1
                self._conn.execute(
                    "INSERT INTO legacy_json_imports(path, imported_at) VALUES (?, ?)",
                    (path, _now()),
                )
        if imported:
            logger.info("Migrated %d legacy JSON session(s) into SQLite", imported)

    # -- row projections -------------------------------------------------

    @staticmethod
    def summarize(session: dict) -> dict:
        summary = {
            "id": session["id"],
            "title": session.get("title", ""),
            "createdAt": session.get("createdAt"),
            "updatedAt": session.get("updatedAt"),
            "messageCount": session.get("messageCount", 0),
            "promptCount": session.get("promptCount", 0),
            "isFavorite": bool(session.get("isFavorite")),
            "isPinned": bool(session.get("isPinned")),
            "project": session.get("project", ""),
            "labels": list(session.get("labels") or []),
            "awaitingResponse": bool(session.get("awaitingResponse")),
        }
        for key in ("machineId", "machineName"):
            if session.get(key):
                summary[key] = session[key]
        return summary

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict:
        item = {
            "id": row["id"],
            "conversationId": row["conversation_id"],
            "role": row["role"],
            "text": row["text"],
            "ts": row["ts"],
            "turnId": row["turn_id"],
            "branchId": row["branch_id"],
            "ordinal": row["ordinal"],
        }
        if row["ok"] is not None:
            item["ok"] = bool(row["ok"])
        if row["exit_code"] is not None:
            item["exitCode"] = row["exit_code"]
        if row["attachments_json"]:
            try:
                item["attachments"] = json.loads(row["attachments_json"])
            except json.JSONDecodeError:
                item["attachments"] = []
        return item

    def _project_session(self, row: sqlite3.Row) -> dict:
        branch_id = self._main_branch_id(row["id"])
        branch = self._conn.execute(
            "SELECT origin_device_id FROM branches WHERE id=?", (branch_id,)
        ).fetchone()
        machine_id = branch["origin_device_id"]
        message_rows = self._conn.execute(
            "SELECT * FROM messages WHERE branch_id=? ORDER BY ordinal", (branch_id,)
        ).fetchall()
        messages = [self._message_dict(message) for message in message_rows]
        try:
            labels = json.loads(row["labels_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            labels = []
        return {
            "id": row["id"],
            "title": row["title"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "messageCount": len(messages),
            "promptCount": sum(message["role"] == "user" for message in messages),
            "isFavorite": bool(row["is_favorite"]),
            "isPinned": bool(row["is_pinned"]),
            "project": row["project"],
            "labels": labels if isinstance(labels, list) else [],
            "awaitingResponse": bool(messages and messages[-1]["role"] == "user"),
            "branchId": branch_id,
            "machineId": machine_id,
            "machineName": self._device_name(machine_id),
            "messages": messages,
        }

    # -- transcript writes ----------------------------------------------

    def _insert_transcript(self, conversation_id: str, branch_id: str, messages: list,
                           legacy: bool = False) -> list[dict]:
        turn_id = None
        turn_ordinal = 0
        inserted: list[dict] = []
        for index, source in enumerate(messages or []):
            role = source.get("role") or "user"
            text = source.get("text") or ""
            ts = source.get("ts") or _now()
            supplied_id = source.get("id")
            if supplied_id:
                message_id = str(supplied_id)
            elif legacy:
                message_id = self._stable_legacy_id("message", conversation_id, index, role)
            else:
                message_id = str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"copilotbridge:message:{conversation_id}:{index}:{role}:{ts}:{_norm_text(text)}",
                ))
            if role == "user":
                turn_ordinal += 1
                turn_id = source.get("turnId") or (
                    self._stable_legacy_id("turn", conversation_id, turn_ordinal)
                    if legacy else str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"copilotbridge:turn:{conversation_id}:{message_id}",
                    ))
                )
                self._conn.execute(
                    "INSERT INTO turns(id, conversation_id, branch_id, ordinal, "
                    "user_message_id, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                    (turn_id, conversation_id, branch_id, turn_ordinal, message_id, ts),
                )
            attachments = source.get("attachments")
            self._conn.execute(
                "INSERT INTO messages(id, conversation_id, branch_id, turn_id, ordinal, "
                "role, text, ts, ok, exit_code, attachments_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id, conversation_id, branch_id, turn_id, index + 1,
                    role, text, ts,
                    None if source.get("ok") is None else int(bool(source.get("ok"))),
                    source.get("exitCode"),
                    json.dumps(attachments, ensure_ascii=False) if attachments else None,
                ),
            )
            if role == "assistant" and turn_id:
                status = "completed" if source.get("ok") is not False else "failed"
                self._conn.execute("UPDATE turns SET status=? WHERE id=?", (status, turn_id))
            row = self._conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            inserted.append(self._message_dict(row))
        return inserted

    # -- legacy-compatible public API -----------------------------------

    def create(self, title: str = "", session_id: str = "") -> dict:
        sid = self._norm_id(session_id) if (session_id and session_id.strip()) else str(uuid.uuid4())
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM conversations WHERE id=?", (sid,)).fetchone():
                raise ValueError(f"session already exists: {sid}")
            now = _now()
            branch_id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, title or "", now, now),
            )
            self._conn.execute(
                "INSERT INTO branches(id, conversation_id, origin_device_id, created_at, is_main) "
                "VALUES (?, ?, ?, ?, 1)",
                (branch_id, sid, self.device_id, now),
            )
            self._record_event("conversation", sid, "created", {
                "id": sid, "branchId": branch_id, "title": title or "",
                "createdAt": now, "updatedAt": now, "originDeviceId": self.device_id,
            })
        return self.get(sid)

    def get(self, session_id: str) -> dict | None:
        try:
            sid = self._norm_id(session_id)
        except ValueError:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL", (sid,)
            ).fetchone()
            return self._project_session(row) if row else None

    def get_or_create(self, session_id: str, title: str = "") -> dict:
        sid = self._norm_id(session_id)
        with self._lock:
            existing = self.get(sid)
            return existing if existing is not None else self.create(title=title, session_id=sid)

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.*, COUNT(m.id) AS message_count, "
                "SUM(CASE WHEN m.role='user' THEN 1 ELSE 0 END) AS prompt_count, "
                "(SELECT mm.role FROM messages mm WHERE mm.branch_id=b.id "
                "ORDER BY mm.ordinal DESC LIMIT 1) AS last_role, "
                "b.origin_device_id AS machine_id, d.name AS machine_name "
                "FROM conversations c "
                "LEFT JOIN branches b ON b.conversation_id=c.id AND b.is_main=1 "
                "LEFT JOIN devices d ON d.id=b.origin_device_id "
                "LEFT JOIN messages m ON m.branch_id=b.id "
                "WHERE c.deleted_at IS NULL GROUP BY c.id "
                "ORDER BY c.is_pinned DESC, c.is_favorite DESC, c.updated_at DESC"
            ).fetchall()
            return [{
                "id": row["id"], "title": row["title"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"],
                "messageCount": row["message_count"],
                "promptCount": row["prompt_count"],
                "isFavorite": bool(row["is_favorite"]),
                "isPinned": bool(row["is_pinned"]),
                "project": row["project"],
                "labels": json.loads(row["labels_json"] or "[]"),
                "awaitingResponse": row["last_role"] == "user",
                "machineId": row["machine_id"],
                "machineName": row["machine_name"] or f"Device {str(row['machine_id'])[:8]}",
            } for row in rows]

    def append_message(self, session_id: str, role: str, text: str, ok=None, exit_code=None,
                       attachments=None) -> dict:
        sid = self._norm_id(session_id)
        with self._lock:
            if self.get(sid) is None:
                self.create(session_id=sid)
            with self._conn:
                branch_id = self._main_branch_id(sid)
                message_ordinal = self._conn.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) + 1 AS value FROM messages WHERE branch_id=?",
                    (branch_id,),
                ).fetchone()["value"]
                ts = _now()
                message_id = str(uuid.uuid4())
                if role == "user":
                    turn_ordinal = self._conn.execute(
                        "SELECT COALESCE(MAX(ordinal), 0) + 1 AS value FROM turns WHERE branch_id=?",
                        (branch_id,),
                    ).fetchone()["value"]
                    turn_id = str(uuid.uuid4())
                    self._conn.execute(
                        "INSERT INTO turns(id, conversation_id, branch_id, ordinal, "
                        "user_message_id, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (turn_id, sid, branch_id, turn_ordinal, message_id, ts),
                    )
                else:
                    row = self._conn.execute(
                        "SELECT id, ordinal FROM turns WHERE branch_id=? ORDER BY ordinal DESC LIMIT 1",
                        (branch_id,),
                    ).fetchone()
                    turn_id = row["id"] if row else None
                    turn_ordinal = row["ordinal"] if row else None
                self._conn.execute(
                    "INSERT INTO messages(id, conversation_id, branch_id, turn_id, ordinal, "
                    "role, text, ts, ok, exit_code, attachments_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id, sid, branch_id, turn_id, message_ordinal, role, text, ts,
                        None if ok is None else int(bool(ok)), exit_code,
                        json.dumps(attachments, ensure_ascii=False) if attachments else None,
                    ),
                )
                if role == "assistant" and turn_id:
                    self._conn.execute(
                        "UPDATE turns SET status=? WHERE id=?",
                        ("completed" if ok is not False else "failed", turn_id),
                    )
                current = self._conn.execute(
                    "SELECT title FROM conversations WHERE id=?", (sid,)
                ).fetchone()
                new_title = current["title"]
                if not new_title and role == "user":
                    new_title = _title_from_text(text) or _title_from_attachments(attachments)
                self._conn.execute(
                    "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                    (new_title, ts, sid),
                )
                message = {
                    "id": message_id, "conversationId": sid, "branchId": branch_id,
                    "turnId": turn_id, "turnOrdinal": turn_ordinal,
                    "ordinal": message_ordinal, "role": role, "text": text, "ts": ts,
                }
                if ok is not None:
                    message["ok"] = bool(ok)
                if exit_code is not None:
                    message["exitCode"] = exit_code
                if attachments:
                    message["attachments"] = attachments
                self._record_event("message", message_id, "created", message)
        return self.get(sid)

    def replace_messages(self, session_id: str, messages: list, title: str = "",
                         updated_at: float | None = None) -> dict:
        sid = self._norm_id(session_id)
        with self._lock:
            existing = self.get(sid)
            if existing is None:
                existing = self.create(session_id=sid)
            with self._conn:
                branch_id = self._main_branch_id(sid)
                evidence_rows = [dict(row) for row in self._conn.execute(
                    "SELECT e.* FROM knowledge_evidence e "
                    "JOIN messages m ON m.id=e.message_id WHERE m.branch_id=?",
                    (branch_id,),
                ).fetchall()]
                old_rows = self._conn.execute(
                    "SELECT id FROM messages WHERE branch_id=?", (branch_id,)
                ).fetchall()
                old_ids = {row["id"] for row in old_rows}
                self._conn.execute("DELETE FROM messages WHERE branch_id=?", (branch_id,))
                self._conn.execute("DELETE FROM turns WHERE branch_id=?", (branch_id,))
                inserted = self._insert_transcript(sid, branch_id, messages or [])
                new_ids = {message["id"] for message in inserted}
                for evidence in evidence_rows:
                    source = self._conn.execute(
                        "SELECT conversation_id, branch_id, turn_id, text "
                        "FROM messages WHERE id=?",
                        (evidence["message_id"],),
                    ).fetchone()
                    if source is None:
                        continue
                    self._conn.execute(
                        "INSERT INTO knowledge_evidence(id, knowledge_id, version_id, "
                        "conversation_id, branch_id, turn_id, message_id, snippet, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            evidence["id"], evidence["knowledge_id"],
                            evidence["version_id"], source["conversation_id"],
                            source["branch_id"], source["turn_id"],
                            evidence["message_id"], self._preview(source["text"], 320),
                            evidence["created_at"],
                        ),
                    )
                latest = max(
                    [message.get("ts") or 0 for message in inserted]
                    + [existing.get("updatedAt") or 0, updated_at or 0, _now()]
                )
                new_title = existing.get("title") or _title_from_text(title)
                if not new_title:
                    first_user = next(
                        (message for message in inserted if message["role"] == "user"), None
                    )
                    if first_user:
                        new_title = _title_from_text(first_user["text"])
                self._conn.execute(
                    "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                    (new_title or "", latest, sid),
                )
                for deleted_id in sorted(old_ids - new_ids):
                    self._record_event("message", deleted_id, "deleted", {"id": deleted_id})
                for message in inserted:
                    if message["id"] not in old_ids:
                        self._record_event("message", message["id"], "created", message)
        return self.get(sid)

    def rename(self, session_id: str, title: str) -> dict | None:
        return self.update_metadata(session_id, {"title": title})

    @staticmethod
    def _normalize_labels(value) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("labels must be an array")
        labels: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("each label must be a string")
            label = " ".join(item.split())
            if not label:
                continue
            if len(label) > 32:
                raise ValueError("labels must be at most 32 characters")
            if label.casefold() not in {existing.casefold() for existing in labels}:
                labels.append(label)
        if len(labels) > 20:
            raise ValueError("a conversation can have at most 20 labels")
        return labels

    def _normalize_organization_changes(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("organization changes must be an object")
        allowed = {"isFavorite", "isPinned", "project", "labels"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown organization field(s): {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("at least one organization field is required")
        for key in ("isFavorite", "isPinned"):
            if key in changes and not isinstance(changes[key], bool):
                raise ValueError(f"{key} must be a boolean")
        if "project" in changes:
            if not isinstance(changes["project"], str):
                raise ValueError("project must be a string")
            project = " ".join(changes["project"].split())
            if len(project) > 80:
                raise ValueError("project must be at most 80 characters")
            changes = {**changes, "project": project}
        if "labels" in changes:
            changes = {**changes, "labels": self._normalize_labels(changes["labels"])}
        return changes

    def update_organization(self, session_id: str, changes: dict) -> dict | None:
        return self.update_metadata(
            session_id, self._normalize_organization_changes(changes)
        )

    def update_metadata(self, session_id: str, changes: dict) -> dict | None:
        sid = self._norm_id(session_id)
        if not isinstance(changes, dict):
            raise ValueError("session changes must be an object")
        allowed = {"title", "isFavorite", "isPinned", "project", "labels"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown session field(s): {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("at least one field is required")
        if "title" in changes and not isinstance(changes["title"], str):
            raise ValueError("title must be a string")
        organization = {
            key: changes[key] for key in allowed - {"title"} if key in changes
        }
        if organization:
            organization = self._normalize_organization_changes(organization)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL", (sid,)
            ).fetchone()
            if row is None:
                return None
            title = changes.get("title", row["title"]).strip()
            favorite = bool(organization.get("isFavorite", row["is_favorite"]))
            pinned = bool(organization.get("isPinned", row["is_pinned"]))
            project = organization.get("project", row["project"])
            labels = organization.get("labels", json.loads(row["labels_json"] or "[]"))
            now = _now()
            self._conn.execute(
                "UPDATE conversations SET title=?, is_favorite=?, is_pinned=?, "
                "project=?, labels_json=?, updated_at=?, organization_updated_at=? "
                "WHERE id=?",
                (
                    title, int(favorite), int(pinned), project,
                    json.dumps(labels, ensure_ascii=False),
                    now if "title" in changes else row["updated_at"],
                    now if organization else row["organization_updated_at"], sid,
                ),
            )
            if "title" in changes:
                self._record_event("conversation", sid, "updated", {
                    "id": sid, "title": title, "updatedAt": now,
                })
            if organization:
                self._record_event("conversation", sid, "organized", {
                    "id": sid, "isFavorite": favorite, "isPinned": pinned,
                    "project": project, "labels": labels, "updatedAt": now,
                })
        return self.get(sid)

    def delete(self, session_id: str) -> bool:
        try:
            sid = self._norm_id(session_id)
        except ValueError:
            return False
        with self._lock, self._conn:
            if not self._conn.execute(
                "SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL", (sid,)
            ).fetchone():
                return False
            now = _now()
            self._conn.execute(
                "UPDATE conversations SET deleted_at=?, updated_at=? WHERE id=?",
                (now, now, sid),
            )
            self._record_event("conversation", sid, "deleted", {
                "id": sid, "deletedAt": now,
            })
            return True

    # -- native source ids / local execution bindings -------------------

    @staticmethod
    def _external_ref_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "conversationId": row["conversation_id"],
            "branchId": row["branch_id"], "deviceId": row["device_id"],
            "adapterId": row["adapter_id"], "nativeSessionId": row["native_session_id"],
            "capabilities": json.loads(row["capabilities_json"] or "{}"),
            "createdAt": row["created_at"],
        }

    def add_external_ref(self, conversation_id: str, adapter_id: str,
                         native_session_id: str, capabilities: dict | None = None,
                         branch_id: str = "", device_id: str = "") -> dict:
        sid = self._norm_id(conversation_id)
        adapter = str(adapter_id or "").strip()
        native_id = str(native_session_id or "").strip()
        if not adapter or not native_id:
            raise ValueError("adapter id and native session id are required")
        device = device_id or self.device_id
        with self._lock, self._conn:
            if self.get(sid) is None:
                raise ValueError(f"unknown conversation: {sid}")
            branch = branch_id or self._main_branch_id(sid)
            existing = self._conn.execute(
                "SELECT * FROM external_refs WHERE device_id=? AND adapter_id=? "
                "AND native_session_id=?",
                (device, adapter, native_id),
            ).fetchone()
            if existing:
                if existing["conversation_id"] != sid:
                    raise ValueError("native session is already linked to another conversation")
                return self._external_ref_dict(existing)
            ref_id = str(uuid.uuid4())
            now = _now()
            payload = {
                "id": ref_id, "conversationId": sid, "branchId": branch,
                "deviceId": device, "adapterId": adapter,
                "nativeSessionId": native_id, "capabilities": capabilities or {},
                "createdAt": now,
            }
            self._conn.execute(
                "INSERT INTO external_refs(id, conversation_id, branch_id, device_id, "
                "adapter_id, native_session_id, capabilities_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref_id, sid, branch, device, adapter, native_id,
                    json.dumps(capabilities or {}, ensure_ascii=False), now,
                ),
            )
            if device == self.device_id:
                self._record_event("external_ref", ref_id, "created", payload)
            row = self._conn.execute("SELECT * FROM external_refs WHERE id=?", (ref_id,)).fetchone()
            return self._external_ref_dict(row)

    def find_by_external_ref(self, adapter_id: str, native_session_id: str,
                             device_id: str = "") -> dict | None:
        device = device_id or self.device_id
        with self._lock:
            row = self._conn.execute(
                "SELECT conversation_id FROM external_refs WHERE device_id=? "
                "AND adapter_id=? AND native_session_id=?",
                (device, adapter_id, native_session_id),
            ).fetchone()
            return self.get(row["conversation_id"]) if row else None

    def external_ref_for_conversation(self, conversation_id: str,
                                      adapter_ids: tuple[str, ...] = ()) -> dict | None:
        sid = self._norm_id(conversation_id)
        sql = "SELECT * FROM external_refs WHERE conversation_id=?"
        params: list = [sid]
        if adapter_ids:
            sql += " AND adapter_id IN (" + ",".join("?" for _ in adapter_ids) + ")"
            params.extend(adapter_ids)
        sql += " ORDER BY (device_id=?) DESC, created_at LIMIT 1"
        params.append(self.device_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return self._external_ref_dict(row) if row else None

    @staticmethod
    def _binding_dict(row: sqlite3.Row, created: bool = False) -> dict:
        return {
            "id": row["id"], "conversationId": row["conversation_id"],
            "branchId": row["branch_id"], "deviceId": row["device_id"],
            "provider": row["provider"], "nativeSessionId": row["native_session_id"],
            "state": row["state"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "created": created,
        }

    def get_or_create_execution_binding(self, conversation_id: str, provider: str,
                                        native_session_id: str = "") -> dict:
        sid = self._norm_id(conversation_id)
        provider_id = str(provider or "").strip().lower()
        if not provider_id:
            raise ValueError("provider is required")
        with self._lock, self._conn:
            if self.get(sid) is None:
                raise ValueError(f"unknown conversation: {sid}")
            branch_id = self._main_branch_id(sid)
            row = self._conn.execute(
                "SELECT * FROM execution_bindings WHERE device_id=? AND branch_id=? "
                "AND provider=? AND state='active'",
                (self.device_id, branch_id, provider_id),
            ).fetchone()
            if row:
                if native_session_id and row["native_session_id"] != native_session_id:
                    raise ValueError("conversation already has a different active native binding")
                return self._binding_dict(row)
            binding_id = str(uuid.uuid4())
            native_id = native_session_id or str(uuid.uuid4())
            now = _now()
            self._conn.execute(
                "INSERT INTO execution_bindings(id, conversation_id, branch_id, device_id, "
                "provider, native_session_id, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                (
                    binding_id, sid, branch_id, self.device_id, provider_id,
                    native_id, now, now,
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM execution_bindings WHERE id=?", (binding_id,)
            ).fetchone()
            return self._binding_dict(row, created=True)

    def execution_binding_for(self, conversation_id: str, provider: str = "") -> dict | None:
        sid = self._norm_id(conversation_id)
        sql = (
            "SELECT * FROM execution_bindings WHERE conversation_id=? AND device_id=? "
            "AND state='active'"
        )
        params = [sid, self.device_id]
        if provider:
            sql += " AND provider=?"
            params.append(provider.lower())
        sql += " ORDER BY created_at LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return self._binding_dict(row) if row else None

    # -- native watcher / unified inbox ---------------------------------

    @staticmethod
    def _inbox_dict(row: sqlite3.Row) -> dict:
        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                pass
        return {
            "id": row["id"],
            "source": row["source"],
            "sourceKey": row["source_key"],
            "nativeSessionId": row["native_session_id"],
            "conversationId": row["conversation_id"],
            "title": row["title"],
            "summary": row["summary"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
            "seenAt": row["seen_at"],
            "remindAt": row["remind_at"],
            "metadata": metadata,
        }

    def create_inbox_item(self, *, dedupe_key: str, source: str, source_key: str,
                          native_session_id: str = "", conversation_id: str | None = None,
                          title: str, summary: str, created_at: float | None = None,
                          metadata: dict | None = None) -> tuple[dict, bool]:
        """Insert one attention item, returning ``(item, created)``.

        ``dedupe_key`` is based on the source-qualified native session and final
        assistant response fingerprint, so rescans and process restarts cannot
        produce duplicate notifications.
        """
        dedupe = str(dedupe_key or "").strip()
        source_id = str(source or "").strip()
        qualified = str(source_key or "").strip()
        if not dedupe or not source_id or not qualified:
            raise ValueError("dedupe key, source, and source key are required")
        if conversation_id and self.get(conversation_id) is None:
            conversation_id = None
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM inbox_items WHERE dedupe_key=?", (dedupe,)
            ).fetchone()
            if existing:
                return self._inbox_dict(existing), False
            item_id = str(uuid.uuid4())
            now = float(created_at or _now())
            self._conn.execute(
                "INSERT INTO inbox_items(id, dedupe_key, source, source_key, "
                "native_session_id, conversation_id, title, summary, status, "
                "created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unread', ?, ?, ?)",
                (
                    item_id, dedupe, source_id, qualified,
                    native_session_id or None, conversation_id,
                    title or "Untitled conversation", summary or "(no text)",
                    now, now,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM inbox_items WHERE id=?", (item_id,)
            ).fetchone()
            return self._inbox_dict(row), True

    def list_inbox_items(self, statuses: tuple[str, ...] = (),
                         limit: int = 100) -> list[dict]:
        self._reactivate_due_reminders()
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM inbox_items"
        params: list = []
        if statuses:
            sql += " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._inbox_dict(row) for row in rows]

    def inbox_unread_count(self) -> int:
        self._reactivate_due_reminders()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS value FROM inbox_items WHERE status='unread'"
            ).fetchone()
            return int(row["value"])

    def update_inbox_item(self, item_id: str, status: str) -> dict | None:
        if status not in ("unread", "seen", "completed", "ignored"):
            raise ValueError("invalid inbox status")
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM inbox_items WHERE id=?", (item_id,)
            ).fetchone()
            if not row:
                return None
            seen_at = now if status != "unread" else None
            self._conn.execute(
                "UPDATE inbox_items SET status=?, updated_at=?, seen_at=?, remind_at=NULL "
                "WHERE id=?",
                (status, now, seen_at, item_id),
            )
            row = self._conn.execute(
                "SELECT * FROM inbox_items WHERE id=?", (item_id,)
            ).fetchone()
            return self._inbox_dict(row)

    def remind_inbox_item(self, item_id: str, remind_at: float) -> dict | None:
        try:
            target = float(remind_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("remindAt must be a timestamp") from exc
        now = _now()
        if target <= now:
            raise ValueError("remindAt must be in the future")
        if target > now + 366 * 86400:
            raise ValueError("remindAt must be within one year")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM inbox_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE inbox_items SET status='seen', updated_at=?, seen_at=?, remind_at=? "
                "WHERE id=?",
                (now, now, target, item_id),
            )
            row = self._conn.execute(
                "SELECT * FROM inbox_items WHERE id=?", (item_id,)
            ).fetchone()
            return self._inbox_dict(row)

    def _reactivate_due_reminders(self) -> int:
        now = _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE inbox_items SET status='unread', updated_at=?, seen_at=NULL, "
                "remind_at=NULL WHERE remind_at IS NOT NULL AND remind_at<=?",
                (now, now),
            )
            return cursor.rowcount

    def mark_all_inbox_seen(self) -> int:
        now = _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE inbox_items SET status='seen', updated_at=?, seen_at=?, remind_at=NULL "
                "WHERE status='unread'",
                (now, now),
            )
            return cursor.rowcount

    def complete_all_inbox_items(self) -> int:
        now = _now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE inbox_items SET status='completed', updated_at=?, seen_at=?, "
                "remind_at=NULL WHERE status IN ('unread', 'seen')",
                (now, now),
            )
            return cursor.rowcount

    def get_native_watch_cursor(self, source_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM native_watch_cursors WHERE source_key=?", (source_key,)
            ).fetchone()
            if not row:
                return None
            return {
                "sourceKey": row["source_key"],
                "source": row["source"],
                "nativeSessionId": row["native_session_id"],
                "observedMessageCount": row["observed_message_count"],
                "notifiedMessageCount": row["notified_message_count"],
                "lastAssistantFingerprint": row["last_assistant_fingerprint"] or "",
                "notifiedAssistantFingerprint": row["notified_assistant_fingerprint"] or "",
                "lastUpdatedAt": row["last_updated_at"],
                "stableScans": row["stable_scans"],
                "scannedAt": row["scanned_at"],
            }

    def upsert_native_watch_cursor(self, *, source_key: str, source: str,
                                   native_session_id: str, observed_message_count: int,
                                   notified_message_count: int,
                                   last_assistant_fingerprint: str = "",
                                   notified_assistant_fingerprint: str = "",
                                   last_updated_at: float | None = None,
                                   stable_scans: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO native_watch_cursors(source_key, source, native_session_id, "
                "observed_message_count, notified_message_count, "
                "last_assistant_fingerprint, notified_assistant_fingerprint, "
                "last_updated_at, stable_scans, scanned_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_key) DO UPDATE SET source=excluded.source, "
                "native_session_id=excluded.native_session_id, "
                "observed_message_count=excluded.observed_message_count, "
                "notified_message_count=excluded.notified_message_count, "
                "last_assistant_fingerprint=excluded.last_assistant_fingerprint, "
                "notified_assistant_fingerprint=excluded.notified_assistant_fingerprint, "
                "last_updated_at=excluded.last_updated_at, "
                "stable_scans=excluded.stable_scans, scanned_at=excluded.scanned_at",
                (
                    source_key, source, native_session_id,
                    max(0, int(observed_message_count)),
                    max(0, int(notified_message_count)),
                    last_assistant_fingerprint or None,
                    notified_assistant_fingerprint or None,
                    last_updated_at, max(0, int(stable_scans)), _now(),
                ),
            )

    # -- prompt timeline / settings -------------------------------------

    def get_settings(self) -> dict:
        settings = {
            "promptPreviewLength": self.DEFAULT_PROMPT_PREVIEW_LENGTH,
            "uiLanguage": "zh",
        }
        with self._lock:
            rows = self._conn.execute("SELECT key, value_json FROM settings").fetchall()
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value_json"])
            except json.JSONDecodeError:
                pass
        return settings

    def update_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        unknown = set(changes) - {"promptPreviewLength", "uiLanguage"}
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        if "promptPreviewLength" in changes:
            value = changes["promptPreviewLength"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("promptPreviewLength must be an integer")
            if not self.MIN_PROMPT_PREVIEW_LENGTH <= value <= self.MAX_PROMPT_PREVIEW_LENGTH:
                raise ValueError(
                    f"promptPreviewLength must be between {self.MIN_PROMPT_PREVIEW_LENGTH} "
                    f"and {self.MAX_PROMPT_PREVIEW_LENGTH}"
                )
        if "uiLanguage" in changes and changes["uiLanguage"] not in {"zh", "en"}:
            raise ValueError("uiLanguage must be 'zh' or 'en'")
        with self._lock, self._conn:
            for key, value in changes.items():
                now = _now()
                self._conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value), now),
                )
                self._record_event("setting", key, "updated", {
                    "key": key, "value": value, "updatedAt": now,
                })
        return self.get_settings()

    @staticmethod
    def _preview(text: str, limit: int) -> str:
        collapsed = " ".join((text or "").split())
        if len(collapsed) <= limit:
            return collapsed
        suffix = "..."
        return collapsed[:max(0, limit - len(suffix))].rstrip() + suffix

    def list_turns(self, session_id: str, preview_length: int | None = None) -> list[dict]:
        sid = self._norm_id(session_id)
        if preview_length is None:
            preview_length = self.get_settings()["promptPreviewLength"]
        if not self.MIN_PROMPT_PREVIEW_LENGTH <= preview_length <= self.MAX_PROMPT_PREVIEW_LENGTH:
            raise ValueError(
                f"preview length must be between {self.MIN_PROMPT_PREVIEW_LENGTH} "
                f"and {self.MAX_PROMPT_PREVIEW_LENGTH}"
            )
        with self._lock:
            if self.get(sid) is None:
                return []
            branch_id = self._main_branch_id(sid)
            rows = self._conn.execute(
                "SELECT t.*, m.text AS user_text, m.attachments_json, "
                "COUNT(all_messages.id) AS message_count "
                "FROM turns t "
                "LEFT JOIN messages m ON m.id=t.user_message_id "
                "LEFT JOIN messages all_messages ON all_messages.turn_id=t.id "
                "WHERE t.branch_id=? GROUP BY t.id ORDER BY t.ordinal",
                (branch_id,),
            ).fetchall()
        result = []
        for row in rows:
            text = row["user_text"] or ""
            if not text and row["attachments_json"]:
                try:
                    attachments = json.loads(row["attachments_json"])
                    names = [item.get("name") or "attachment" for item in attachments]
                    text = "Attachments: " + ", ".join(names)
                except json.JSONDecodeError:
                    pass
            result.append({
                "id": row["id"], "turnId": row["id"],
                "conversationId": sid, "branchId": branch_id,
                "ordinal": row["ordinal"], "userMessageId": row["user_message_id"],
                "previewText": self._preview(text, preview_length),
                "createdAt": row["created_at"], "status": row["status"],
                "messageCount": row["message_count"],
            })
        return result

    # -- versioned knowledge and message evidence ----------------------

    @staticmethod
    def _knowledge_version_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "knowledgeId": row["knowledge_id"],
            "versionNumber": row["version_number"],
            "bodyMarkdown": row["body_markdown"],
            "inputDigest": row["input_digest"],
            "extractorVersion": row["extractor_version"],
            "confidence": row["confidence"], "createdAt": row["created_at"],
            "evidenceCount": row["evidence_count"] if "evidence_count" in row.keys() else 0,
        }

    @staticmethod
    def _knowledge_evidence_dict(row: sqlite3.Row) -> dict:
        deep_link = f"/?conversation={row['conversation_id']}"
        if row["turn_id"]:
            deep_link += f"&turn={row['turn_id']}"
        deep_link += f"&message={row['message_id']}"
        return {
            "id": row["id"], "knowledgeId": row["knowledge_id"],
            "versionId": row["version_id"],
            "conversationId": row["conversation_id"],
            "conversationTitle": row["conversation_title"]
            if "conversation_title" in row.keys() else "",
            "branchId": row["branch_id"], "turnId": row["turn_id"],
            "turnOrdinal": row["turn_ordinal"]
            if "turn_ordinal" in row.keys() else None,
            "messageId": row["message_id"],
            "role": row["role"] if "role" in row.keys() else "",
            "snippet": row["snippet"], "createdAt": row["created_at"],
            "deepLink": deep_link,
        }

    @staticmethod
    def _knowledge_item_dict(row: sqlite3.Row) -> dict:
        try:
            labels = json.loads(row["labels_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            labels = []
        item = {
            "id": row["id"], "type": row["type"], "title": row["title"],
            "status": row["status"], "project": row["project"],
            "labels": labels if isinstance(labels, list) else [],
            "sourceConversationId": row["source_conversation_id"],
            "currentVersionId": row["current_version_id"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "deletedAt": row["deleted_at"],
        }
        if "body_markdown" in row.keys() and row["body_markdown"] is not None:
            item["bodyMarkdown"] = row["body_markdown"]
            item["versionNumber"] = row["version_number"]
            item["confidence"] = row["confidence"]
            item["extractorVersion"] = row["extractor_version"]
            item["evidenceCount"] = row["evidence_count"]
        return item

    def _normalize_knowledge_fields(self, knowledge_type: str, title: str,
                                    status: str, project: str, labels) -> tuple:
        kind = str(knowledge_type or "").strip().lower()
        if kind not in self.KNOWLEDGE_TYPES:
            raise ValueError("unsupported knowledge type")
        normalized_title = " ".join(str(title or "").split())
        if not normalized_title or len(normalized_title) > 160:
            raise ValueError("knowledge title must be between 1 and 160 characters")
        normalized_status = str(status or "draft").strip().lower()
        if normalized_status not in self.KNOWLEDGE_STATUSES:
            raise ValueError("unsupported knowledge status")
        normalized_project = " ".join(str(project or "").split())
        if len(normalized_project) > 80:
            raise ValueError("project must be at most 80 characters")
        return (
            kind, normalized_title, normalized_status, normalized_project,
            self._normalize_labels(labels or []),
        )

    def _knowledge_sources(self, message_ids: list[str]) -> list[sqlite3.Row]:
        ids = list(dict.fromkeys(str(value or "").strip() for value in message_ids or []))
        ids = [value for value in ids if value]
        if not ids:
            raise ValueError("at least one evidence message is required")
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            "SELECT m.*, c.title AS conversation_title, t.ordinal AS turn_ordinal "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "LEFT JOIN turns t ON t.id=m.turn_id "
            f"WHERE m.id IN ({placeholders})",
            ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        missing = [message_id for message_id in ids if message_id not in by_id]
        if missing:
            raise ValueError(f"unknown evidence message: {missing[0]}")
        return [by_id[message_id] for message_id in ids]

    @staticmethod
    def _knowledge_digest(body_markdown: str, message_ids: list[str]) -> str:
        source = json.dumps({
            "bodyMarkdown": body_markdown,
            "messageIds": sorted(message_ids),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _insert_knowledge_version(self, knowledge_id: str, body_markdown: str,
                                  source_rows: list[sqlite3.Row], confidence: str,
                                  extractor_version: str, input_digest: str,
                                  version_id: str = "") -> str:
        version_number = self._conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS value "
            "FROM knowledge_versions WHERE knowledge_id=?",
            (knowledge_id,),
        ).fetchone()["value"]
        version_id = version_id or str(uuid.uuid4())
        now = _now()
        self._conn.execute(
            "INSERT INTO knowledge_versions(id, knowledge_id, version_number, "
            "body_markdown, input_digest, extractor_version, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id, knowledge_id, version_number, body_markdown,
                input_digest, extractor_version, confidence, now,
            ),
        )
        self._record_event("knowledge_version", version_id, "created", {
            "id": version_id, "knowledgeId": knowledge_id,
            "versionNumber": version_number, "bodyMarkdown": body_markdown,
            "inputDigest": input_digest, "extractorVersion": extractor_version,
            "confidence": confidence, "createdAt": now,
        })
        for source in source_rows:
            evidence_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"copilotbridge:knowledge-evidence:{version_id}:{source['id']}",
            ))
            snippet = self._preview(source["text"], 320)
            self._conn.execute(
                "INSERT INTO knowledge_evidence(id, knowledge_id, version_id, "
                "conversation_id, branch_id, turn_id, message_id, snippet, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id, knowledge_id, version_id, source["conversation_id"],
                    source["branch_id"], source["turn_id"], source["id"], snippet, now,
                ),
            )
            self._record_event("knowledge_evidence", evidence_id, "created", {
                "id": evidence_id, "knowledgeId": knowledge_id,
                "versionId": version_id,
                "conversationId": source["conversation_id"],
                "branchId": source["branch_id"], "turnId": source["turn_id"],
                "messageId": source["id"], "snippet": snippet, "createdAt": now,
            })
        return version_id

    def _renumber_knowledge_versions(self, knowledge_id: str) -> None:
        rows = self._conn.execute(
            "SELECT id FROM knowledge_versions WHERE knowledge_id=? "
            "ORDER BY created_at, id",
            (knowledge_id,),
        ).fetchall()
        for index, row in enumerate(rows, 1):
            self._conn.execute(
                "UPDATE knowledge_versions SET version_number=? WHERE id=?",
                (-index, row["id"]),
            )
        for index, row in enumerate(rows, 1):
            self._conn.execute(
                "UPDATE knowledge_versions SET version_number=? WHERE id=?",
                (index, row["id"]),
            )

    def create_knowledge_item(self, *, knowledge_type: str, title: str,
                              body_markdown: str, evidence_message_ids: list[str],
                              status: str = "draft", project: str = "", labels=None,
                              confidence: str = "", extractor_version: str = "manual-v1",
                              input_digest: str = "") -> dict:
        kind, title, status, project, labels = self._normalize_knowledge_fields(
            knowledge_type, title, status, project, labels
        )
        body = str(body_markdown or "").strip()
        if not body:
            raise ValueError("knowledge body is required")
        extractor = str(extractor_version or "manual-v1").strip()[:80]
        confidence = str(confidence or "").strip().lower()[:32]
        with self._lock, self._conn:
            sources = self._knowledge_sources(evidence_message_ids)
            message_ids = [row["id"] for row in sources]
            digest = str(input_digest or "").strip() or self._knowledge_digest(body, message_ids)
            knowledge_id = str(uuid.uuid4())
            version_id = str(uuid.uuid4())
            now = _now()
            source_conversation_id = sources[0]["conversation_id"]
            self._conn.execute(
                "INSERT INTO knowledge_items(id, type, title, status, project, labels_json, "
                "source_conversation_id, current_version_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    knowledge_id, kind, title, status, project,
                    json.dumps(labels, ensure_ascii=False), source_conversation_id,
                    version_id, now, now,
                ),
            )
            self._record_event("knowledge_item", knowledge_id, "created", {
                "id": knowledge_id, "type": kind, "title": title, "status": status,
                "project": project, "labels": labels,
                "sourceConversationId": source_conversation_id,
                "currentVersionId": version_id, "createdAt": now, "updatedAt": now,
            })
            self._insert_knowledge_version(
                knowledge_id, body, sources, confidence, extractor, digest, version_id
            )
        return self.get_knowledge_item(knowledge_id)

    def add_knowledge_version(self, knowledge_id: str, *, body_markdown: str,
                              evidence_message_ids: list[str], confidence: str = "",
                              extractor_version: str = "manual-v1",
                              input_digest: str = "") -> dict | None:
        item_id = self._norm_id(knowledge_id)
        body = str(body_markdown or "").strip()
        if not body:
            raise ValueError("knowledge body is required")
        extractor = str(extractor_version or "manual-v1").strip()[:80]
        confidence = str(confidence or "").strip().lower()[:32]
        with self._lock, self._conn:
            item = self._conn.execute(
                "SELECT * FROM knowledge_items WHERE id=? AND deleted_at IS NULL",
                (item_id,),
            ).fetchone()
            if item is None:
                return None
            sources = self._knowledge_sources(evidence_message_ids)
            digest = str(input_digest or "").strip() or self._knowledge_digest(
                body, [row["id"] for row in sources]
            )
            existing = self._conn.execute(
                "SELECT id FROM knowledge_versions WHERE knowledge_id=? "
                "AND input_digest=? AND extractor_version=?",
                (item_id, digest, extractor),
            ).fetchone()
            if existing:
                return self.get_knowledge_item(item_id)
            version_id = self._insert_knowledge_version(
                item_id, body, sources, confidence, extractor, digest
            )
            now = _now()
            self._conn.execute(
                "UPDATE knowledge_items SET current_version_id=?, updated_at=? WHERE id=?",
                (version_id, now, item_id),
            )
            self._record_event("knowledge_item", item_id, "updated", {
                "id": item_id, "currentVersionId": version_id, "updatedAt": now,
            })
        return self.get_knowledge_item(item_id)

    def list_knowledge_items(self, *, query: str = "", status: str = "",
                             knowledge_type: str = "", project: str = "",
                             conversation_id: str = "") -> list[dict]:
        sql = (
            "SELECT k.*, v.body_markdown, v.version_number, v.confidence, "
            "v.extractor_version, (SELECT COUNT(*) FROM knowledge_evidence e "
            "WHERE e.version_id=k.current_version_id) AS evidence_count "
            "FROM knowledge_items k LEFT JOIN knowledge_versions v "
            "ON v.id=k.current_version_id WHERE k.deleted_at IS NULL"
        )
        params: list = []
        if status:
            sql += " AND k.status=?"; params.append(status)
        if knowledge_type:
            sql += " AND k.type=?"; params.append(knowledge_type)
        if project:
            sql += " AND k.project=?"; params.append(project)
        if conversation_id:
            sql += (
                " AND EXISTS (SELECT 1 FROM knowledge_evidence ce "
                "WHERE ce.knowledge_id=k.id AND ce.conversation_id=?)"
            )
            params.append(self._norm_id(conversation_id))
        if query.strip():
            sql += " AND (k.title LIKE ? OR v.body_markdown LIKE ?)"
            value = f"%{query.strip()}%"; params.extend((value, value))
        sql += " ORDER BY k.updated_at DESC, k.id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [self._knowledge_item_dict(row) for row in rows]

    def get_knowledge_item(self, knowledge_id: str) -> dict | None:
        try:
            item_id = self._norm_id(knowledge_id)
        except ValueError:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT k.*, v.body_markdown, v.version_number, v.confidence, "
                "v.extractor_version, (SELECT COUNT(*) FROM knowledge_evidence e "
                "WHERE e.version_id=k.current_version_id) AS evidence_count "
                "FROM knowledge_items k LEFT JOIN knowledge_versions v "
                "ON v.id=k.current_version_id WHERE k.id=? AND k.deleted_at IS NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            item = self._knowledge_item_dict(row)
            version_rows = self._conn.execute(
                "SELECT v.*, (SELECT COUNT(*) FROM knowledge_evidence e "
                "WHERE e.version_id=v.id) AS evidence_count FROM knowledge_versions v "
                "WHERE v.knowledge_id=? ORDER BY v.version_number DESC",
                (item_id,),
            ).fetchall()
            evidence_rows = self._conn.execute(
                "SELECT e.*, m.role, c.title AS conversation_title, "
                "t.ordinal AS turn_ordinal FROM knowledge_evidence e "
                "JOIN messages m ON m.id=e.message_id "
                "JOIN conversations c ON c.id=e.conversation_id "
                "LEFT JOIN turns t ON t.id=e.turn_id "
                "WHERE e.version_id=? ORDER BY e.created_at, e.id",
                (row["current_version_id"],),
            ).fetchall()
            item["versions"] = [self._knowledge_version_dict(value) for value in version_rows]
            item["evidence"] = [self._knowledge_evidence_dict(value) for value in evidence_rows]
            return item

    def update_knowledge_item(self, knowledge_id: str, changes: dict) -> dict | None:
        item_id = self._norm_id(knowledge_id)
        allowed = {"type", "title", "status", "project", "labels"}
        if not changes or set(changes) - allowed:
            raise ValueError("unsupported knowledge item update")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM knowledge_items WHERE id=? AND deleted_at IS NULL",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            kind, title, status, project, labels = self._normalize_knowledge_fields(
                changes.get("type", row["type"]), changes.get("title", row["title"]),
                changes.get("status", row["status"]), changes.get("project", row["project"]),
                changes.get("labels", json.loads(row["labels_json"] or "[]")),
            )
            now = _now()
            self._conn.execute(
                "UPDATE knowledge_items SET type=?, title=?, status=?, project=?, "
                "labels_json=?, updated_at=? WHERE id=?",
                (
                    kind, title, status, project, json.dumps(labels, ensure_ascii=False),
                    now, item_id,
                ),
            )
            self._record_event("knowledge_item", item_id, "updated", {
                "id": item_id, "type": kind, "title": title, "status": status,
                "project": project, "labels": labels,
                "currentVersionId": row["current_version_id"], "updatedAt": now,
            })
        return self.get_knowledge_item(item_id)

    def delete_knowledge_item(self, knowledge_id: str) -> bool:
        try:
            item_id = self._norm_id(knowledge_id)
        except ValueError:
            return False
        with self._lock, self._conn:
            if not self._conn.execute(
                "SELECT 1 FROM knowledge_items WHERE id=? AND deleted_at IS NULL",
                (item_id,),
            ).fetchone():
                return False
            now = _now()
            self._conn.execute(
                "UPDATE knowledge_items SET deleted_at=?, updated_at=? WHERE id=?",
                (now, now, item_id),
            )
            self._record_event("knowledge_item", item_id, "deleted", {
                "id": item_id, "deletedAt": now, "updatedAt": now,
            })
            return True

    @staticmethod
    def _knowledge_generation_job_dict(row: sqlite3.Row) -> dict:
        status = row["status"]
        phase = row["phase"]
        processed = row["processed_message_count"]
        remaining = row["remaining_message_count"]
        if status == "completed":
            progress = 100
        elif phase == "mapping":
            progress = 90
        elif phase == "extracting" and processed + remaining:
            progress = min(80, round(processed * 80 / (processed + remaining)))
        else:
            progress = 0
        result = {
            "conversationId": row["conversation_id"],
            "status": status,
            "phase": phase,
            "progressPercent": progress,
            "processedMessageCount": processed,
            "remainingMessageCount": remaining,
            "generatedItemCount": row["generated_item_count"],
            "maxConversations": row["max_conversations"],
            "resultMapId": row["result_map_id"] or "",
            "error": row["error"] or "",
            "startedAt": row["started_at"],
            "updatedAt": row["updated_at"],
            "completedAt": row["completed_at"],
        }
        if "conversation_title" in row.keys():
            result["conversationTitle"] = row["conversation_title"] or ""
        return result

    def get_knowledge_generation_job(self, conversation_id: str) -> dict | None:
        sid = self._norm_id(conversation_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT j.*, c.title AS conversation_title "
                "FROM knowledge_generation_jobs j "
                "JOIN conversations c ON c.id=j.conversation_id "
                "WHERE j.conversation_id=?",
                (sid,),
            ).fetchone()
        return self._knowledge_generation_job_dict(row) if row else None

    def list_knowledge_generation_jobs(self, *, limit: int = 20) -> list[dict]:
        safe_limit = max(1, min(int(limit), 100))
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.*, c.title AS conversation_title "
                "FROM knowledge_generation_jobs j "
                "JOIN conversations c ON c.id=j.conversation_id "
                "ORDER BY CASE WHEN j.status IN ('queued', 'running') THEN 0 ELSE 1 END, "
                "j.updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [self._knowledge_generation_job_dict(row) for row in rows]

    def begin_knowledge_generation_job(
        self, conversation_id: str, *, max_conversations: int = 48,
    ) -> tuple[dict, bool]:
        sid = self._norm_id(conversation_id)
        coverage = max(1, min(int(max_conversations), 200))
        with self._lock, self._conn:
            if self.get(sid) is None:
                raise ValueError(f"unknown conversation: {sid}")
            row = self._conn.execute(
                "SELECT status FROM knowledge_generation_jobs WHERE conversation_id=?",
                (sid,),
            ).fetchone()
            if row and row["status"] in {"queued", "running"}:
                return self.get_knowledge_generation_job(sid), False
            now = _now()
            self._conn.execute(
                "INSERT INTO knowledge_generation_jobs("
                "conversation_id, status, phase, max_conversations, started_at, updated_at"
                ") VALUES (?, 'queued', 'queued', ?, ?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET status='queued', phase='queued', "
                "processed_message_count=0, remaining_message_count=0, "
                "generated_item_count=0, max_conversations=excluded.max_conversations, "
                "result_map_id=NULL, error='', started_at=excluded.started_at, "
                "updated_at=excluded.updated_at, completed_at=NULL",
                (sid, coverage, now, now),
            )
        return self.get_knowledge_generation_job(sid), True

    def update_knowledge_generation_job(
        self, conversation_id: str, *, status: str, phase: str,
        processed_message_count: int | None = None,
        remaining_message_count: int | None = None,
        generated_item_count: int | None = None,
        result_map_id: str | None = None, error: str = "",
    ) -> dict:
        sid = self._norm_id(conversation_id)
        if status not in self.KNOWLEDGE_GENERATION_STATUSES:
            raise ValueError("invalid knowledge generation status")
        if phase not in self.KNOWLEDGE_GENERATION_PHASES:
            raise ValueError("invalid knowledge generation phase")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM knowledge_generation_jobs WHERE conversation_id=?",
                (sid,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown knowledge generation job: {sid}")
            processed = row["processed_message_count"] if processed_message_count is None \
                else max(0, int(processed_message_count))
            remaining = row["remaining_message_count"] if remaining_message_count is None \
                else max(0, int(remaining_message_count))
            generated = row["generated_item_count"] if generated_item_count is None \
                else max(0, int(generated_item_count))
            map_id = row["result_map_id"] if result_map_id is None else result_map_id
            now = _now()
            completed_at = now if status in {"completed", "failed"} else None
            self._conn.execute(
                "UPDATE knowledge_generation_jobs SET status=?, phase=?, "
                "processed_message_count=?, remaining_message_count=?, "
                "generated_item_count=?, result_map_id=?, error=?, updated_at=?, "
                "completed_at=? WHERE conversation_id=?",
                (
                    status, phase, processed, remaining, generated, map_id,
                    str(error or "")[:2000], now, completed_at, sid,
                ),
            )
        return self.get_knowledge_generation_job(sid)

    def prepare_knowledge_extraction(self, conversation_id: str, extractor_version: str,
                                     *, max_chars: int = 45000,
                                     max_messages: int = 120) -> dict:
        sid = self._norm_id(conversation_id)
        version = str(extractor_version or "").strip()
        if not version:
            raise ValueError("extractor version is required")
        max_chars = max(1000, min(int(max_chars), 200000))
        max_messages = max(1, min(int(max_messages), 500))
        with self._lock:
            if self.get(sid) is None:
                raise ValueError(f"unknown conversation: {sid}")
            branch_id = self._main_branch_id(sid)
            rows = self._conn.execute(
                "SELECT id, role, text, turn_id, ordinal, ts FROM messages "
                "WHERE branch_id=? AND role IN ('user', 'assistant') ORDER BY ordinal",
                (branch_id,),
            ).fetchall()
            run_rows = self._conn.execute(
                "SELECT source_message_ids_json, result_item_ids_json FROM "
                "knowledge_extraction_runs WHERE conversation_id=? AND extractor_version=? "
                "ORDER BY created_at, id",
                (sid, version),
            ).fetchall()
            processed = set()
            latest_result_ids = []
            for run in run_rows:
                try:
                    processed.update(json.loads(run["source_message_ids_json"] or "[]"))
                    latest_result_ids = json.loads(run["result_item_ids_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    continue
            pending = [row for row in rows if row["id"] not in processed and row["text"].strip()]
            batch = []
            used_chars = 0
            for row in pending:
                if len(batch) >= max_messages:
                    break
                remaining = max_chars - used_chars
                if remaining <= 0:
                    break
                text = row["text"]
                if len(text) > remaining:
                    if batch:
                        break
                    text = text[:remaining]
                batch.append({
                    "id": row["id"], "role": row["role"], "text": text,
                    "turnId": row["turn_id"], "ordinal": row["ordinal"], "ts": row["ts"],
                })
                used_chars += len(text)
            if not batch:
                items = [self.get_knowledge_item(item_id) for item_id in latest_result_ids]
                return {
                    "conversationId": sid, "extractorVersion": version,
                    "messages": [], "sourceMessageIds": [], "inputDigest": "",
                    "existingItems": self.list_knowledge_items(),
                    "remainingMessageCount": 0, "processedMessageCount": len(processed),
                    "cached": True, "noNewMessages": True,
                    "cachedItems": [item for item in items if item is not None],
                }
            digest_source = json.dumps(
                [{"id": item["id"], "role": item["role"], "text": item["text"]}
                 for item in batch],
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            cached = self._conn.execute(
                "SELECT result_item_ids_json FROM knowledge_extraction_runs "
                "WHERE conversation_id=? AND extractor_version=? AND input_digest=?",
                (sid, version, digest),
            ).fetchone()
            cached_items = []
            if cached:
                try:
                    cached_items = [
                        self.get_knowledge_item(item_id)
                        for item_id in json.loads(cached["result_item_ids_json"] or "[]")
                    ]
                except (json.JSONDecodeError, TypeError):
                    cached_items = []
            return {
                "conversationId": sid, "extractorVersion": version,
                "messages": batch, "sourceMessageIds": [item["id"] for item in batch],
                "inputDigest": digest,
                "existingItems": self.list_knowledge_items(),
                "remainingMessageCount": max(0, len(pending) - len(batch)),
                "processedMessageCount": len(processed), "cached": bool(cached),
                "noNewMessages": False,
                "cachedItems": [item for item in cached_items if item is not None],
            }

    def apply_knowledge_extraction(self, conversation_id: str, extractor_version: str,
                                   input_digest: str, source_message_ids: list[str],
                                   candidates: list[dict]) -> dict:
        sid = self._norm_id(conversation_id)
        version = str(extractor_version or "").strip()
        digest = str(input_digest or "").strip()
        source_ids = list(dict.fromkeys(str(value or "").strip()
                                        for value in source_message_ids or []))
        if not version or not digest or not source_ids:
            raise ValueError("extraction run metadata is incomplete")
        with self._lock, self._conn:
            cached = self._conn.execute(
                "SELECT result_item_ids_json FROM knowledge_extraction_runs "
                "WHERE conversation_id=? AND extractor_version=? AND input_digest=?",
                (sid, version, digest),
            ).fetchone()
            if cached:
                result_ids = json.loads(cached["result_item_ids_json"] or "[]")
                items = [self.get_knowledge_item(item_id) for item_id in result_ids]
                return {"cached": True, "items": [item for item in items if item]}

            source_rows = self._knowledge_sources(source_ids)
            if any(row["conversation_id"] != sid for row in source_rows):
                raise ValueError("extraction evidence must belong to the source conversation")
            allowed_sources = {row["id"]: row for row in source_rows}
            existing_rows = self._conn.execute(
                "SELECT * FROM knowledge_items WHERE deleted_at IS NULL"
            ).fetchall()
            existing = {
                (row["type"], _norm_text(row["title"]).casefold()): row
                for row in existing_rows
            }
            result_ids = []
            for candidate in candidates:
                kind, title, _, project, labels = self._normalize_knowledge_fields(
                    candidate.get("type"), candidate.get("title"), "draft",
                    candidate.get("project") or "", candidate.get("labels") or [],
                )
                body = str(candidate.get("bodyMarkdown") or "").strip()
                if not body:
                    raise ValueError("extracted knowledge body is required")
                evidence_ids = list(dict.fromkeys(candidate.get("evidenceMessageIds") or []))
                if not evidence_ids or any(value not in allowed_sources for value in evidence_ids):
                    raise ValueError("extracted knowledge references out-of-scope evidence")
                evidence_rows = [allowed_sources[value] for value in evidence_ids]
                confidence = str(candidate.get("confidence") or "").strip().lower()[:32]
                item_key = (kind, _norm_text(title).casefold())
                row = existing.get(item_key)
                version_digest = self._knowledge_digest(body, evidence_ids)
                if row is None:
                    item_id = str(uuid.uuid4())
                    version_id = str(uuid.uuid4())
                    now = _now()
                    self._conn.execute(
                        "INSERT INTO knowledge_items(id, type, title, status, project, "
                        "labels_json, source_conversation_id, current_version_id, "
                        "created_at, updated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)",
                        (
                            item_id, kind, title, project,
                            json.dumps(labels, ensure_ascii=False), sid, version_id, now, now,
                        ),
                    )
                    self._record_event("knowledge_item", item_id, "created", {
                        "id": item_id, "type": kind, "title": title, "status": "draft",
                        "project": project, "labels": labels,
                        "sourceConversationId": sid, "currentVersionId": version_id,
                        "createdAt": now, "updatedAt": now,
                    })
                    self._insert_knowledge_version(
                        item_id, body, evidence_rows, confidence, version,
                        version_digest, version_id,
                    )
                    row = self._conn.execute(
                        "SELECT * FROM knowledge_items WHERE id=?", (item_id,)
                    ).fetchone()
                    existing[item_key] = row
                else:
                    item_id = row["id"]
                    prior = self._conn.execute(
                        "SELECT id FROM knowledge_versions WHERE knowledge_id=? "
                        "AND input_digest=? AND extractor_version=?",
                        (item_id, version_digest, version),
                    ).fetchone()
                    if not prior:
                        version_id = self._insert_knowledge_version(
                            item_id, body, evidence_rows, confidence, version, version_digest
                        )
                        now = _now()
                        self._conn.execute(
                            "UPDATE knowledge_items SET current_version_id=?, title=?, "
                            "project=?, labels_json=?, updated_at=? WHERE id=?",
                            (
                                version_id, title, project,
                                json.dumps(labels, ensure_ascii=False), now, item_id,
                            ),
                        )
                        self._record_event("knowledge_item", item_id, "updated", {
                            "id": item_id, "type": kind, "title": title,
                            "status": row["status"], "project": project, "labels": labels,
                            "currentVersionId": version_id, "updatedAt": now,
                        })
                result_ids.append(item_id)

            run_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"copilotbridge:knowledge-run:{sid}:{version}:{digest}",
            ))
            self._conn.execute(
                "INSERT INTO knowledge_extraction_runs(id, conversation_id, "
                "extractor_version, input_digest, source_message_ids_json, "
                "result_item_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, sid, version, digest,
                    json.dumps(source_ids, ensure_ascii=False),
                    json.dumps(result_ids, ensure_ascii=False), _now(),
                ),
            )
        items = [self.get_knowledge_item(item_id) for item_id in result_ids]
        return {"cached": False, "items": [item for item in items if item]}

    @staticmethod
    def _balanced_history_sessions(sessions: list[dict], limit: int) -> list[dict]:
        noise_prefixes = (
            "you are a tool-free personal knowledge extraction engine",
            "you are a tool-free knowledge architect",
            "reply with exactly:", "security probe:", "[terminal ",
        )
        eligible = [
            item for item in sessions
            if not str(item.get("title") or "").strip().lower().startswith(noise_prefixes)
            and (
                int(item.get("promptCount") or 0) >= 3
                or item.get("isPinned") or item.get("isFavorite")
                or item.get("project") or item.get("labels")
            )
            and int(item.get("messageCount") or 0) > 0
        ]
        if len(eligible) <= limit:
            return eligible
        priority = [
            item for item in eligible
            if item.get("isPinned") or item.get("isFavorite")
        ][:limit]
        chosen_ids = {item["id"] for item in priority}
        regular = [item for item in eligible if item["id"] not in chosen_ids]
        remaining = limit - len(priority)
        recent_count = min(len(regular), (remaining + 1) // 2)
        selected = priority + regular[:recent_count]
        chosen_ids.update(item["id"] for item in selected)
        older = [item for item in regular[recent_count:] if item["id"] not in chosen_ids]
        older_slots = limit - len(selected)
        if older_slots > 0 and older:
            for index in range(older_slots):
                position = min(int(index * len(older) / older_slots), len(older) - 1)
                item = older[position]
                if item["id"] not in chosen_ids:
                    selected.append(item)
                    chosen_ids.add(item["id"])
        return selected[:limit]

    def prepare_knowledge_map(self, extractor_version: str, *,
                              max_chars: int = 22000,
                              max_conversations: int = 48,
                              focus_conversation_id: str = "") -> dict:
        version = str(extractor_version or "").strip()
        if not version:
            raise ValueError("extractor version is required")
        focus_id = (
            self._norm_id(focus_conversation_id)
            if str(focus_conversation_id or "").strip() else ""
        )
        max_chars = max(4000, min(int(max_chars), 120000))
        max_conversations = max(4, min(int(max_conversations), 200))
        with self._lock:
            all_sessions = self.list()
            focus_session = next(
                (item for item in all_sessions if item["id"] == focus_id), None
            ) if focus_id else None
            if focus_id and focus_session is None:
                raise ValueError(f"unknown conversation: {focus_id}")
            eligible_sessions = self._balanced_history_sessions(
                all_sessions, len(all_sessions) or 1
            )
            eligible_ids = {item["id"] for item in eligible_sessions}
            if focus_session:
                eligible_ids.add(focus_id)
            eligible_count = len(eligible_ids)
            selected = self._balanced_history_sessions(
                all_sessions, max_conversations
            )
            if focus_session:
                selected = [focus_session] + [
                    item for item in selected if item["id"] != focus_id
                ][:max_conversations - 1]
            conversations = []
            source_ids = []
            used_chars = 2
            for session in selected:
                focused = session["id"] == focus_id
                branch_id = self._main_branch_id(session["id"])
                rows = self._conn.execute(
                    "SELECT id, role, text, turn_id, ordinal, ts FROM messages "
                    "WHERE branch_id=? AND role IN ('user', 'assistant') "
                    "AND trim(text)<>'' ORDER BY ordinal",
                    (branch_id,),
                ).fetchall()
                if not rows:
                    if focused:
                        raise ValueError(
                            "focused conversation has no eligible messages"
                        )
                    continue
                if focused:
                    limit = min(40, len(rows))
                    positions = {
                        round(index * (len(rows) - 1) / max(1, limit - 1))
                        for index in range(limit)
                    }
                    candidates = [rows[position] for position in sorted(positions)]
                else:
                    user_rows = [row for row in rows if row["role"] == "user"]
                    assistant_rows = [row for row in rows if row["role"] == "assistant"]
                    candidates = []
                    if user_rows:
                        candidates.append(user_rows[0])
                        if user_rows[-1]["id"] != user_rows[0]["id"]:
                            candidates.append(user_rows[-1])
                    if assistant_rows:
                        candidates.append(assistant_rows[-1])
                    if not candidates:
                        candidates.append(rows[-1])
                candidates.sort(key=lambda row: row["ordinal"])
                messages = [{
                    "messageId": row["id"],
                    "role": row["role"],
                    "text": self._preview(row["text"], 360 if focused else 220),
                    "turnId": row["turn_id"],
                } for row in candidates]
                entry = {
                    "conversationId": session["id"],
                    "focus": focused,
                    "title": self._preview(str(
                        session.get("title") or "未命名会话"
                    ), 180),
                    "project": self._preview(str(
                        session.get("project") or ""
                    ), 120),
                    "labels": [
                        self._preview(str(label), 80)
                        for label in (session.get("labels") or [])[:8]
                    ],
                    "updatedAt": session.get("updatedAt"),
                    "messages": messages,
                }
                encoded = json.dumps(
                    entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                separator_chars = 1 if conversations else 0
                remaining_chars = max_chars - used_chars - separator_chars
                if len(encoded) > remaining_chars and conversations:
                    continue
                if len(encoded) > remaining_chars:
                    for message in messages:
                        message["text"] = self._preview(message["text"], 120)
                    encoded = json.dumps(
                        entry, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    )
                    while len(encoded) > remaining_chars and len(messages) > 1:
                        limit = max(1, len(messages) // 2)
                        positions = {
                            round(index * (len(messages) - 1) / max(1, limit - 1))
                            for index in range(limit)
                        }
                        messages[:] = [
                            messages[position] for position in sorted(positions)
                        ]
                        encoded = json.dumps(
                            entry, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        )
                if len(encoded) > remaining_chars:
                    if focused:
                        raise ValueError(
                            "focused conversation exceeds the map input limit"
                        )
                    continue
                conversations.append(entry)
                source_ids.extend(message["messageId"] for message in messages)
                used_chars += separator_chars + len(encoded)
            digest_source = json.dumps(
                conversations, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            map_id = f"conversation-{focus_id}" if focus_id else "history"
            cached = self.get_knowledge_map(map_id)
            return {
                "conversations": conversations,
                "sourceMessageIds": list(dict.fromkeys(source_ids)),
                "inputDigest": digest,
                "extractorVersion": version,
                "mapId": map_id,
                "eligibleConversationCount": eligible_count,
                "selectedConversationCount": len(conversations),
                "cached": bool(
                    cached
                    and cached.get("inputDigest") == digest
                    and cached.get("extractorVersion") == version
                ),
                "cachedMap": cached,
            }

    def get_knowledge_map(self, map_id: str = "history") -> dict | None:
        map_id = self._norm_id(map_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_maps WHERE id=?", (map_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                payload = json.loads(row["map_json"])
            except (json.JSONDecodeError, TypeError):
                return None
            evidence_ids = []
            for node in payload.get("nodes") or []:
                evidence_ids.extend(node.get("evidenceMessageIds") or [])
            evidence_by_id = {}
            if evidence_ids:
                unique_ids = list(dict.fromkeys(evidence_ids))
                placeholders = ",".join("?" for _ in unique_ids)
                sources = self._conn.execute(
                    "SELECT m.*, c.title AS conversation_title, "
                    "t.ordinal AS turn_ordinal "
                    "FROM messages m JOIN conversations c "
                    "ON c.id=m.conversation_id "
                    "LEFT JOIN turns t ON t.id=m.turn_id "
                    f"WHERE m.id IN ({placeholders})",
                    unique_ids,
                ).fetchall()
                for source in sources:
                    evidence_by_id[source["id"]] = {
                        "messageId": source["id"],
                        "conversationId": source["conversation_id"],
                        "conversationTitle": source["conversation_title"],
                        "turnId": source["turn_id"],
                        "turnOrdinal": source["turn_ordinal"],
                        "role": source["role"],
                        "snippet": self._preview(source["text"], 240),
                    }
            nodes = []
            for node in payload.get("nodes") or []:
                hydrated = dict(node)
                node_evidence_ids = list(dict.fromkeys(
                    node.get("evidenceMessageIds") or []
                ))
                hydrated["evidence"] = [
                    evidence_by_id[message_id]
                    for message_id in node_evidence_ids
                    if message_id in evidence_by_id
                ]
                hydrated["missingEvidenceCount"] = sum(
                    message_id not in evidence_by_id
                    for message_id in node_evidence_ids
                )
                nodes.append(hydrated)
            return {
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "nodes": nodes,
                "inputDigest": row["input_digest"],
                "extractorVersion": row["extractor_version"],
                "sourceConversationCount": row["source_conversation_count"],
                "sourceMessageCount": row["source_message_count"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }

    def save_knowledge_map(self, payload: dict, *, input_digest: str,
                           extractor_version: str,
                           source_message_ids: list[str],
                           source_conversation_count: int,
                           map_id: str = "history") -> dict:
        map_id = self._norm_id(map_id)
        digest = str(input_digest or "").strip()
        version = str(extractor_version or "").strip()
        if not digest or not version:
            raise ValueError("knowledge map metadata is incomplete")
        source_ids = list(dict.fromkeys(
            str(value or "").strip() for value in source_message_ids or []
        ))
        source_ids = [value for value in source_ids if value]
        if source_ids:
            self._knowledge_sources(source_ids)
        now = _now()
        existing = self.get_knowledge_map(map_id)
        created_at = existing["createdAt"] if existing else now
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO knowledge_maps(id, title, summary, map_json, "
                "input_digest, extractor_version, source_message_ids_json, "
                "source_conversation_count, source_message_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
                "summary=excluded.summary, map_json=excluded.map_json, "
                "input_digest=excluded.input_digest, "
                "extractor_version=excluded.extractor_version, "
                "source_message_ids_json=excluded.source_message_ids_json, "
                "source_conversation_count=excluded.source_conversation_count, "
                "source_message_count=excluded.source_message_count, "
                "updated_at=excluded.updated_at",
                (
                    map_id, str(payload.get("title") or "知识全景").strip(),
                    str(payload.get("summary") or "").strip(),
                    json.dumps(payload, ensure_ascii=False), digest, version,
                    json.dumps(source_ids, ensure_ascii=False),
                    max(0, int(source_conversation_count)), len(source_ids),
                    created_at, now,
                ),
            )
        return self.get_knowledge_map(map_id)

    # -- sync outbox -----------------------------------------------------

    def pending_sync_events(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sync_events WHERE device_id=? AND published_at IS NULL "
                "ORDER BY device_seq LIMIT ?",
                (self.device_id, limit),
            ).fetchall()
        return [{
            "eventId": row["event_id"], "deviceId": row["device_id"],
            "deviceSeq": row["device_seq"], "entityType": row["entity_type"],
            "entityId": row["entity_id"], "operation": row["operation"],
            "payload": json.loads(row["payload_json"]), "createdAt": row["created_at"],
        } for row in rows]

    def mark_sync_events_published(self, event_ids: list[str], package_id: str) -> None:
        ids = [str(event_id) for event_id in event_ids if event_id]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE sync_events SET published_at=?, package_id=? "
                f"WHERE device_id=? AND event_id IN ({placeholders})",
                (_now(), package_id, self.device_id, *ids),
            )

    def sync_cursor(self, source_device_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_device_seq FROM sync_cursors WHERE source_device_id=?",
                (source_device_id,),
            ).fetchone()
            return int(row["last_device_seq"]) if row else 0

    def _remote_ordinal(self, table: str, branch_id: str, preferred: int,
                        entity_id: str) -> int:
        if table not in {"turns", "messages"}:
            raise ValueError(f"unsupported ordinal table: {table}")
        preferred = max(1, int(preferred))
        row = self._conn.execute(
            f"SELECT id FROM {table} WHERE branch_id=? AND ordinal=?",
            (branch_id, preferred),
        ).fetchone()
        if row is None or row["id"] == entity_id:
            return preferred
        return int(self._conn.execute(
            f"SELECT COALESCE(MAX(ordinal), 0) + 1 AS value "
            f"FROM {table} WHERE branch_id=?",
            (branch_id,),
        ).fetchone()["value"])

    def apply_remote_events(self, events: list[dict], source_device_id: str,
                            through_seq: int) -> int:
        """Apply one ordered remote package and advance its cursor atomically.

        Remote mutations deliberately bypass ``_record_event`` so downloaded data
        never re-enters the local outbox and creates an upload loop.
        """
        applied = 0
        with self._lock, self._conn:
            for event in sorted(events, key=lambda item: int(item["deviceSeq"])):
                event_id = str(event["eventId"])
                if event.get("deviceId") != source_device_id:
                    raise ValueError("sync package contains an event from another device")
                if self._conn.execute(
                    "SELECT 1 FROM applied_events WHERE event_id=?", (event_id,)
                ).fetchone():
                    continue
                self._apply_remote_event(event)
                self._conn.execute(
                    "INSERT INTO applied_events(event_id, source_device_id, applied_at) "
                    "VALUES (?, ?, ?)",
                    (event_id, source_device_id, _now()),
                )
                applied += 1
            current = self.sync_cursor(source_device_id)
            self._conn.execute(
                "INSERT INTO sync_cursors(source_device_id, last_device_seq, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(source_device_id) DO UPDATE SET "
                "last_device_seq=MAX(sync_cursors.last_device_seq, excluded.last_device_seq), "
                "updated_at=excluded.updated_at",
                (source_device_id, max(current, int(through_seq)), _now()),
            )
        return applied

    def _apply_remote_event(self, event: dict) -> None:
        entity_type = event.get("entityType")
        operation = event.get("operation")
        payload = event.get("payload") or {}
        event_time = float(event.get("createdAt") or _now())

        if entity_type == "device" and operation == "updated":
            device_id = str(payload.get("id") or event.get("deviceId") or "")
            name = " ".join(str(payload.get("name") or "").split())[:80]
            if not device_id or not name:
                raise ValueError("device event is missing id or name")
            if device_id != str(event.get("deviceId") or ""):
                raise ValueError("device event cannot rename another device")
            updated = float(payload.get("updatedAt") or event_time)
            self._conn.execute(
                "INSERT INTO devices(id, name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at "
                "WHERE devices.updated_at<=excluded.updated_at",
                (device_id, name, updated),
            )
            return

        if entity_type == "conversation" and operation == "created":
            sid = self._norm_id(payload.get("id"))
            branch_id = str(payload.get("branchId") or uuid.uuid4())
            created = float(payload.get("createdAt") or event_time)
            updated = float(payload.get("updatedAt") or created)
            self._conn.execute(
                "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, payload.get("title") or "", created, updated),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO branches(id, conversation_id, origin_device_id, "
                "created_at, is_main) VALUES (?, ?, ?, ?, 1)",
                (branch_id, sid, payload.get("originDeviceId") or event["deviceId"], created),
            )
            return

        if entity_type == "conversation" and operation == "updated":
            sid = self._norm_id(payload.get("id"))
            updated = float(payload.get("updatedAt") or event_time)
            self._conn.execute(
                "UPDATE conversations SET title=?, updated_at=? "
                "WHERE id=? AND updated_at<=?",
                (payload.get("title") or "", updated, sid, updated),
            )
            return

        if entity_type == "conversation" and operation == "organized":
            sid = self._norm_id(payload.get("id"))
            updated = float(payload.get("updatedAt") or event_time)
            labels = self._normalize_labels(payload.get("labels") or [])
            project = " ".join(str(payload.get("project") or "").split())[:80]
            self._conn.execute(
                "UPDATE conversations SET is_favorite=?, is_pinned=?, project=?, "
                "labels_json=?, organization_updated_at=? "
                "WHERE id=? AND organization_updated_at<=?",
                (
                    int(bool(payload.get("isFavorite"))),
                    int(bool(payload.get("isPinned"))), project,
                    json.dumps(labels, ensure_ascii=False), updated, sid, updated,
                ),
            )
            return

        if entity_type == "conversation" and operation == "deleted":
            sid = self._norm_id(payload.get("id"))
            deleted = float(payload.get("deletedAt") or event_time)
            self._conn.execute(
                "UPDATE conversations SET deleted_at=?, updated_at=? WHERE id=?",
                (deleted, deleted, sid),
            )
            return

        if entity_type == "message" and operation == "created":
            sid = self._norm_id(payload.get("conversationId"))
            branch_id = str(payload.get("branchId") or "")
            if not self._conn.execute(
                "SELECT 1 FROM conversations WHERE id=?", (sid,)
            ).fetchone():
                raise ValueError(f"message references unknown conversation: {sid}")
            if not self._conn.execute(
                "SELECT 1 FROM branches WHERE id=? AND conversation_id=?", (branch_id, sid)
            ).fetchone():
                raise ValueError(f"message references unknown branch: {branch_id}")
            turn_id = payload.get("turnId")
            role = payload.get("role") or "user"
            ts = float(payload.get("ts") or event_time)
            if turn_id and not self._conn.execute(
                "SELECT 1 FROM turns WHERE id=?", (turn_id,)
            ).fetchone():
                turn_ordinal = self._remote_ordinal(
                    "turns", branch_id, payload.get("turnOrdinal") or 1, str(turn_id)
                )
                self._conn.execute(
                    "INSERT INTO turns(id, conversation_id, branch_id, ordinal, "
                    "user_message_id, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        turn_id, sid, branch_id, turn_ordinal,
                        payload.get("id") if role == "user" else None,
                        ts, "pending",
                    ),
                )
            attachments = payload.get("attachments")
            message_id = str(payload["id"])
            message_ordinal = self._remote_ordinal(
                "messages", branch_id, payload.get("ordinal") or 1, message_id
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO messages(id, conversation_id, branch_id, turn_id, "
                "ordinal, role, text, ts, ok, exit_code, attachments_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id, sid, branch_id, turn_id,
                    message_ordinal, role, payload.get("text") or "", ts,
                    None if payload.get("ok") is None else int(bool(payload.get("ok"))),
                    payload.get("exitCode"),
                    json.dumps(attachments, ensure_ascii=False) if attachments else None,
                ),
            )
            if role == "assistant" and turn_id:
                self._conn.execute(
                    "UPDATE turns SET status=? WHERE id=?",
                    ("completed" if payload.get("ok") is not False else "failed", turn_id),
                )
            self._conn.execute(
                "UPDATE conversations SET updated_at=MAX(updated_at, ?) WHERE id=?",
                (ts, sid),
            )
            return

        if entity_type == "message" and operation == "deleted":
            self._conn.execute("DELETE FROM messages WHERE id=?", (payload.get("id"),))
            return

        if entity_type == "setting" and operation == "updated":
            key = str(payload.get("key") or "")
            if key != "promptPreviewLength":
                return
            updated = float(payload.get("updatedAt") or event_time)
            current = self._conn.execute(
                "SELECT updated_at FROM settings WHERE key=?", (key,)
            ).fetchone()
            if not current or float(current["updated_at"]) <= updated:
                self._conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(payload.get("value")), updated),
                )
            return

        if entity_type == "knowledge_item" and operation == "created":
            item_id = self._norm_id(payload.get("id"))
            source_conversation_id = payload.get("sourceConversationId") or None
            if source_conversation_id:
                source_conversation_id = self._norm_id(source_conversation_id)
                if not self._conn.execute(
                    "SELECT 1 FROM conversations WHERE id=?", (source_conversation_id,)
                ).fetchone():
                    raise SyncDependencyError(
                        f"knowledge item references unknown conversation: {source_conversation_id}"
                    )
            kind, title, status, project, labels = self._normalize_knowledge_fields(
                payload.get("type"), payload.get("title"), payload.get("status") or "draft",
                payload.get("project") or "", payload.get("labels") or [],
            )
            created = float(payload.get("createdAt") or event_time)
            updated = float(payload.get("updatedAt") or created)
            self._conn.execute(
                "INSERT INTO knowledge_items(id, type, title, status, project, labels_json, "
                "source_conversation_id, current_version_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET type=excluded.type, title=excluded.title, "
                "status=excluded.status, project=excluded.project, "
                "labels_json=excluded.labels_json, "
                "source_conversation_id=excluded.source_conversation_id, "
                "current_version_id=excluded.current_version_id, updated_at=excluded.updated_at "
                "WHERE knowledge_items.updated_at<=excluded.updated_at",
                (
                    item_id, kind, title, status, project,
                    json.dumps(labels, ensure_ascii=False), source_conversation_id,
                    payload.get("currentVersionId"), created, updated,
                ),
            )
            return

        if entity_type == "knowledge_item" and operation == "updated":
            item_id = self._norm_id(payload.get("id"))
            row = self._conn.execute(
                "SELECT * FROM knowledge_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"knowledge update references unknown item: {item_id}")
            updated = float(payload.get("updatedAt") or event_time)
            if float(row["updated_at"]) > updated:
                return
            incoming_version_id = (
                str(payload.get("currentVersionId") or "").strip()
                or row["current_version_id"]
            )
            if (
                float(row["updated_at"]) == updated
                and row["current_version_id"]
                and incoming_version_id
                and incoming_version_id < row["current_version_id"]
            ):
                return
            kind, title, status, project, labels = self._normalize_knowledge_fields(
                payload.get("type", row["type"]), payload.get("title", row["title"]),
                payload.get("status", row["status"]), payload.get("project", row["project"]),
                payload.get("labels", json.loads(row["labels_json"] or "[]")),
            )
            self._conn.execute(
                "UPDATE knowledge_items SET type=?, title=?, status=?, project=?, "
                "labels_json=?, current_version_id=?, updated_at=? WHERE id=?",
                (
                    kind, title, status, project, json.dumps(labels, ensure_ascii=False),
                    incoming_version_id,
                    updated, item_id,
                ),
            )
            return

        if entity_type == "knowledge_item" and operation == "deleted":
            item_id = self._norm_id(payload.get("id"))
            deleted = float(payload.get("deletedAt") or event_time)
            self._conn.execute(
                "UPDATE knowledge_items SET deleted_at=?, updated_at=? "
                "WHERE id=? AND (deleted_at IS NULL OR deleted_at<=?)",
                (deleted, deleted, item_id, deleted),
            )
            return

        if entity_type == "knowledge_version" and operation == "created":
            version_id = self._norm_id(payload.get("id"))
            item_id = self._norm_id(payload.get("knowledgeId"))
            if not self._conn.execute(
                "SELECT 1 FROM knowledge_items WHERE id=?", (item_id,)
            ).fetchone():
                raise SyncDependencyError(
                    f"knowledge version references unknown item: {item_id}"
                )
            if self._conn.execute(
                "SELECT 1 FROM knowledge_versions WHERE id=?", (version_id,)
            ).fetchone():
                return
            digest = str(payload.get("inputDigest") or "")
            extractor = str(payload.get("extractorVersion") or "manual-v1")
            semantic_match = self._conn.execute(
                "SELECT id FROM knowledge_versions WHERE knowledge_id=? "
                "AND input_digest=? AND extractor_version=?",
                (item_id, digest, extractor),
            ).fetchone()
            if semantic_match and semantic_match["id"] != version_id:
                larger_id = max(semantic_match["id"], version_id)
                conflict_digest = f"{digest}:concurrent:{larger_id}"
                if larger_id == semantic_match["id"]:
                    self._conn.execute(
                        "UPDATE knowledge_versions SET input_digest=? WHERE id=?",
                        (conflict_digest, semantic_match["id"]),
                    )
                else:
                    digest = conflict_digest
            next_number = self._conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 AS value "
                "FROM knowledge_versions WHERE knowledge_id=?",
                (item_id,),
            ).fetchone()["value"]
            self._conn.execute(
                "INSERT INTO knowledge_versions(id, knowledge_id, version_number, "
                "body_markdown, input_digest, extractor_version, confidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id, item_id, next_number,
                    str(payload.get("bodyMarkdown") or ""),
                    digest, extractor,
                    str(payload.get("confidence") or ""),
                    float(payload.get("createdAt") or event_time),
                ),
            )
            self._renumber_knowledge_versions(item_id)
            return

        if entity_type == "knowledge_evidence" and operation == "created":
            evidence_id = self._norm_id(payload.get("id"))
            item_id = self._norm_id(payload.get("knowledgeId"))
            version_id = self._norm_id(payload.get("versionId"))
            conversation_id = self._norm_id(payload.get("conversationId"))
            branch_id = self._norm_id(payload.get("branchId"))
            turn_id = str(payload.get("turnId") or "").strip() or None
            message_id = self._norm_id(payload.get("messageId"))
            if not self._conn.execute(
                "SELECT 1 FROM knowledge_items WHERE id=?", (item_id,)
            ).fetchone():
                raise SyncDependencyError(
                    f"knowledge evidence references unknown item: {item_id}"
                )
            version = self._conn.execute(
                "SELECT knowledge_id FROM knowledge_versions WHERE id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise SyncDependencyError(
                    f"knowledge evidence references unknown version: {version_id}"
                )
            if version["knowledge_id"] != item_id:
                raise ValueError("knowledge evidence version does not belong to item")
            source = self._conn.execute(
                "SELECT conversation_id, branch_id, turn_id FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            if source is None:
                raise SyncDependencyError(
                    f"knowledge evidence references unknown message: {message_id}"
                )
            if (
                source["conversation_id"] != conversation_id
                or source["branch_id"] != branch_id
                or source["turn_id"] != turn_id
            ):
                raise ValueError("knowledge evidence source provenance is inconsistent")
            self._conn.execute(
                "INSERT OR IGNORE INTO knowledge_evidence(id, knowledge_id, version_id, "
                "conversation_id, branch_id, turn_id, message_id, snippet, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id, item_id, version_id, conversation_id,
                    branch_id, turn_id, message_id,
                    str(payload.get("snippet") or ""),
                    float(payload.get("createdAt") or event_time),
                ),
            )
            return

        if entity_type == "external_ref" and operation == "created":
            sid = self._norm_id(payload.get("conversationId"))
            if not self._conn.execute(
                "SELECT 1 FROM conversations WHERE id=?", (sid,)
            ).fetchone():
                raise ValueError(f"external reference points to unknown conversation: {sid}")
            self._conn.execute(
                "INSERT OR IGNORE INTO external_refs(id, conversation_id, branch_id, "
                "device_id, adapter_id, native_session_id, capabilities_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["id"], sid, payload.get("branchId"), payload["deviceId"],
                    payload["adapterId"], payload["nativeSessionId"],
                    json.dumps(payload.get("capabilities") or {}, ensure_ascii=False),
                    float(payload.get("createdAt") or event_time),
                ),
            )
            return

        raise ValueError(f"unsupported sync event: {entity_type}/{operation}")
