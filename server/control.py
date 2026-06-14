"""Runtime control plane for the Copilot Bridge server.

Exposes a tiny localhost-only HTTP API the desktop **Control Panel** uses to
manage a running server without touching files or Task Manager:

    GET  /api/control/status            -> {server, tunnel, publicUrl, ...}
    POST /api/control/shutdown          -> stop the whole server (graceful)
    POST /api/control/tunnel {action}   -> pause | resume | restart the Dev Tunnel

The Dev Tunnel's desired state is also persisted to a sentinel file
(``.tunnel-paused``) so the server's watchdog keeps enforcing it (and re-hosting
on an unexpected drop) across restarts. Everything requires the host API key and
is intended for ``localhost`` only.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from aiohttp.web import json_response

from paths import app_base_dir

logger = logging.getLogger("copilot_bridge.control")

_PAUSE_SENTINEL = ".tunnel-paused"


def tunnel_paused() -> bool:
    """True if the user paused the Dev Tunnel from the control panel."""
    try:
        return (app_base_dir() / _PAUSE_SENTINEL).exists()
    except OSError:
        return False


def set_tunnel_paused(paused: bool) -> None:
    """Persist the paused/resumed intent for the Dev Tunnel."""
    path = app_base_dir() / _PAUSE_SENTINEL
    try:
        if paused:
            path.write_text("paused\n", encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("could not update tunnel pause sentinel: %s", exc)


class Controller:
    """Owns the live Dev Tunnel handle so endpoints + the watchdog can act on it.

    Created by the launcher once the server is up and attached to the aiohttp app
    as ``app['cb_controller']``. All tunnel mutations are serialized by a lock so
    the watchdog and an endpoint never fight over the same process.
    """

    def __init__(self, tunnel, config, write_card, api_key, provider,
                 public_url, host, port):
        self.tunnel = tunnel  # devtunnel.DevTunnel | None
        self.config = config
        self._write_card = write_card
        self.api_key = api_key
        self.provider = provider
        self.public_url = public_url
        self.host = host
        self.port = port
        self._lock = asyncio.Lock()

    @property
    def local_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def has_tunnel(self) -> bool:
        return self.tunnel is not None

    def _save_card(self) -> None:
        try:
            self._write_card(self.public_url, self.local_url, self.api_key, self.provider)
        except Exception as exc:  # noqa: BLE001
            logger.debug("write_connection_card failed: %s", exc)

    def status(self) -> dict:
        if not self.tunnel:
            tstate = "disabled"
        elif tunnel_paused():
            tstate = "paused"
        elif self.tunnel.is_hosting():
            tstate = "running"
        else:
            tstate = "down"
        return {
            "tunnel": tstate,
            "publicUrl": self.public_url if tstate == "running" else None,
            "localUrl": self.local_url,
            "provider": self.provider,
        }

    async def pause(self) -> None:
        async with self._lock:
            set_tunnel_paused(True)
            if self.tunnel:
                await self.tunnel.stop()
            self.public_url = None
            self._save_card()

    async def resume(self) -> None:
        async with self._lock:
            set_tunnel_paused(False)
            if self.tunnel and not self.tunnel.is_hosting():
                self.public_url = await self.tunnel.host()
                self._save_card()

    async def restart(self) -> None:
        async with self._lock:
            set_tunnel_paused(False)
            if self.tunnel:
                await self.tunnel.stop()
                self.public_url = await self.tunnel.host()
                self._save_card()

    async def watchdog(self, stop_event: asyncio.Event, interval: float = 5.0) -> None:
        """Keep the tunnel in its desired state and self-heal an unexpected drop.

        Honours the pause sentinel and re-hosts (single attempt per tick) if the
        tunnel should be up but its process has exited. Uses the same lock as the
        endpoints so concurrent control actions stay consistent.
        """
        while not stop_event.is_set():
            try:
                await asyncio.sleep(interval)
                if not self.tunnel:
                    continue
                async with self._lock:
                    paused = tunnel_paused()
                    hosting = self.tunnel.is_hosting()
                    if paused and hosting:
                        logger.info("Dev Tunnel paused — stopping host process.")
                        await self.tunnel.stop()
                        self.public_url = None
                        self._save_card()
                    elif (not paused) and (not hosting):
                        logger.info("Dev Tunnel not running — re-hosting (watchdog).")
                        url = await self.tunnel.host(url_timeout=30.0, retries=1)
                        if url:
                            self.public_url = url
                            self._save_card()
                            logger.info("Dev Tunnel re-established: %s", url)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - a watchdog must never die
                logger.debug("tunnel watchdog tick failed: %s", exc)


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return True
    provided = request.headers.get("X-API-Key")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    return provided == token


def setup_control_routes(app: web.Application, config) -> None:
    """Register the localhost control endpoints on ``app``."""

    token = getattr(config, "CHAT_API_TOKEN", "")

    def _guard(request: web.Request):
        # API key required (same trust model as the chat API). The control panel
        # always talks to localhost; we never advertise these via the tunnel.
        if not _authorized(request, token):
            return json_response({"error": "unauthorized"}, status=401)
        return None

    async def status(request: web.Request) -> web.Response:
        denied = _guard(request)
        if denied is not None:
            return denied
        body = {"server": "running"}
        ctl: Controller | None = request.app.get("cb_controller")
        if ctl is not None:
            body.update(ctl.status())
        else:
            # Server is up but the controller isn't wired (e.g. tunnel disabled).
            body["tunnel"] = "disabled"
            body["localUrl"] = f"http://{getattr(config, 'HOST', 'localhost')}:{getattr(config, 'PORT', 3978)}"
        return json_response(body)

    async def shutdown(request: web.Request) -> web.Response:
        denied = _guard(request)
        if denied is not None:
            return denied
        ev: asyncio.Event | None = request.app.get("cb_stop")
        if ev is not None:
            ev.set()
            return json_response({"stopping": True})
        return json_response({"error": "no-stop-handle"}, status=503)

    async def tunnel(request: web.Request) -> web.Response:
        denied = _guard(request)
        if denied is not None:
            return denied
        ctl: Controller | None = request.app.get("cb_controller")
        if ctl is None or not ctl.has_tunnel():
            return json_response({"error": "tunnel-unavailable"}, status=409)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        action = str(data.get("action", "")).strip().lower()
        if action == "pause":
            await ctl.pause()
        elif action == "resume":
            await ctl.resume()
        elif action == "restart":
            await ctl.restart()
        else:
            return json_response({"error": "bad-action"}, status=400)
        return json_response(ctl.status())

    app.router.add_get("/api/control/status", status)
    app.router.add_post("/api/control/shutdown", shutdown)
    app.router.add_post("/api/control/tunnel", tunnel)
