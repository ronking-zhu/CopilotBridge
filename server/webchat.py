"""Direct mobile/web chat API for the Copilot bridge.

Adds a plain-JSON chat API (no Bot Framework needed) plus serves the mobile web app.
Designed to be reached directly from a phone through a Dev Tunnel:

    POST /api/chat        {message, conversationId, reset?, images?}  -> {jobId, conversationId, sessionId, title}
    GET  /api/chat/{id}                                      -> {status, reply, sessionId, ...}
    POST /api/chat-sync   {message, conversationId, reset?, images?}  -> {reply, sessionId, title, ...}  (waits)
    GET  /api/webconfig                                      -> {authRequired}
    POST   /api/sessions  {title?}                           -> 201 session summary
    GET    /api/sessions                                     -> [session summaries]
    GET    /api/sessions/{id}                                -> full session (with messages)
    POST   /api/sessions/{id}/sync                           -> reconcile with the Copilot CLI's
                                                                 own transcript, then full session
    DELETE /api/sessions/{id}                                -> {deleted: true}
    PATCH  /api/sessions/{id} {title}                        -> session summary
    GET  /api/copilot-sessions                               -> [Copilot CLI native session summaries]
    GET  /api/copilot-sessions/{id}                          -> full Copilot session (with messages)
    POST /api/copilot-sessions/{id}/import                   -> import into bridge store (201 summary)
    GET  /api/uploads/{sid}/{name}                           -> a client-uploaded image (auth via header or ?key=)
    GET  /                                                   -> mobile web app

Clients may attach images to a turn via ``images: [{name, mime, data}]`` (``data`` is
base64 or a data: URL). The server saves them under ``<workdir>/.uploads/<sid>/`` and
passes each to the Copilot CLI through its native ``--attachment`` flag; the saved
files are recorded on the user message as ``attachments`` so they re-render on reload.

The conversationId a client sends IS the persisted session id and the Copilot CLI
``--session-id`` (1:1, immutable). A shared token (CHAT_API_TOKEN) protects the API
since the tunnel is public.
"""

import asyncio
import base64
import binascii
import collections
import json
import os
import re
import time
import uuid

from aiohttp import web

import copilot_sessions
from paths import app_base_dir, resource_dir
from session_store import SessionStore, merge_message_lists

WEBAPP_DIR = str(resource_dir() / "webapp")

# Accepted image attachment MIME types -> canonical file extension. Clients send
# base64 (optionally as a data: URL); the server saves the bytes and hands the
# path to the Copilot CLI via --attachment.
_IMAGE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(;base64)?,", re.IGNORECASE)
# Prompt sent to Copilot when the client attached image(s) but typed no text.
_DEFAULT_IMAGE_PROMPT = "Please look at the attached image(s) and respond."


def _authorized(request: web.Request, token: str, allow_query: bool = False) -> bool:
    if not token:
        return True
    provided = request.headers.get("X-API-Key")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    # Image <img>/download requests can't set custom headers, so the uploads route
    # opts in to a ?key=/?token= query-string fallback.
    if not provided and allow_query:
        provided = (request.query.get("key") or request.query.get("token")
                    or request.query.get("api_key"))
    return provided == token


def setup_web_routes(app: web.Application, config, runner):
    """Register the chat API + static web app routes on an existing aiohttp app."""
    jobs: dict = {}
    locks: dict = {}
    # Per-conversation FIFO of pending+running ticket ids (head = currently running).
    # Multiple clients posting to the same session are queued here so Copilot only
    # runs one turn per session at a time (concurrent `--session-id` runs would
    # corrupt the conversation), and each waiter can see its position in line.
    queues: dict = {}

    # Request authorization: shared API key and/or Microsoft Entra ID sign-in,
    # per config.AUTH_MODE (default 'apikey' = unchanged legacy behaviour).
    from auth import Authenticator
    authn = Authenticator(config)

    def _authorized(request: web.Request, token=None, allow_query: bool = False) -> bool:
        # ``token`` is accepted (and ignored) so the many existing call sites that
        # pass config.CHAT_API_TOKEN stay unchanged; the Authenticator reads config.
        return authn.check(request, allow_query=allow_query)

    sessions_dir = getattr(config, "SESSIONS_DIR", "") or str(app_base_dir() / "sessions")
    store = SessionStore(sessions_dir)

    # Client-uploaded images live UNDER the Copilot working directory (so they're
    # inside --add-dir and the CLI can read them for --attachment), one folder per
    # session: <workdir>/.uploads/<sessionId>/<uuid>.<ext>.
    uploads_root = os.path.join(os.path.abspath(runner.workdir), ".uploads")
    max_attachment_bytes = max(1, getattr(config, "MAX_ATTACHMENT_MB", 25)) * 1024 * 1024
    max_attachments = max(0, getattr(config, "MAX_ATTACHMENTS", 8))

    def _safe_seg(seg) -> str:
        """Return a path-safe single path segment or raise ValueError.

        Rejects separators, '..' and NUL so a client-supplied session id / file
        name can never escape ``uploads_root``.
        """
        if not isinstance(seg, str):
            raise ValueError("segment must be a string")
        s = seg.strip()
        if not s or "\x00" in s or ".." in s or "/" in s or "\\" in s:
            raise ValueError(f"unsafe path segment: {seg!r}")
        if os.sep in s or (os.altsep and os.altsep in s):
            raise ValueError(f"unsafe path segment: {seg!r}")
        return s

    def save_images(sid: str, images) -> tuple[list[str], list[dict]]:
        """Decode + persist client-sent images for session ``sid``.

        Returns ``(paths, metas)``: absolute file paths to feed to the Copilot CLI
        via --attachment, and metadata dicts to record on the transcript message.
        Raises ValueError on malformed, oversized, or too-many inputs.
        """
        if not images:
            return [], []
        if not isinstance(images, list):
            raise ValueError("images must be a list")
        if len(images) > max_attachments:
            raise ValueError(f"too many attachments (max {max_attachments})")
        sid = _safe_seg(sid)
        dest_dir = os.path.join(uploads_root, sid)
        paths: list[str] = []
        metas: list[dict] = []
        for item in images:
            if not isinstance(item, dict):
                raise ValueError("each image must be an object")
            raw = item.get("data")
            if raw is None:
                raw = item.get("dataUrl") or item.get("dataURL") or item.get("base64")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("image is missing base64 data")
            mime = (item.get("mime") or item.get("type") or "").strip().lower()
            # Strip a data: URL prefix if present and adopt its mime when absent.
            m = _DATA_URL_RE.match(raw)
            if m:
                if not mime and m.group(1):
                    mime = m.group(1).strip().lower()
                raw = raw[m.end():]
            raw = raw.strip().replace("\n", "").replace("\r", "")
            try:
                blob = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("image data is not valid base64")
            if not blob:
                raise ValueError("image is empty")
            if len(blob) > max_attachment_bytes:
                raise ValueError(f"image exceeds the {config.MAX_ATTACHMENT_MB} MB limit")
            if not mime:
                mime = "image/png"
            if mime not in _IMAGE_EXT:
                raise ValueError(f"unsupported image type: {mime}")
            ext = _IMAGE_EXT[mime]
            fname = uuid.uuid4().hex + ext
            os.makedirs(dest_dir, exist_ok=True)
            with open(os.path.join(dest_dir, fname), "wb") as fh:
                fh.write(blob)
            name = (item.get("name") or "").strip() or ("image" + ext)
            paths.append(os.path.join(dest_dir, fname))
            metas.append({
                "name": name, "mime": mime, "size": len(blob), "kind": "image",
                "url": f"/api/uploads/{sid}/{fname}",
            })
        return paths, metas

    def lock_for(conversation_id: str) -> asyncio.Lock:
        lock = locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[conversation_id] = lock
        return lock

    def queue_for(conversation_id: str) -> "collections.deque":
        q = queues.get(conversation_id)
        if q is None:
            q = collections.deque()
            queues[conversation_id] = q
        return q

    def queue_position(conversation_id: str, ticket: str):
        """Return (position, length): position 0 == running/your turn, N == N ahead."""
        q = queues.get(conversation_id)
        if not q:
            return 0, 0
        try:
            return q.index(ticket), len(q)
        except ValueError:
            return 0, len(q)

    def _gc():
        if len(jobs) <= 300:
            return
        done = [k for k, v in jobs.items() if v.get("status") == "done"]
        for key in sorted(done, key=lambda k: jobs[k].get("createdAt", 0))[:150]:
            jobs.pop(key, None)

    def _parse(body):
        message = (body.get("message") or "").strip()
        conversation_id = (body.get("conversationId") or "webapp").strip()
        reset = bool(body.get("reset"))
        images = body.get("images") or []
        return message, conversation_id, reset, images

    async def chat_start(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        message, conversation_id, reset, images = _parse(body)
        if not message and not images:
            return web.json_response({"error": "empty message"}, status=400)

        # reset => start a brand-new server-owned session whose fresh uuid REPLACES
        # the sent conversationId. There is no reset-in-place: a new conversation is
        # always a new session id. The new id is returned below so the client adopts
        # it for subsequent turns.
        try:
            session = store.create() if reset else store.get_or_create(conversation_id)
        except ValueError:
            return web.json_response({"error": "invalid conversationId"}, status=400)
        sid = session["id"]
        title = session.get("title", "")

        # Persist any attached images under this session before queueing the turn.
        try:
            attach_paths, attach_metas = save_images(sid, images)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        # An image-only turn still needs a textual prompt for the CLI.
        prompt = message or _DEFAULT_IMAGE_PROMPT

        # Enqueue this turn. The user message is appended INSIDE the lock (below) so
        # that when several clients post to the same session concurrently the stored
        # transcript stays correctly ordered (user, assistant, user, assistant) and
        # Copilot never runs two turns on the same session at once.
        job_id = uuid.uuid4().hex
        q = queue_for(sid)
        q.append(job_id)
        position = q.index(job_id)
        jobs[job_id] = {
            "status": "running" if position == 0 else "queued",
            "queuePosition": position,
            "queueLength": len(q),
            "createdAt": time.time(),
            "conversationId": sid, "sessionId": sid, "title": title,
        }

        async def _run():
            try:
                async with lock_for(sid):
                    jobs[job_id]["status"] = "running"
                    jobs[job_id]["queuePosition"] = 0
                    store.append_message(sid, "user", message, attachments=attach_metas)
                    result = await runner.run(prompt, session_id=sid, attachments=attach_paths)
                    updated = store.append_message(
                        sid, "assistant", result.text or "(no output)",
                        ok=result.ok, exit_code=result.exit_code,
                    )
                    jobs[job_id].update(
                        status="done", ok=result.ok, reply=result.text or "(no output)",
                        exitCode=result.exit_code, timedOut=result.timed_out,
                        title=updated.get("title", ""),
                    )
            except Exception as exc:  # noqa: BLE001
                jobs[job_id].update(status="done", ok=False, reply=f"Error running Copilot: {exc}")
            finally:
                try:
                    q.remove(job_id)
                except ValueError:
                    pass

        asyncio.create_task(_run())
        _gc()
        return web.json_response({
            "jobId": job_id, "status": jobs[job_id]["status"],
            "queuePosition": jobs[job_id]["queuePosition"], "queueLength": len(q),
            "conversationId": sid, "sessionId": sid, "title": title,
        })

    async def chat_status(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        job_id = request.match_info["job_id"]
        job = jobs.get(job_id)
        if not job:
            return web.json_response({"error": "not found"}, status=404)
        # Recompute the live queue position while the job is still pending/running.
        if job.get("status") != "done":
            pos, length = queue_position(job.get("conversationId", ""), job_id)
            job["queuePosition"] = pos
            job["queueLength"] = length
            job["status"] = "queued" if pos > 0 else "running"
        return web.json_response(job)

    async def chat_sync(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        message, conversation_id, reset, images = _parse(body)
        if not message and not images:
            return web.json_response({"error": "empty message"}, status=400)
        # reset mints a fresh session id that replaces the sent conversationId
        # (see chat_start); the new id is returned so the client can adopt it.
        try:
            session = store.create() if reset else store.get_or_create(conversation_id)
        except ValueError:
            return web.json_response({"error": "invalid conversationId"}, status=400)
        sid = session["id"]
        try:
            attach_paths, attach_metas = save_images(sid, images)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        prompt = message or _DEFAULT_IMAGE_PROMPT
        # Take a queue slot so this synchronous turn is serialized with (and counted
        # by) any concurrent /api/chat jobs on the same session. The user message is
        # appended inside the lock for correct transcript ordering under concurrency.
        ticket = "sync-" + uuid.uuid4().hex
        q = queue_for(sid)
        q.append(ticket)
        try:
            async with lock_for(sid):
                store.append_message(sid, "user", message, attachments=attach_metas)
                result = await runner.run(prompt, session_id=sid, attachments=attach_paths)
                session = store.append_message(
                    sid, "assistant", result.text or "(no output)",
                    ok=result.ok, exit_code=result.exit_code,
                )
        finally:
            try:
                q.remove(ticket)
            except ValueError:
                pass
        return web.json_response({
            "ok": result.ok, "reply": result.text or "(no output)",
            "exitCode": result.exit_code, "timedOut": result.timed_out,
            "conversationId": sid, "sessionId": sid, "title": session.get("title", ""),
        })

    async def webconfig(request: web.Request) -> web.Response:  # noqa: ARG001
        return web.json_response(authn.describe())

    async def sessions_create(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - body is optional for create
            body = {}
        title = (body.get("title") or "").strip() if isinstance(body, dict) else ""
        session = store.create(title=title)
        return web.json_response(store.summarize(session), status=201)

    async def sessions_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(store.list())

    async def sessions_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        session = store.get(request.match_info["id"])
        if session is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(session)

    async def sessions_sync(request: web.Request) -> web.Response:
        """Reconcile a session with the Copilot CLI's own on-disk transcript, then
        return the full merged session.

        Talking to ``copilot`` directly on the host records turns under
        ``~/.copilot/session-state/<id>/`` that never went through the bridge, so
        the web/mobile clients would otherwise miss them. A client calls this when
        it (re)opens a session: we pull the CLI's native transcript (bridge id ==
        Copilot ``--session-id``), merge any new/divergent turns INTO the
        server-owned store, persist, and hand back the reconciled session. The
        merge is a lossless union, so the server stays the single source of truth.
        """
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        cid = request.match_info["id"]
        try:
            native = copilot_sessions.read_session(cid)
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)

        bridge = store.get(cid)
        if bridge is None and native is None:
            return web.json_response({"error": "not found"}, status=404)
        if native is None:
            # No native transcript to reconcile against; return the store as-is.
            return web.json_response(bridge)

        native_msgs = native.get("messages", [])
        if bridge is None:
            # First contact: adopt the CLI conversation under the same id.
            try:
                session = store.replace_messages(
                    cid, native_msgs, title=native.get("title", ""),
                    updated_at=native.get("updatedAt"))
            except ValueError:
                return web.json_response({"error": "invalid id"}, status=400)
            return web.json_response(session)

        merged, changed = merge_message_lists(bridge.get("messages", []), native_msgs)
        if not changed:
            return web.json_response(bridge)
        session = store.replace_messages(
            cid, merged, title=native.get("title", ""), updated_at=native.get("updatedAt"))
        return web.json_response(session)

    async def sessions_delete(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        if store.delete(request.match_info["id"]):
            return web.json_response({"deleted": True})
        return web.json_response({"error": "not found"}, status=404)

    async def sessions_rename(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        title = (body.get("title") or "").strip()
        session = store.rename(request.match_info["id"], title)
        if session is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(store.summarize(session))

    def _file(name, content_type=None, headers=None):
        async def handler(request: web.Request):  # noqa: ARG001
            path = os.path.join(WEBAPP_DIR, name)
            if not os.path.isfile(path):
                return web.Response(status=404, text="not found")
            return web.FileResponse(path, headers=headers)
        return handler

    async def _index(request: web.Request):  # noqa: ARG001
        """Serve index.html with the host's API key injected.

        In the MS edition the Dev Tunnel relay enforces a Microsoft sign-in
        (owner-only) BEFORE any request reaches this server, so whoever loads this
        page has already been authenticated by Microsoft. We therefore hand the
        per-host API key to the page (``window.__CB_KEY``) so the web app works
        immediately — no manual API-token entry. The key never leaves the
        Microsoft-authenticated tunnel.
        """
        path = os.path.join(WEBAPP_DIR, "index.html")
        if not os.path.isfile(path):
            return web.Response(status=404, text="not found")
        try:
            html = open(path, encoding="utf-8").read()
            key = config.CHAT_API_TOKEN or ""
            # Only hand the key to the page when the tunnel itself enforces a
            # Microsoft sign-in (private/tenant/org). In 'anonymous' mode the key is
            # the only gate, so embedding it would defeat it — require manual entry.
            tunnel_auth = (getattr(config, "TUNNEL_AUTH", "private") or "private").strip().lower()
            if key and tunnel_auth != "anonymous":
                inject = ("<script>window.__CB_KEY="
                          + json.dumps(key) + ";</script>")
                html = html.replace("</head>", inject + "</head>", 1)
            return web.Response(text=html, content_type="text/html",
                                charset="utf-8")
        except OSError:
            return web.FileResponse(path)

    # ----- Copilot CLI native sessions (read-only discovery + import) -----

    async def copilot_sessions_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(copilot_sessions.list_sessions())

    async def copilot_session_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            data = copilot_sessions.read_session(request.match_info["id"])
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)
        if data is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(data)

    async def copilot_session_import(request: web.Request) -> web.Response:
        """Copy a Copilot CLI session into the bridge store under the SAME id, so it
        appears in the normal session list and the next turn resumes the real
        Copilot conversation (bridge id == Copilot --session-id). Idempotent."""
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        cid = request.match_info["id"]
        try:
            data = copilot_sessions.read_session(cid)
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)
        if data is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            session = store.get_or_create(cid)
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)
        # Only populate on first import so re-importing doesn't duplicate messages.
        if not session.get("messages"):
            for m in data.get("messages", []):
                store.append_message(cid, m.get("role") or "user", m.get("text") or "")
            if data.get("title"):
                store.rename(cid, data["title"])
            session = store.get(cid) or session
        return web.json_response(store.summarize(session), status=201)

    # ----- Client-uploaded image serving -----

    async def uploads_get(request: web.Request) -> web.Response:
        """Serve a previously uploaded image so clients can render thumbnails.

        Auth accepts the usual X-API-Key header OR a ?key=/?token= query param so
        plain <img> tags (which can't set headers) work.
        """
        if not _authorized(request, config.CHAT_API_TOKEN, allow_query=True):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            sid = _safe_seg(request.match_info["sid"])
            name = _safe_seg(request.match_info["name"])
        except ValueError:
            return web.json_response({"error": "invalid path"}, status=400)
        root = os.path.abspath(uploads_root)
        path = os.path.abspath(os.path.join(root, sid, name))
        # Belt-and-suspenders: the resolved file must live under uploads_root.
        if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
            return web.json_response({"error": "not found"}, status=404)
        return web.FileResponse(path)

    # API
    app.router.add_post("/api/chat", chat_start)
    app.router.add_get("/api/chat/{job_id}", chat_status)
    app.router.add_post("/api/chat-sync", chat_sync)
    app.router.add_get("/api/webconfig", webconfig)
    app.router.add_get("/api/uploads/{sid}/{name}", uploads_get)

    # Session management
    app.router.add_post("/api/sessions", sessions_create)
    app.router.add_get("/api/sessions", sessions_list)
    app.router.add_get("/api/sessions/{id}", sessions_get)
    app.router.add_post("/api/sessions/{id}/sync", sessions_sync)
    app.router.add_delete("/api/sessions/{id}", sessions_delete)
    app.router.add_patch("/api/sessions/{id}", sessions_rename)

    # Copilot CLI native session discovery + import
    app.router.add_get("/api/copilot-sessions", copilot_sessions_list)
    app.router.add_get("/api/copilot-sessions/{id}", copilot_session_get)
    app.router.add_post("/api/copilot-sessions/{id}/import", copilot_session_import)

    # Web app (explicit files only - no directory listing)
    app.router.add_get("/", _index)
    app.router.add_get("/app", _index)
    app.router.add_get("/index.html", _index)
    app.router.add_get("/manifest.webmanifest", _file("manifest.webmanifest"))
    app.router.add_get("/sw.js", _file("sw.js", headers={"Service-Worker-Allowed": "/"}))
    app.router.add_get("/icon.svg", _file("icon.svg"))
    # Bundled MSAL.js (browser) for the optional Microsoft Entra ID sign-in.
    app.router.add_get("/vendor/msal-browser.min.js", _file("vendor/msal-browser.min.js"))
