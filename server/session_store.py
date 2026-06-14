"""Durable, server-owned conversation sessions for the Copilot bridge.

Each session is identified by a uuid that is BOTH the ``conversationId`` the
client sends AND the Copilot CLI ``--session-id`` (1:1 and immutable). Sessions
are the source of truth and are persisted one-JSON-file-per-session under
``sessions_dir`` (``<id>.json``). Writes are atomic (temp file + ``os.replace``)
so a crash or concurrent reader never observes a half-written file.

Session dict shape (timestamps are epoch seconds, ``time.time()``)::

    {
      "id": "<uuid4>",            # == conversationId == copilot --session-id
      "title": "",               # derived from the first user message
      "createdAt": <ts>,
      "updatedAt": <ts>,
      "messageCount": <int>,
      "messages": [
        {"role": "user"|"assistant", "text": "...", "ts": <ts>,
         "ok": <bool optional>, "exitCode": <int optional>}
      ]
    }
"""

import json
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger("copilot_bridge.sessions")

# Titles are a single trimmed line capped at ~60 characters.
_TITLE_MAX = 60


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


class SessionStore:
    """Thread-safe, disk-backed store of conversation sessions."""

    def __init__(self, sessions_dir: str):
        self.dir = os.path.abspath(sessions_dir)
        os.makedirs(self.dir, exist_ok=True)
        # Guards the in-memory index AND serializes file writes. Reentrant so
        # public methods can compose (e.g. append_message -> create).
        self._lock = threading.RLock()
        self._index: dict[str, dict] = {}
        self._load_existing()

    # -- id / path safety -------------------------------------------------

    def _norm_id(self, session_id) -> str:
        """Return a normalized, path-safe session id or raise ValueError.

        Rejects anything that could escape ``self.dir`` (path separators, '..',
        NUL bytes, empties) so a client-supplied conversationId can never be
        used to read or write outside the sessions directory.
        """
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

    def _path_for(self, session_id: str) -> str:
        sid = self._norm_id(session_id)
        path = os.path.abspath(os.path.join(self.dir, sid + ".json"))
        # Belt-and-suspenders: the resolved file must live directly in self.dir.
        if os.path.dirname(path) != self.dir:
            raise ValueError(f"unsafe session id: {session_id!r}")
        return path

    # -- persistence helpers ---------------------------------------------

    def _load_existing(self) -> None:
        try:
            names = os.listdir(self.dir)
        except OSError as exc:  # pragma: no cover - dir just created above
            logger.warning("Cannot list sessions dir %s: %s", self.dir, exc)
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:  # noqa: BLE001 - skip bad files
                logger.warning("Skipping unreadable session file %s: %s", name, exc)
                continue
            sid = data.get("id") if isinstance(data, dict) else None
            if isinstance(sid, str) and sid:
                self._index[sid] = data
            else:
                logger.warning("Skipping session file %s: missing/invalid id", name)
        logger.info("Loaded %d session(s) from %s", len(self._index), self.dir)

    def _persist(self, session: dict) -> None:
        """Atomically write ``session`` to ``<id>.json`` (temp file + replace)."""
        path = self._path_for(session["id"])
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        payload = json.dumps(session, ensure_ascii=False, indent=2)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)  # atomic on Windows + POSIX
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    # -- public API ------------------------------------------------------

    @staticmethod
    def summarize(session: dict) -> dict:
        """Return the message-free summary view of a session."""
        return {
            "id": session["id"],
            "title": session.get("title", ""),
            "createdAt": session.get("createdAt"),
            "updatedAt": session.get("updatedAt"),
            "messageCount": session.get("messageCount", 0),
        }

    def create(self, title: str = "", session_id: str = "") -> dict:
        """Create and persist a new session, returning the full dict."""
        with self._lock:
            sid = self._norm_id(session_id) if (session_id and session_id.strip()) else str(uuid.uuid4())
            now = _now()
            session = {
                "id": sid,
                "title": title or "",
                "createdAt": now,
                "updatedAt": now,
                "messageCount": 0,
                "messages": [],
            }
            self._persist(session)
            self._index[sid] = session
            return session

    def get(self, session_id: str) -> dict | None:
        try:
            sid = self._norm_id(session_id)
        except ValueError:
            return None
        with self._lock:
            return self._index.get(sid)

    def get_or_create(self, session_id: str, title: str = "") -> dict:
        """Return the session for ``session_id``, creating it with that id if
        unknown so client-chosen uuids become the canonical session id."""
        sid = self._norm_id(session_id)  # raises on unsafe id
        with self._lock:
            existing = self._index.get(sid)
            if existing is not None:
                return existing
            return self.create(title=title, session_id=sid)

    def list(self) -> list[dict]:
        """Return summaries (no messages) sorted by updatedAt descending."""
        with self._lock:
            summaries = [self.summarize(s) for s in self._index.values()]
        summaries.sort(key=lambda s: (s.get("updatedAt") or 0), reverse=True)
        return summaries

    def append_message(self, session_id: str, role: str, text: str, ok=None, exit_code=None,
                       attachments=None) -> dict:
        """Append a message (creating the session if missing) and persist.

        ``attachments`` is an optional list of metadata dicts describing files the
        client sent with the turn, e.g.
        ``{"name", "mime", "url", "size", "kind"}``; stored verbatim so clients can
        render image thumbnails when the transcript is reloaded.
        """
        with self._lock:
            session = self._index.get(self._norm_id(session_id))
            if session is None:
                session = self.create(session_id=session_id)
            ts = _now()
            message = {"role": role, "text": text, "ts": ts}
            if ok is not None:
                message["ok"] = ok
            if exit_code is not None:
                message["exitCode"] = exit_code
            if attachments:
                message["attachments"] = attachments
            session["messages"].append(message)
            session["messageCount"] = len(session["messages"])
            session["updatedAt"] = ts
            if not session.get("title") and role == "user":
                session["title"] = _title_from_text(text) or _title_from_attachments(attachments)
            self._persist(session)
            return session

    def rename(self, session_id: str, title: str) -> dict | None:
        with self._lock:
            session = self.get(session_id)
            if session is None:
                return None
            session["title"] = title or ""
            session["updatedAt"] = _now()
            self._persist(session)
            return session

    def delete(self, session_id: str) -> bool:
        try:
            path = self._path_for(session_id)
            sid = self._norm_id(session_id)
        except ValueError:
            return False
        with self._lock:
            existed = self._index.pop(sid, None) is not None
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as exc:  # noqa: BLE001
                logger.warning("Failed to remove session file %s: %s", path, exc)
            return existed
