"""Orchestrated entry point for the Copilot Bridge.

Starts the app server **and** the Dev Tunnel together, with clear staged startup
logging so you can see exactly which phase startup reached:

    [startup 1/6] configuration & app graph
    [startup 2/6] GitHub Copilot CLI
    [startup 3/6] Dev Tunnel CLI
    [startup 4/6] bind + serve the HTTP app
    [startup 5/6] /health readiness probe
    [startup 6/6] host the Dev Tunnel (public URL)

Run with:  python server/launcher.py     (or scripts/run.ps1)

Plain ``python server/app.py`` still works for a server-only (no tunnel) run.
"""

import asyncio
import logging
import signal
import sys

# Windows consoles default to cp1252; force UTF-8 so Unicode never crashes startup.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("copilot_bridge.launcher")

_TOTAL_STAGES = 6


def _stage(num: int, message: str) -> None:
    log.info("[startup %d/%d] %s", num, _TOTAL_STAGES, message)


def _gui_enabled() -> bool:
    """Whether to show GUI dialogs (login / connection info).

    On only for a desktop-attached run. Suppressed when CB_NO_GUI=1 (the hidden
    background service sets this) or when no Tk display is available, so headless
    hosts and the auto-start service never pop windows.
    """
    import os
    if str(os.environ.get("CB_NO_GUI", "")).strip().lower() in ("1", "true", "yes"):
        return False
    try:
        from gui import gui_available
        return gui_available()
    except Exception:  # noqa: BLE001
        return False


def _probe_health(host: str, port: int, timeout: float = 1.5) -> dict | None:
    """Return ``/health`` JSON if a Copilot Bridge already serves this port, else None.

    Used to detect that another copy (e.g. one whose window was closed but whose
    server kept running) is already up, so we don't try to bind the port twice.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _is_addr_in_use(exc: BaseException) -> bool:
    """True if ``exc`` is a 'port already in use' bind error (WSAEADDRINUSE 10048)."""
    import errno
    return (getattr(exc, "winerror", None) == 10048
            or getattr(exc, "errno", None) in (10048, getattr(errno, "EADDRINUSE", -1))
            or "10048" in str(exc))


async def _wait_for_health(host: str, port: int, timeout: float = 30.0) -> dict | None:
    """Poll /health until it returns 200 or the timeout elapses."""
    url = f"http://{host}:{port}/health"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    async with aiohttp.ClientSession() as session:
        while loop.time() < deadline:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
            except Exception:  # noqa: BLE001 - server may not be accepting yet
                pass
            await asyncio.sleep(0.3)
    return None


async def run(info_q=None) -> None:
    # ---- Stage 0: first-run provisioning + setup wizard ---------------
    # Generate a host-unique CHAT_API_TOKEN on first run (persisted to .env) so a
    # freshly-installed server "just works" and every machine gets its own key.
    # Must happen BEFORE importing app/config (config reads .env at import time).
    from provisioning import ensure_api_token, ensure_tunnel_id, write_connection_card

    api_key, created = ensure_api_token()
    if created:
        log.info("[startup 0/%d] Generated a new host-unique API key (saved to .env).",
                 _TOTAL_STAGES)
    # Dev Tunnel ids are GLOBALLY unique; a shared id collides across machines
    # ("Unauthorized tunnel access ... expected [host]"), so give every host its own.
    tunnel_id, tid_created = ensure_tunnel_id()
    if tid_created:
        log.info("[startup 0/%d] Using host-unique Dev Tunnel id '%s' (saved to .env).",
                 _TOTAL_STAGES, tunnel_id)

    # ``info_q`` set => we're driven by the desktop GUI (gui mode): the GUI wizard
    # already handled the Dev Tunnel + AI choices on the main thread, so DON'T run
    # the console wizard or any tkinter call here (we're on a worker thread).
    gui_mode = info_q is not None
    if not gui_mode:
        # Console first-run wizard (only when a real console is attached + first run).
        from setup import run_setup
        run_setup(force=("--setup" in sys.argv))

    # The wizard may have written AI_PROVIDER (and DEVTUNNEL_PATH) to .env and
    # os.environ; reload config so those choices take effect this run.
    import importlib
    import config as _config
    importlib.reload(_config)

    # ---- Stage 1: configuration & application graph --------------------
    _stage(1, "Loading configuration and building the application…")
    from app import APP, CONFIG, RUNNER  # importing wires config + AI provider

    host, port = CONFIG.HOST, CONFIG.PORT

    # ---- Stage 2: AI provider -----------------------------------------
    provider_name = getattr(RUNNER, "display_name", "AI provider")
    if getattr(RUNNER, "available", True):
        _stage(2, f"{provider_name} ready: {RUNNER.exe} "
                  f"(scope={CONFIG.COPILOT_SCOPE}, workdir={RUNNER.workdir})")
    else:
        _stage(2, f"{provider_name} selected but NOT available on this host "
                  f"(set up the tool or change AI_PROVIDER). Server will still start.")

    # ---- Stage 3: Dev Tunnel CLI --------------------------------------
    from devtunnel import DevTunnel, discover_devtunnel

    tunnel_exe = discover_devtunnel(CONFIG.DEVTUNNEL_PATH) if CONFIG.TUNNEL_ENABLED else None
    if not CONFIG.TUNNEL_ENABLED:
        _stage(3, "Dev Tunnel disabled (TUNNEL_ENABLED=false). Server-only mode.")
    elif tunnel_exe:
        _stage(3, f"Dev Tunnel CLI located: {tunnel_exe}")
    else:
        _stage(3, "Dev Tunnel CLI not found. Run scripts/install.ps1 to bundle it, "
                  "or set DEVTUNNEL_PATH. Continuing server-only.")

    # ---- Stage 4: bind + serve ----------------------------------------
    _stage(4, f"Starting app server on http://{host}:{port} …")
    app_runner = web.AppRunner(APP)
    await app_runner.setup()
    site = web.TCPSite(app_runner, host, port)
    await site.start()

    # ---- Stage 5: readiness probe -------------------------------------
    _stage(5, "Probing /health for readiness…")
    health = await _wait_for_health(host, port)
    if health:
        log.info("[startup 5/%d] Healthy: authMode=%s, allowlist=%s, scope=%s",
                 _TOTAL_STAGES, health.get("authMode"),
                 health.get("allowlistEntries"), health.get("scope"))
    else:
        log.warning("[startup 5/%d] /health did not return 200 in time; continuing.",
                    _TOTAL_STAGES)

    # ---- Control plane: attach BEFORE the (slow) Dev Tunnel hosting ----
    # The Control Panel talks to the server over HTTP. Stage 6 hosting can take
    # several seconds with retries, so wire the stop event + an (empty) controller
    # now — otherwise control calls during startup would 409. Stage 6 fills in the
    # tunnel handle once it has one.
    from control import Controller, tunnel_paused

    stop = asyncio.Event()
    APP["cb_stop"] = stop
    controller = Controller(None, CONFIG, write_connection_card, api_key,
                            getattr(RUNNER, "name", "copilot"), None, host, port)
    APP["cb_controller"] = controller

    # ---- Stage 6: host the Dev Tunnel ---------------------------------
    tunnel: DevTunnel | None = None
    public_url: str | None = None
    if CONFIG.TUNNEL_ENABLED and tunnel_exe:
        candidate = DevTunnel(tunnel_exe, CONFIG.TUNNEL_ID, port, CONFIG.TUNNEL_ANONYMOUS)
        controller.tunnel = candidate  # let the Control Panel act on it immediately
        # In GUI mode the wizard already handled sign-in on the main thread, so we
        # must NOT call tkinter here (we're on a worker thread). In console/headless
        # mode, only a real console prompt would apply; we just honour the current
        # sign-in state. Either way: signed in => host; otherwise LAN-only.
        if not candidate.is_logged_in():
            _stage(6, "Dev Tunnel: not signed in. Server is up on the LAN; public "
                      "tunnel skipped. Sign in to Dev Tunnel and restart for a public URL.")
            # Keep the handle so the Control Panel can explain *why* (not signed in)
            # and host it once the user signs in — don't silently disable it.
            tunnel = candidate
        elif tunnel_paused():
            _stage(6, "Dev Tunnel is paused (from the Control Panel). Serving LAN-only; "
                      "resume it from the Control Panel for a public URL.")
            tunnel = candidate  # keep the handle so the watchdog can host on resume
        else:
            _stage(6, f"Hosting Dev Tunnel '{CONFIG.TUNNEL_ID}' for port {port} "
                      f"(with retries) …")
            try:
                # host() now ensures the tunnel + retries until a public URL is up.
                public_url = await candidate.host()
            except Exception as exc:  # noqa: BLE001
                log.warning("[startup 6/%d] Dev Tunnel failed to start: %s", _TOTAL_STAGES, exc)
                public_url = None
            # Keep the handle regardless: the watchdog / Control Panel can retry.
            tunnel = candidate
            if public_url:
                _stage(6, f"Dev Tunnel is live: {public_url}")
            else:
                log.warning("[startup 6/%d] Dev Tunnel did not come up after retries; "
                            "serving LAN-only. The watchdog will keep retrying; you can "
                            "also restart it from the Control Panel.", _TOTAL_STAGES)
    else:
        _stage(6, "Dev Tunnel not started.")

    local_url = f"http://{host}:{port}"
    provider = getattr(RUNNER, "name", "copilot")
    card = write_connection_card(public_url, local_url, api_key, provider)
    _print_ready_banner(host, port, public_url, bool(CONFIG.CHAT_API_TOKEN), provider)
    for line in card.splitlines():
        log.info(line)

    # Hand the connection details to the GUI (main thread) when in GUI mode; it
    # shows them in the connection-info window. In headless/console mode there's
    # no window — the banner + connection.json are the source of truth.
    if gui_mode:
        try:
            info_q.put({
                "publicUrl": public_url, "localUrl": local_url,
                "apiKey": api_key, "provider": provider,
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("posting connection info failed: %s", exc)

    # ---- Control plane: finalize state + start the watchdog -----------
    # The controller was attached before Stage 6 (so control calls work during
    # startup); now record the final tunnel handle + public URL and start the
    # watchdog that keeps the tunnel in its desired state / self-heals a drop.
    controller.tunnel = tunnel
    controller.public_url = public_url
    controller.provider = provider
    watchdog_task = (asyncio.create_task(controller.watchdog(stop))
                     if tunnel is not None else None)

    # ---- Run until interrupted ----------------------------------------
    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows Proactor loop: fall back to KeyboardInterrupt

    try:
        await stop.wait()
    finally:
        log.info("Shutting down…")
        if watchdog_task:
            watchdog_task.cancel()
        if tunnel:
            await tunnel.stop()
        await app_runner.cleanup()


def _print_ready_banner(host: str, port: int, public_url: str | None, has_token: bool,
                        provider: str = "copilot") -> None:
    bar = "=" * 62
    log.info(bar)
    log.info("  Copilot Bridge is READY")
    log.info("  Provider: %s", provider)
    log.info("  Local   : http://%s:%s", host, port)
    if public_url:
        log.info("  Public  : %s", public_url)
        log.info("            %s/api/chat   %s/health", public_url, public_url)
    log.info("  Auth    : %s", "X-API-Key required" if has_token
             else "OPEN — no token set (anyone with the URL can run the AI)")
    log.info("  Stop    : Ctrl+C")
    log.info(bar)


def _run_gui() -> None:
    """Desktop flow: run the tkinter wizard + connection-info window on the MAIN
    thread (tkinter is main-thread-only) while the asyncio server runs on a
    worker thread.

    Closing the connection-info window does NOT stop the server: the worker is a
    non-daemon thread, so the process stays alive serving requests until the
    console is closed / the process is killed.
    """
    import importlib
    import queue
    import threading

    from provisioning import ensure_api_token, ensure_tunnel_id, read_connection_card

    # Ensure the API key + host-unique Dev Tunnel id exist before we read config
    # for the wizard (a shared tunnel id collides across machines).
    try:
        ensure_api_token()
        ensure_tunnel_id()
    except Exception as exc:  # noqa: BLE001
        log.warning("provisioning failed: %s", exc)

    import config as _config
    importlib.reload(_config)
    cfg = _config.DefaultConfig()

    from gui import run_setup_wizard, show_connection_info, show_message
    from paths import app_base_dir

    def _show_running_info(heading: str, note: str | None = None) -> None:
        """Show the already-running server's connection details.

        Prefers the saved card, but only trusts its URLs when they match the port
        we actually probed (a stale card from another instance would otherwise show
        the wrong port). When that instance is LAN-only the default note explains
        how to enable a public URL.
        """
        local_url = f"http://{cfg.HOST}:{cfg.PORT}"
        card = read_connection_card() or {}
        # Only trust the saved card if it describes THIS port (else it's stale).
        same = (card.get("localUrl") or "").endswith(f":{cfg.PORT}")
        pub = card.get("publicUrl") if same else None
        api_key = (card.get("apiKey") if same else None) or cfg.CHAT_API_TOKEN
        provider = (card.get("provider") if same else None) or getattr(cfg, "AI_PROVIDER", "copilot")
        if note is None:
            if pub:
                note = ("It's already running on this PC \u2014 these are its current "
                        "connection details. To stop it, end CopilotBridgeServer.exe "
                        "in Task Manager.")
            else:
                note = ("It's already running in LAN-only mode (no public URL). To let "
                        "your phone connect from anywhere: end CopilotBridgeServer.exe in "
                        "Task Manager, then start Copilot Bridge again and sign in to Dev "
                        "Tunnel when prompted.")
        show_connection_info(pub, local_url, api_key, provider,
                             heading=heading, note=note)

    # Already running? A previous launch keeps serving after its window closes, so
    # re-launching would crash with "address already in use" (WinError 10048).
    # Detect it and just show that instance's connection details instead.
    if _probe_health(cfg.HOST, cfg.PORT) is not None:
        _show_running_info("Copilot Bridge is already running")
        return

    # Build a tunnel handle for the wizard (may be None if the CLI isn't found).
    from devtunnel import DevTunnel, discover_devtunnel

    tunnel = None
    if cfg.TUNNEL_ENABLED:
        try:
            exe = discover_devtunnel(cfg.DEVTUNNEL_PATH)
            if exe:
                tunnel = DevTunnel(exe, cfg.TUNNEL_ID, cfg.PORT, cfg.TUNNEL_ANONYMOUS)
        except Exception as exc:  # noqa: BLE001
            log.debug("tunnel discovery failed: %s", exc)

    marker = app_base_dir() / ".setup-done"
    if ("--setup" in sys.argv) or (not marker.exists()):
        # First run or forced: the full wizard (Dev Tunnel + AI tool choice).
        try:
            run_setup_wizard(cfg, tunnel)
        except Exception as exc:  # noqa: BLE001
            log.warning("setup wizard failed: %s", exc)
        importlib.reload(_config)  # pick up the AI_PROVIDER / DEVTUNNEL_PATH choices
        cfg = _config.DefaultConfig()
    elif cfg.TUNNEL_ENABLED and (tunnel is None or not tunnel.is_logged_in()):
        # Already set up, but there's no public URL because Dev Tunnel isn't signed
        # in (the user manually relaunched after the phone couldn't connect). Prompt
        # sign-in so the server gets a public URL; they can still choose LAN-only.
        try:
            if tunnel is None:
                run_setup_wizard(cfg, tunnel)  # offers the "Install Dev Tunnel" path
                importlib.reload(_config)
                cfg = _config.DefaultConfig()
            else:
                from gui import prompt_devtunnel_login
                prompt_devtunnel_login(tunnel)
        except Exception as exc:  # noqa: BLE001
            log.warning("Dev Tunnel sign-in prompt failed: %s", exc)

    # Start the server on a NON-daemon worker thread; it posts the connection
    # details to info_q once the tunnel is up (or skipped), or an error if the
    # port is taken / startup fails.
    info_q: "queue.Queue" = queue.Queue()

    def _serve() -> None:
        try:
            asyncio.run(run(info_q=info_q))
        except Exception as exc:  # noqa: BLE001
            kind = "port-in-use" if _is_addr_in_use(exc) else "crash"
            log.exception("server thread failed (%s): %s", kind, exc)
            try:
                info_q.put({"error": kind, "detail": str(exc)})
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_serve, name="copilot-bridge-server", daemon=False).start()

    # Wait for startup (tunnel hosting retries can take a little while), then show
    # the info. The timeout exceeds the tunnel's own retry budget so we normally
    # get the real public URL rather than timing out into a fallback.
    try:
        info = info_q.get(timeout=240)
    except Exception:  # noqa: BLE001
        info = None

    # Success: the server is up — show the live connection details.
    if info and not info.get("error"):
        try:
            show_connection_info(info.get("publicUrl"), info.get("localUrl"),
                                 info.get("apiKey"), info.get("provider", "copilot"),
                                 note="Closing this window keeps the server running. "
                                      "Reopen it from Start menu \u2192 Copilot Bridge \u2192 "
                                      "View Connection Info.")
        except Exception as exc:  # noqa: BLE001
            log.debug("connection info window failed: %s", exc)
        return

    # Startup failed or timed out. If another copy grabbed the port meanwhile,
    # show its details; otherwise explain the failure clearly (no fake "running").
    if _probe_health(cfg.HOST, cfg.PORT) is not None:
        _show_running_info("Copilot Bridge is already running")
        return

    detail = (info or {}).get("detail", "")
    if info and info.get("error") == "port-in-use":
        show_message(
            "Couldn't start Copilot Bridge",
            f"Port {cfg.PORT} is already in use by another program. Close that program, "
            f"or change PORT in your .env file, then try again.\n\n{detail}",
            kind="error")
    else:
        show_message(
            "Couldn't start Copilot Bridge",
            "The server didn't start. See the log window for details, then try again."
            + (f"\n\n{detail}" if detail else ""),
            kind="error")
    # Window closed: the non-daemon server thread (if it started) keeps the process alive.


def _hide_console() -> None:
    """Hide this process's own console window (frozen Windows build only).

    Used by the "View Connection Info" shortcut so the viewer feels like a plain
    dialog. Gated on the frozen build so a developer running from a shared
    terminal never has their terminal hidden.
    """
    import os
    from paths import is_frozen
    if os.name != "nt" or not is_frozen():
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:  # noqa: BLE001
        pass


def _show_connection_info_only() -> None:
    """Display the saved endpoint + API key, then exit (the ``--show-info`` flag).

    Reads ``connection.json`` (written on the last successful run); if the server
    hasn't run yet it falls back to the local URL + key from ``.env``. Shows a GUI
    window on a desktop, otherwise prints the details to the console.
    """
    _hide_console()

    import importlib
    import config as _config
    importlib.reload(_config)
    cfg = _config.DefaultConfig()

    from provisioning import ensure_api_token, read_connection_card

    card = read_connection_card() or {}
    api_key = card.get("apiKey") or cfg.CHAT_API_TOKEN
    if not api_key:
        # Brand-new install that never started: mint the host key so we can show it.
        try:
            api_key, _ = ensure_api_token()
        except Exception:  # noqa: BLE001
            api_key = ""
    local_url = card.get("localUrl") or f"http://{cfg.HOST}:{cfg.PORT}"
    public_url = card.get("publicUrl")
    provider = card.get("provider") or getattr(cfg, "AI_PROVIDER", "copilot")
    fresh = not card

    if _gui_enabled():
        try:
            from gui import show_connection_info
            heading = ("Copilot Bridge — your connection details" if not fresh
                       else "Copilot Bridge — connection details")
            note = ("These are saved on this PC. The public URL appears here after you "
                    "start the server at least once." if fresh or not public_url
                    else "Paste these into the app's Settings on your phone or PC.")
            show_connection_info(public_url, local_url, api_key, provider,
                                 heading=heading, note=note)
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("connection info window failed: %s", exc)

    # Headless fallback: print the details.
    server_url = public_url or local_url
    bar = "-" * 62
    for line in (bar, "  COPILOT BRIDGE — CONNECTION", f"    Server URL : {server_url}",
                 f"    Local URL  : {local_url}",
                 f"    API Key    : {api_key or '(none — auth disabled)'}",
                 f"    AI Provider: {provider}", bar):
        log.info(line)
    if fresh:
        log.info("Note: start the server once to obtain the public (internet) URL.")


def _spawn_server() -> None:
    """Launch a detached, headless server process (used by the Control Panel).

    Runs the same executable with ``CB_NO_GUI=1`` (no wizard/info windows) and
    fully detached so it keeps running after the Control Panel closes. Output goes
    to ``<data>\\logs\\server.log`` so there's a log to inspect if it misbehaves.
    """
    import os
    import subprocess
    from pathlib import Path

    from paths import app_base_dir, is_frozen

    if is_frozen():
        args = [sys.executable]
    else:
        args = [sys.executable, str(Path(__file__).resolve())]

    env = dict(os.environ)
    env["CB_NO_GUI"] = "1"

    logdir = app_base_dir() / "logs"
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        logf = open(logdir / "server.log", "ab")
    except OSError:
        logf = subprocess.DEVNULL

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        subprocess.Popen(
            args, env=env, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
            creationflags=flags, cwd=str(app_base_dir()), close_fds=True,
        )
    finally:
        # The child inherited its own handle; release the parent's copy.
        if logf is not subprocess.DEVNULL:
            try:
                logf.close()
            except OSError:
                pass


def _run_control_panel() -> None:
    """Open the desktop Control Panel (the ``--control-panel`` flag)."""
    _hide_console()
    import importlib
    import config as _config
    importlib.reload(_config)
    cfg = _config.DefaultConfig()
    try:
        from gui import run_control_panel
        run_control_panel(cfg, _spawn_server)
    except Exception as exc:  # noqa: BLE001
        log.exception("Control Panel failed: %s", exc)


def main() -> None:
    # Control Panel: manage start/stop/restart of the server + Dev Tunnel.
    if "--control-panel" in sys.argv or "--panel" in sys.argv:
        _run_control_panel()
        return

    # "View Connection Info" shortcut: just show the saved endpoint + key and exit.
    if "--show-info" in sys.argv or "--connection-info" in sys.argv:
        _show_connection_info_only()
        return

    # Desktop session => drive everything from GUI windows (no console wizard).
    if _gui_enabled():
        try:
            _run_gui()
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("GUI startup failed; falling back to headless: %s", exc)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
