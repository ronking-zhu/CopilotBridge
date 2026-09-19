"""Direct mobile/web chat API for the Copilot bridge.

Adds a plain-JSON chat API (no Bot Framework needed) plus serves the mobile web app.
Designed to be reached directly from a phone through a Dev Tunnel:

    POST /api/chat        {message, conversationId, reset?, images?}  -> {jobId, conversationId, sessionId, title}
    GET  /api/chat/{id}                                      -> {status, reply, sessionId, ...}
    POST /api/chat-sync   {message, conversationId, reset?, images?}  -> {reply, sessionId, title, ...}  (waits)
    GET  /api/webconfig                                      -> {authRequired}
    GET/PATCH /api/settings                                  -> user settings
    GET  /api/dashboard                                     -> inbox + active job summary
    GET/POST /api/knowledge                                 -> knowledge list / create
    GET/PATCH/DELETE /api/knowledge/{id}                    -> versioned knowledge CRUD
    POST /api/knowledge/{id}/versions                       -> append an evidence-backed version
    GET/PATCH /api/inbox*                                   -> persistent attention inbox
    POST   /api/sessions  {title?}                           -> 201 session summary
    GET    /api/sessions                                     -> [session summaries]
    GET    /api/sessions/{id}                                -> full session (with messages)
    GET    /api/sessions/{id}/turns                          -> prompt timeline
    POST   /api/sessions/{id}/sync                           -> reconcile with the Copilot CLI's
                                                                 own transcript, then full session
    DELETE /api/sessions/{id}                                -> {deleted: true}
    PATCH  /api/sessions/{id} {title,isFavorite,isPinned,project,labels}
                                                               -> session summary
    GET  /api/copilot-sessions                               -> [Copilot CLI native session summaries]
    GET  /api/copilot-sessions/{id}                          -> full Copilot session (with messages)
    POST /api/copilot-sessions/{id}/import                   -> import into bridge store (201 summary)
    GET  /api/uploads/{sid}/{name}                           -> a client-uploaded image (auth via header or ?key=)
    GET  /                                                   -> mobile web app

Clients may attach images to a turn via ``images: [{name, mime, data}]`` (``data`` is
base64 or a data: URL). The server saves them under ``<workdir>/.uploads/<sid>/`` and
passes each to the Copilot CLI through its native ``--attachment`` flag; the saved
files are recorded on the user message as ``attachments`` so they re-render on reload.

The conversationId a client sends is the stable Bridge conversation id. A local
execution binding maps it to a provider-native session id, so VS Code, Copilot CLI,
and Bridge ids cannot be confused. A shared token (CHAT_API_TOKEN) protects the API
since the tunnel is public.
"""

import asyncio
import base64
import binascii
import collections
from functools import partial
import json
import os
import re
import time
import uuid

from aiohttp import web

import copilot_sessions
from knowledge_extractor import (
    KnowledgeExtractionError, parse_extraction_output, parse_mind_map_output,
)
from paths import app_base_dir, resource_dir
from session_store import SessionStore, merge_message_lists
from session_watcher import SessionWatcher

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


def setup_web_routes(app: web.Application, config, runner, knowledge_extractor=None):
    """Register the chat API + static web app routes on an existing aiohttp app."""
    jobs: dict = {}
    locks: dict = {}
    # Per-conversation FIFO of pending+running ticket ids (head = currently running).
    # Multiple clients posting to the same session are queued here so Copilot only
    # runs one turn per session at a time (concurrent `--session-id` runs would
    # corrupt the conversation), and each waiter can see its position in line.
    queues: dict = {}
    knowledge_locks: dict = {}
    knowledge_map_lock = asyncio.Lock()
    knowledge_generation_tasks: dict[str, asyncio.Task] = {}

    if knowledge_extractor is None:
        from knowledge_extractor import UnavailableKnowledgeExtractor
        knowledge_extractor = UnavailableKnowledgeExtractor(
            "knowledge extractor was not configured"
        )

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
    provider_name = (getattr(runner, "name", "") or "copilot").strip().lower()
    watcher_enabled = bool(getattr(config, "SESSION_WATCHER_ENABLED", False))
    watcher = SessionWatcher(
        store,
        interval=getattr(config, "SESSION_WATCHER_INTERVAL", 5.0),
        settle_scans=getattr(config, "SESSION_WATCHER_SETTLE_SCANS", 1),
    )
    sync_service = None
    if bool(getattr(config, "ONEDRIVE_SYNC_ENABLED", False)):
        from onedrive_auth import OneDriveAuth
        from onedrive_local import LocalOneDriveConnection, detect_onedrive_root
        from sync.engine import SyncEngine
        from sync.service import SyncService
        from transports.filesystem import FileSystemTransport
        from transports.onedrive import OneDriveGraphTransport

        sync_mode = getattr(config, "ONEDRIVE_TRANSPORT", "auto") or "auto"
        client_id = getattr(config, "ONEDRIVE_CLIENT_ID", "")
        use_graph = sync_mode == "graph" or (sync_mode == "auto" and bool(client_id))
        if use_graph:
            sync_auth = OneDriveAuth(
                client_id,
                tenant_id=getattr(config, "ONEDRIVE_TENANT_ID", "common"),
            )
            sync_transport = OneDriveGraphTransport(sync_auth)
        else:
            local_root = detect_onedrive_root(
                getattr(config, "ONEDRIVE_LOCAL_ROOT", "")
            )
            if local_root is not None:
                sync_auth = LocalOneDriveConnection(local_root)
                sync_transport = FileSystemTransport(str(sync_auth.sync_root))
            else:
                # Keep a useful not-configured state when no signed-in local
                # OneDrive or publisher Graph registration is available.
                sync_auth = OneDriveAuth("")
                sync_transport = OneDriveGraphTransport(sync_auth)
        sync_engine = SyncEngine(
            store, sync_transport,
            space_id=getattr(config, "ONEDRIVE_SYNC_SPACE_ID", "default"),
        )
        sync_service = SyncService(
            store, sync_engine, sync_auth,
            native_list=copilot_sessions.list_sessions,
            native_read=copilot_sessions.read_session_key,
            interval=getattr(config, "ONEDRIVE_SYNC_INTERVAL", 900),
        )

    def _adapter_for_native_source(source: str) -> str:
        return {
            "cli": "copilot-cli",
            "vscode-insiders": "vscode-insiders-copilot-chat",
            "vscode": "vscode-copilot-chat",
        }.get(source, f"{source}-copilot-chat")

    def _native_source_for_adapter(adapter_id: str) -> str:
        return {
            "copilot-cli": "cli",
            "vscode-insiders-copilot-chat": "vscode-insiders",
            "vscode-copilot-chat": "vscode",
        }.get(adapter_id, "")

    app["session_store"] = store
    app["session_watcher"] = watcher
    app["sync_service"] = sync_service
    app["knowledge_generation_tasks"] = knowledge_generation_tasks

    async def _start_services(_app: web.Application) -> None:
        if watcher_enabled:
            _app["session_watcher_task"] = asyncio.create_task(watcher.run())
        if sync_service is not None:
            _app["sync_service_task"] = asyncio.create_task(sync_service.run())

    async def _close_store(_app: web.Application) -> None:
        watcher.stop()
        task = _app.get("session_watcher_task")
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if sync_service is not None:
            await sync_service.close()
            sync_task = _app.get("sync_service_task")
            if sync_task is not None:
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
        generation_tasks = list(knowledge_generation_tasks.values())
        for generation_task in generation_tasks:
            generation_task.cancel()
        if generation_tasks:
            await asyncio.gather(*generation_tasks, return_exceptions=True)
        store.close()

    app.on_startup.append(_start_services)
    app.on_cleanup.append(_close_store)

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

    def _inbox_summary(text: str, limit: int = 240) -> str:
        collapsed = " ".join((text or "").split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit - 3].rstrip() + "..."

    def _record_job_inbox(job_id: str, sid: str, title: str,
                          reply: str, ok: bool) -> str:
        item, _created = store.create_inbox_item(
            dedupe_key=f"bridge-job:{job_id}",
            source="bridge",
            source_key=f"bridge:{sid}",
            conversation_id=sid,
            title=title or "Untitled conversation",
            summary=_inbox_summary(reply),
            metadata={
                "jobId": job_id,
                "provider": provider_name,
                "runState": "completed" if ok else "failed",
            },
        )
        return item["id"]

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
                    prior_history = list((store.get(sid) or {}).get("messages", []))
                    binding = store.get_or_create_execution_binding(sid, provider_name)
                    store.append_message(sid, "user", message, attachments=attach_metas)
                    result = await runner.run(
                        prompt, session_id=binding["nativeSessionId"],
                        attachments=attach_paths, history=prior_history,
                    )
                    updated = store.append_message(
                        sid, "assistant", result.text or "(no output)",
                        ok=result.ok, exit_code=result.exit_code,
                    )
                    jobs[job_id].update(
                        status="done", ok=result.ok, reply=result.text or "(no output)",
                        exitCode=result.exit_code, timedOut=result.timed_out,
                        title=updated.get("title", ""),
                    )
                    jobs[job_id]["inboxItemId"] = _record_job_inbox(
                        job_id, sid, updated.get("title", ""),
                        result.text or "(no output)", result.ok,
                    )
            except Exception as exc:  # noqa: BLE001
                reply = f"Error running Copilot: {exc}"
                jobs[job_id].update(status="done", ok=False, reply=reply)
                try:
                    jobs[job_id]["inboxItemId"] = _record_job_inbox(
                        job_id, sid, jobs[job_id].get("title", ""), reply, False,
                    )
                except Exception:  # noqa: BLE001 - preserve the original job failure
                    pass
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
                prior_history = list((store.get(sid) or {}).get("messages", []))
                binding = store.get_or_create_execution_binding(sid, provider_name)
                store.append_message(sid, "user", message, attachments=attach_metas)
                result = await runner.run(
                    prompt, session_id=binding["nativeSessionId"],
                    attachments=attach_paths, history=prior_history,
                )
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
        return web.json_response(authn.describe(), headers={"Cache-Control": "no-store"})

    async def settings_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response(store.get_settings())

    async def settings_update(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            settings = store.update_settings(body)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001 - malformed JSON
            return web.json_response({"error": "invalid json"}, status=400)
        return web.json_response(settings)

    async def dashboard_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        active_jobs = []
        for job_id, job in jobs.items():
            if job.get("status") not in ("queued", "running"):
                continue
            active_jobs.append({
                "jobId": job_id,
                "status": job.get("status"),
                "conversationId": job.get("conversationId"),
                "title": job.get("title") or "Untitled conversation",
                "createdAt": job.get("createdAt"),
                "queuePosition": job.get("queuePosition", 0),
            })
        active_jobs.sort(key=lambda item: item.get("createdAt") or 0, reverse=True)
        watcher_info = watcher.describe()
        watcher_info["enabled"] = watcher_enabled
        return web.json_response({
            "unreadCount": store.inbox_unread_count(),
            "runningJobCount": len(active_jobs),
            "activeJobs": active_jobs,
            "inbox": store.list_inbox_items(limit=100),
            "watcher": watcher_info,
            "sync": sync_service.describe() if sync_service is not None else {
                "enabled": False,
                "running": False,
                "auth": {"configured": False, "connected": False},
            },
        })

    def _sync_unavailable() -> web.Response:
        return web.json_response(
            {"error": "OneDrive synchronization is disabled", "enabled": False},
            status=503,
        )

    async def sync_status(request: web.Request, *, control: bool = False) -> web.Response:
        if not (authn.check_control(request, host_key_only=True) if control else _authorized(request)):
            return web.json_response({"error": "unauthorized"}, status=401)
        if sync_service is None:
            return web.json_response({
                "enabled": False,
                "running": False,
                "auth": {"configured": False, "connected": False},
            })
        return web.json_response(sync_service.describe())

    async def sync_connect(request: web.Request, *, control: bool = False) -> web.Response:
        if not (authn.check_control(request, host_key_only=True) if control else _authorized(request)):
            return web.json_response({"error": "unauthorized"}, status=401)
        if sync_service is None:
            return _sync_unavailable()
        try:
            return web.json_response(await sync_service.connect())
        except Exception as exc:  # noqa: BLE001 - return actionable auth/Graph error
            return web.json_response({"error": str(exc)}, status=502)

    async def sync_run(request: web.Request, *, control: bool = False) -> web.Response:
        if not (authn.check_control(request, host_key_only=True) if control else _authorized(request)):
            return web.json_response({"error": "unauthorized"}, status=401)
        if sync_service is None:
            return _sync_unavailable()
        try:
            return web.json_response(await sync_service.sync_now())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

    async def sync_disconnect(request: web.Request, *, control: bool = False) -> web.Response:
        if not (authn.check_control(request, host_key_only=True) if control else _authorized(request)):
            return web.json_response({"error": "unauthorized"}, status=401)
        if sync_service is None:
            return _sync_unavailable()
        try:
            return web.json_response(await sync_service.disconnect())
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)

    async def inbox_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        raw_status = request.query.get("status", "")
        statuses = tuple(value.strip() for value in raw_status.split(",") if value.strip())
        allowed = {"unread", "seen", "completed", "ignored"}
        if any(status not in allowed for status in statuses):
            return web.json_response({"error": "invalid inbox status"}, status=400)
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            return web.json_response({"error": "invalid limit"}, status=400)
        return web.json_response({
            "unreadCount": store.inbox_unread_count(),
            "items": store.list_inbox_items(statuses=statuses, limit=limit),
        })

    async def inbox_update(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            item = store.update_inbox_item(
                request.match_info["id"], str(body.get("status") or "")
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if item is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item)

    async def inbox_mark_all_seen(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"updated": store.mark_all_inbox_seen()})

    async def inbox_complete_all(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"updated": store.complete_all_inbox_items()})

    async def inbox_remind(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            if "remindAt" in body:
                remind_at = float(body["remindAt"])
            else:
                minutes = int(body.get("minutes", 60))
                if minutes < 1 or minutes > 525600:
                    raise ValueError("minutes must be between 1 and 525600")
                remind_at = time.time() + minutes * 60
            item = store.remind_inbox_item(request.match_info["id"], remind_at)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if item is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item)

    async def inbox_scan(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        result = await watcher.scan_once()
        return web.json_response({
            "sessionsSeen": result.sessions_seen,
            "baselined": result.baselined,
            "notificationsCreated": result.notifications_created,
            "errors": result.errors,
            "unreadCount": store.inbox_unread_count(),
        })

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

    async def history_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        items = [{
            **session,
            "conversationId": session["id"],
            "source": "bridge",
            "sourceKey": "",
            "importable": False,
        } for session in store.list()]
        for native in copilot_sessions.list_sessions():
            source = str(native.get("source") or "cli")
            native_id = str(native.get("nativeId") or native.get("id") or "")
            if not native_id:
                continue
            adapter_id = _adapter_for_native_source(source)
            if store.find_by_external_ref(adapter_id, native_id) is not None:
                continue
            items.append({
                **native,
                "conversationId": "",
                "sourceKey": native.get("sourceKey") or f"{source}:{native_id}",
                "machineId": store.device_id,
                "machineName": store.device_name,
                "importable": True,
            })
        items.sort(key=lambda item: float(item.get("updatedAt") or 0), reverse=True)
        represented: dict[str, str] = {}
        for item in items:
            machine_id = str(item.get("machineId") or "")
            if machine_id:
                represented[machine_id] = str(item.get("machineName") or machine_id[:8])
        devices = {item["id"]: item for item in store.list_devices()}
        machines = [{
            "id": machine_id,
            "name": name,
            "isLocal": bool(devices.get(machine_id, {}).get("isLocal")),
        } for machine_id, name in represented.items()]
        machines.sort(key=lambda item: (not item["isLocal"], item["name"].lower(), item["id"]))
        return web.json_response({"machines": machines, "items": items})

    async def sessions_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        session = store.get(request.match_info["id"])
        if session is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(session)

    async def sessions_turns(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        cid = request.match_info["id"]
        if store.get(cid) is None:
            return web.json_response({"error": "not found"}, status=404)
        raw_length = request.query.get("previewLength")
        try:
            preview_length = int(raw_length) if raw_length is not None else None
            turns = store.list_turns(cid, preview_length=preview_length)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(turns)

    async def knowledge_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            items = store.list_knowledge_items(
                query=request.query.get("query", ""),
                status=request.query.get("status", ""),
                knowledge_type=request.query.get("type", ""),
                project=request.query.get("project", ""),
                conversation_id=request.query.get("conversationId", ""),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"items": items, "count": len(items)})

    async def session_knowledge_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        conversation_id = request.match_info["id"]
        if store.get(conversation_id) is None:
            return web.json_response({"error": "not found"}, status=404)
        items = store.list_knowledge_items(conversation_id=conversation_id)
        return web.json_response({"items": items, "count": len(items)})

    async def _generate_knowledge_map(
        *, force: bool, max_conversations: int, focus_conversation_id: str = "",
    ) -> dict:
        async with knowledge_map_lock:
            map_version = getattr(
                knowledge_extractor, "map_extractor_version",
                f"{knowledge_extractor.extractor_version}:map-v1",
            )
            prepared = store.prepare_knowledge_map(
                map_version,
                max_chars=min(
                    getattr(config, "KNOWLEDGE_MAP_MAX_CHARS", 22000),
                    getattr(knowledge_extractor, "max_map_input_chars", 22000),
                ),
                max_conversations=max_conversations,
                focus_conversation_id=focus_conversation_id,
            )
            if not prepared["conversations"]:
                raise ValueError("没有可用于生成思维导图的会话历史")
            if prepared["cached"] and not force:
                return {
                    "map": prepared["cachedMap"], "cached": True,
                    "selectedConversationCount": prepared["selectedConversationCount"],
                    "eligibleConversationCount": prepared["eligibleConversationCount"],
                    "extractor": knowledge_extractor.describe(),
                }
            for attempt in range(2):
                try:
                    raw = await knowledge_extractor.extract_map(
                        prepared["conversations"]
                    )
                    payload = parse_mind_map_output(
                        raw, set(prepared["sourceMessageIds"]),
                        max_nodes=getattr(knowledge_extractor, "max_map_nodes", 48),
                    )
                    break
                except KnowledgeExtractionError:
                    if attempt == 1:
                        raise
            mind_map = store.save_knowledge_map(
                payload, input_digest=prepared["inputDigest"],
                extractor_version=map_version,
                source_message_ids=prepared["sourceMessageIds"],
                source_conversation_count=prepared["selectedConversationCount"],
                map_id=prepared["mapId"],
            )
            if prepared["mapId"] != "history":
                store.save_knowledge_map(
                    payload, input_digest=prepared["inputDigest"],
                    extractor_version=map_version,
                    source_message_ids=prepared["sourceMessageIds"],
                    source_conversation_count=prepared["selectedConversationCount"],
                )
            return {
                "map": mind_map, "cached": False,
                "selectedConversationCount": prepared["selectedConversationCount"],
                "eligibleConversationCount": prepared["eligibleConversationCount"],
                "extractor": knowledge_extractor.describe(),
            }

    async def knowledge_map_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        conversation_id = request.query.get("conversationId", "").strip()
        try:
            map_id = f"conversation-{store._norm_id(conversation_id)}" \
                if conversation_id else "history"
            mind_map = store.get_knowledge_map(map_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"map": mind_map})

    async def knowledge_map_generate(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not getattr(knowledge_extractor, "available", False):
            return web.json_response({
                "error": getattr(knowledge_extractor, "reason", "extractor unavailable"),
                "extractor": knowledge_extractor.describe(),
            }, status=503)
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            unknown = set(body) - {
                "force", "maxConversations", "focusConversationId",
            }
            if unknown:
                raise ValueError(
                    f"unknown mind map field(s): {', '.join(sorted(unknown))}"
                )
            force = body.get("force", False)
            if not isinstance(force, bool):
                raise ValueError("force must be a boolean")
            max_conversations = int(body.get(
                "maxConversations",
                getattr(config, "KNOWLEDGE_MAP_MAX_CONVERSATIONS", 48),
            ))
            focus_conversation_id = body.get("focusConversationId", "")
            if not isinstance(focus_conversation_id, str):
                raise ValueError("focusConversationId must be a string")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        try:
            result = await _generate_knowledge_map(
                force=force, max_conversations=max_conversations,
                focus_conversation_id=focus_conversation_id,
            )
            return web.json_response(result)
        except KnowledgeExtractionError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def knowledge_create(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            allowed = {
                "type", "title", "bodyMarkdown", "evidenceMessageIds", "status",
                "project", "labels", "confidence", "extractorVersion", "inputDigest",
            }
            unknown = set(body) - allowed
            if unknown:
                raise ValueError(
                    f"unknown knowledge field(s): {', '.join(sorted(unknown))}"
                )
            item = store.create_knowledge_item(
                knowledge_type=body.get("type"), title=body.get("title"),
                body_markdown=body.get("bodyMarkdown"),
                evidence_message_ids=body.get("evidenceMessageIds"),
                status=body.get("status", "draft"), project=body.get("project", ""),
                labels=body.get("labels") or [], confidence=body.get("confidence", ""),
                extractor_version=body.get("extractorVersion", "manual-v1"),
                input_digest=body.get("inputDigest", ""),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        return web.json_response(item, status=201)

    async def knowledge_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        item = store.get_knowledge_item(request.match_info["id"])
        if item is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item)

    async def knowledge_update(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            item = store.update_knowledge_item(request.match_info["id"], body)
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if item is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item)

    async def knowledge_add_version(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            allowed = {
                "bodyMarkdown", "evidenceMessageIds", "confidence",
                "extractorVersion", "inputDigest",
            }
            unknown = set(body) - allowed
            if unknown:
                raise ValueError(
                    f"unknown knowledge version field(s): {', '.join(sorted(unknown))}"
                )
            item = store.add_knowledge_version(
                request.match_info["id"], body_markdown=body.get("bodyMarkdown"),
                evidence_message_ids=body.get("evidenceMessageIds"),
                confidence=body.get("confidence", ""),
                extractor_version=body.get("extractorVersion", "manual-v1"),
                input_digest=body.get("inputDigest", ""),
            )
        except (ValueError, TypeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if item is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(item, status=201)

    async def knowledge_delete(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        if store.delete_knowledge_item(request.match_info["id"]):
            return web.json_response({"deleted": True})
        return web.json_response({"error": "not found"}, status=404)

    async def _extract_knowledge(conversation_id: str) -> dict:
        lock = knowledge_locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            batch = store.prepare_knowledge_extraction(
                conversation_id, knowledge_extractor.extractor_version,
                max_chars=min(
                    getattr(config, "KNOWLEDGE_EXTRACTION_MAX_CHARS", 45000),
                    getattr(knowledge_extractor, "max_input_chars", 45000),
                ),
                max_messages=getattr(config, "KNOWLEDGE_EXTRACTION_MAX_MESSAGES", 120),
            )
            if batch["noNewMessages"] or batch["cached"]:
                return {
                    "items": batch["cachedItems"], "cached": True,
                    "noNewMessages": batch["noNewMessages"],
                    "processedMessageCount": batch["processedMessageCount"],
                    "remainingMessageCount": batch["remainingMessageCount"],
                    "extractor": knowledge_extractor.describe(),
                }
            raw = await knowledge_extractor.extract(
                batch["messages"], batch["existingItems"]
            )
            candidates = parse_extraction_output(
                raw, set(batch["sourceMessageIds"]),
                max_items=getattr(
                    knowledge_extractor, "max_items",
                    max(1, min(int(getattr(
                        config, "KNOWLEDGE_EXTRACTION_MAX_ITEMS", 12
                    )), 30)),
                ),
            )
            result = store.apply_knowledge_extraction(
                conversation_id, knowledge_extractor.extractor_version,
                batch["inputDigest"], batch["sourceMessageIds"], candidates,
            )
            return {
                **result, "noNewMessages": False,
                "processedMessageCount": len(batch["sourceMessageIds"]),
                "remainingMessageCount": batch["remainingMessageCount"],
                "extractor": knowledge_extractor.describe(),
            }

    async def knowledge_extract(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        conversation_id = request.match_info["id"]
        if store.get(conversation_id) is None:
            return web.json_response({"error": "not found"}, status=404)
        if not getattr(knowledge_extractor, "available", False):
            return web.json_response({
                "error": getattr(knowledge_extractor, "reason", "extractor unavailable"),
                "extractor": knowledge_extractor.describe(),
            }, status=503)
        try:
            return web.json_response(await _extract_knowledge(conversation_id))
        except KnowledgeExtractionError as exc:
            return web.json_response({"error": str(exc)}, status=502)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def _run_knowledge_generation(
        conversation_id: str, max_conversations: int,
    ) -> None:
        generated_item_ids: set[str] = set()
        processed_message_count = 0
        remaining_message_count = 0
        try:
            store.update_knowledge_generation_job(
                conversation_id, status="running", phase="extracting"
            )
            for _batch_number in range(20):
                extraction = await _extract_knowledge(conversation_id)
                generated_item_ids.update(
                    item["id"] for item in extraction.get("items") or [] if item.get("id")
                )
                processed_message_count += int(
                    extraction.get("processedMessageCount") or 0
                )
                remaining_message_count = int(
                    extraction.get("remainingMessageCount") or 0
                )
                store.update_knowledge_generation_job(
                    conversation_id, status="running", phase="extracting",
                    processed_message_count=processed_message_count,
                    remaining_message_count=remaining_message_count,
                    generated_item_count=len(generated_item_ids),
                )
                if not remaining_message_count:
                    break
            if remaining_message_count:
                raise KnowledgeExtractionError(
                    "knowledge extraction exceeded the 20-batch safety limit"
                )
            store.update_knowledge_generation_job(
                conversation_id, status="running", phase="mapping",
                processed_message_count=processed_message_count,
                remaining_message_count=0,
                generated_item_count=len(generated_item_ids),
            )
            map_result = await _generate_knowledge_map(
                force=True, max_conversations=max_conversations,
                focus_conversation_id=conversation_id,
            )
            store.update_knowledge_generation_job(
                conversation_id, status="completed", phase="completed",
                processed_message_count=processed_message_count,
                remaining_message_count=0,
                generated_item_count=len(generated_item_ids),
                result_map_id=map_result["map"]["id"],
            )
        except asyncio.CancelledError:
            store.update_knowledge_generation_job(
                conversation_id, status="failed", phase="failed",
                processed_message_count=processed_message_count,
                remaining_message_count=remaining_message_count,
                generated_item_count=len(generated_item_ids),
                error="Generation cancelled during server shutdown.",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - status must retain background failures
            store.update_knowledge_generation_job(
                conversation_id, status="failed", phase="failed",
                processed_message_count=processed_message_count,
                remaining_message_count=remaining_message_count,
                generated_item_count=len(generated_item_ids), error=str(exc),
            )
        finally:
            current = asyncio.current_task()
            if knowledge_generation_tasks.get(conversation_id) is current:
                knowledge_generation_tasks.pop(conversation_id, None)

    async def knowledge_generation_start(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        conversation_id = request.match_info["id"]
        if store.get(conversation_id) is None:
            return web.json_response({"error": "not found"}, status=404)
        if not getattr(knowledge_extractor, "available", False):
            return web.json_response({
                "error": getattr(knowledge_extractor, "reason", "extractor unavailable"),
                "extractor": knowledge_extractor.describe(),
            }, status=503)
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            unknown = set(body) - {"maxConversations"}
            if unknown:
                raise ValueError(
                    f"unknown generation field(s): {', '.join(sorted(unknown))}"
                )
            max_conversations = int(body.get(
                "maxConversations",
                getattr(config, "KNOWLEDGE_MAP_MAX_CONVERSATIONS", 48),
            ))
            job, created = store.begin_knowledge_generation_job(
                conversation_id, max_conversations=max_conversations,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if created:
            knowledge_generation_tasks[conversation_id] = asyncio.create_task(
                _run_knowledge_generation(conversation_id, job["maxConversations"])
            )
        return web.json_response(
            {"accepted": created, "job": job}, status=202 if created else 200
        )

    async def knowledge_generation_get(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        conversation_id = request.match_info["id"]
        if store.get(conversation_id) is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({
            "job": store.get_knowledge_generation_job(conversation_id)
        })

    async def knowledge_generation_list(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        jobs_list = store.list_knowledge_generation_jobs()
        return web.json_response({"jobs": jobs_list, "count": len(jobs_list)})

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
        external_ref = store.external_ref_for_conversation(
            cid, (
                "copilot-cli", "vscode-insiders-copilot-chat",
                "vscode-copilot-chat",
            )
        )
        binding = store.execution_binding_for(cid, "copilot")
        native_id = (
            external_ref["nativeSessionId"] if external_ref
            else binding["nativeSessionId"] if binding
            else cid
        )
        try:
            native = copilot_sessions.read_session(
                native_id,
                source=_native_source_for_adapter(external_ref["adapterId"])
                if external_ref else "",
            )
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

    async def sessions_update(request: web.Request) -> web.Response:
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be an object"}, status=400)
        allowed = {"title", "isFavorite", "isPinned", "project", "labels"}
        unknown = set(body) - allowed
        if unknown:
            return web.json_response({
                "error": f"unknown session field(s): {', '.join(sorted(unknown))}"
            }, status=400)
        if not body:
            return web.json_response({"error": "at least one field is required"}, status=400)
        session_id = request.match_info["id"]
        session = store.get(session_id)
        if session is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            session = store.update_metadata(session_id, body)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
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
        """Serve index.html, injecting a key only for legacy API-key modes."""
        path = os.path.join(WEBAPP_DIR, "index.html")
        if not os.path.isfile(path):
            return web.Response(status=404, text="not found")
        try:
            with open(path, encoding="utf-8") as file:
                html = file.read()
            key = config.CHAT_API_TOKEN or ""
            # Only the tunnel owner may receive legacy browser credentials.
            # Tenant/org tunnels admit other users; never expose the host control
            # key to them (or to an anonymous tunnel).
            tunnel_auth = (getattr(config, "TUNNEL_AUTH", "private") or "private").strip().lower()
            auth_mode = (getattr(config, "AUTH_MODE", "tunnel") or "tunnel").strip().lower()
            if key and tunnel_auth == "private" and auth_mode in ("apikey", "both"):
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
            data = copilot_sessions.read_session_key(request.match_info["id"])
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)
        if data is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(data)

    async def copilot_session_import(request: web.Request) -> web.Response:
        """Import a native session under a separate Bridge conversation id.

        CLI imports receive a local execution binding to their resumable native
        id. VS Code imports remain readable history and get a fresh binding only
        if the user later continues them through a provider.
        """
        if not _authorized(request, config.CHAT_API_TOKEN):
            return web.json_response({"error": "unauthorized"}, status=401)
        source_key = request.match_info["id"]
        try:
            data = copilot_sessions.read_session_key(source_key)
        except ValueError:
            return web.json_response({"error": "invalid id"}, status=400)
        if data is None:
            return web.json_response({"error": "not found"}, status=404)
        source = data.get("source") or "cli"
        native_id = data.get("nativeId") or data.get("id")
        adapter_id = _adapter_for_native_source(source)
        session = store.find_by_external_ref(adapter_id, native_id)
        if session is None:
            session = store.create(title=data.get("title") or "")
            store.replace_messages(
                session["id"], data.get("messages") or [],
                title=data.get("title") or "", updated_at=data.get("updatedAt"),
            )
            store.add_external_ref(
                session["id"], adapter_id, native_id,
                capabilities={"canResume": source == "cli"},
            )
            if source == "cli":
                store.get_or_create_execution_binding(
                    session["id"], "copilot", native_session_id=native_id
                )
            session = store.get(session["id"])
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
    app.router.add_get("/api/settings", settings_get)
    app.router.add_patch("/api/settings", settings_update)
    app.router.add_get("/api/dashboard", dashboard_get)
    app.router.add_get("/api/inbox", inbox_list)
    app.router.add_patch("/api/inbox/{id}", inbox_update)
    app.router.add_post("/api/inbox/{id}/remind", inbox_remind)
    app.router.add_post("/api/inbox/mark-all-seen", inbox_mark_all_seen)
    app.router.add_post("/api/inbox/complete-all", inbox_complete_all)
    app.router.add_post("/api/inbox/scan", inbox_scan)
    app.router.add_get("/api/sync/status", sync_status)
    app.router.add_post("/api/sync/connect", sync_connect)
    app.router.add_post("/api/sync/run", sync_run)
    app.router.add_post("/api/sync/disconnect", sync_disconnect)
    # The native panel uses the host control credential, even in Entra-only web
    # mode. Public sync endpoints retain their existing web authentication policy.
    app.router.add_get("/api/control/sync/status", partial(sync_status, control=True))
    app.router.add_post("/api/control/sync/connect", partial(sync_connect, control=True))
    app.router.add_post("/api/control/sync/run", partial(sync_run, control=True))
    app.router.add_post("/api/control/sync/disconnect", partial(sync_disconnect, control=True))
    app.router.add_get("/api/uploads/{sid}/{name}", uploads_get)

    # Session management
    app.router.add_post("/api/sessions", sessions_create)
    app.router.add_get("/api/sessions", sessions_list)
    app.router.add_get("/api/history", history_list)
    app.router.add_get("/api/sessions/{id}/turns", sessions_turns)
    app.router.add_get("/api/sessions/{id}/knowledge", session_knowledge_list)
    app.router.add_get(
        "/api/sessions/{id}/knowledge/generation", knowledge_generation_get
    )
    app.router.add_post(
        "/api/sessions/{id}/knowledge/generate", knowledge_generation_start
    )
    app.router.add_post("/api/sessions/{id}/knowledge/extract", knowledge_extract)
    app.router.add_get("/api/sessions/{id}", sessions_get)
    app.router.add_post("/api/sessions/{id}/sync", sessions_sync)
    app.router.add_delete("/api/sessions/{id}", sessions_delete)
    app.router.add_patch("/api/sessions/{id}", sessions_update)

    # Versioned personal knowledge with message-level evidence
    app.router.add_get("/api/knowledge/generation-jobs", knowledge_generation_list)
    app.router.add_get("/api/knowledge/map", knowledge_map_get)
    app.router.add_post("/api/knowledge/map/generate", knowledge_map_generate)
    app.router.add_get("/api/knowledge", knowledge_list)
    app.router.add_post("/api/knowledge", knowledge_create)
    app.router.add_post("/api/knowledge/{id}/versions", knowledge_add_version)
    app.router.add_get("/api/knowledge/{id}", knowledge_get)
    app.router.add_patch("/api/knowledge/{id}", knowledge_update)
    app.router.add_delete("/api/knowledge/{id}", knowledge_delete)

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
    app.router.add_get("/v23.css", _file("v23.css"))
    app.router.add_get("/v23.js", _file("v23.js"))
    app.router.add_get("/icon.svg", _file("icon.svg"))
    # Bundled MSAL.js (browser) for the optional Microsoft Entra ID sign-in.
    app.router.add_get("/vendor/msal-browser.min.js", _file("vendor/msal-browser.min.js"))
