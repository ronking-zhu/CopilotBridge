"""Tkinter GUI dialogs for the packaged Copilot Bridge server.

Used at startup when a desktop session is available so the user never has to
touch a console:

* :func:`run_setup_wizard` — the first-run window: Dev Tunnel install/sign-in
  **and** the AI-tool choice, all on the main thread.
* :func:`show_connection_info` — displays the public URL, local URL and API key
  (with copy buttons) so the user can paste them into the mobile/desktop client.
* :func:`prompt_devtunnel_login` — a standalone sign-in dialog (kept for reuse).

Everything degrades gracefully: if no display/Tk is available the functions
return sensible defaults and the server keeps running headless.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import urllib.request

logger = logging.getLogger("copilot_bridge.gui")

_DEVTUNNEL_DOWNLOAD = "https://aka.ms/TunnelsCliDownload"  # + /win-x64 | /win-arm64


def _download_devtunnel_to(dest) -> bool:
    """Download the Dev Tunnel CLI to ``dest`` (a writable path). Best-effort."""
    arch = "win-arm64" if "arm" in (platform.machine() or "").lower() else "win-x64"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"{_DEVTUNNEL_DOWNLOAD}/{arch}", timeout=180) as r, \
                open(dest, "wb") as f:
            f.write(r.read())
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("devtunnel download failed: %s", exc)
        return False


def gui_available() -> bool:
    """True if a Tk display can be opened (desktop session present)."""
    try:
        import tkinter as tk  # noqa: PLC0415
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def prompt_devtunnel_login(tunnel, parent_title: str = "Copilot Bridge") -> bool:
    """Show a login dialog and perform Dev Tunnel sign-in. Returns logged-in state.

    ``tunnel`` is a :class:`devtunnel.DevTunnel`. If already signed in this is a
    silent success. On failure the user is told the server will be LAN-only.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception:  # noqa: BLE001 - no GUI: fall back to a headless attempt
        return tunnel.login()

    if tunnel.is_logged_in():
        return True

    state = {"done": False, "ok": False}

    root = tk.Tk()
    root.title(parent_title + " — Dev Tunnel sign-in")
    root.geometry("460x250")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="Sign in to Dev Tunnel", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    msg = ("To give your phone and other devices a public URL, the server needs a\n"
           "Microsoft Dev Tunnel sign-in (free). Click Sign in to open your browser.\n\n"
           "Without it, the server still works on your local network (LAN) only.")
    ttk.Label(frm, text=msg, justify="left").pack(anchor="w", pady=(8, 12))

    status = ttk.Label(frm, text="", foreground="#0a7", justify="left")
    status.pack(anchor="w")

    btns = ttk.Frame(frm)
    btns.pack(side="bottom", fill="x", pady=(14, 0))

    def do_login():
        sign_btn.config(state="disabled")
        skip_btn.config(state="disabled")
        status.config(text="Opening your browser… complete the sign-in there.", foreground="#555")
        root.update_idletasks()

        def worker():
            ok = tunnel.login()
            def finish():
                state["ok"] = ok
                if ok:
                    state["done"] = True
                    root.destroy()
                else:
                    status.config(
                        text="Sign-in did not complete. The server will run LAN-only.\n"
                             "You can try again, or click Continue (LAN only).",
                        foreground="#c33")
                    sign_btn.config(state="normal")
                    skip_btn.config(state="normal")
            root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def do_skip():
        state["ok"] = tunnel.is_logged_in()
        state["done"] = True
        root.destroy()

    sign_btn = ttk.Button(btns, text="Sign in", command=do_login)
    sign_btn.pack(side="right", padx=(8, 0))
    skip_btn = ttk.Button(btns, text="Continue (LAN only)", command=do_skip)
    skip_btn.pack(side="right")

    root.protocol("WM_DELETE_WINDOW", do_skip)
    root.mainloop()

    if not state["ok"]:
        try:
            r = tk.Tk(); r.withdraw()
            messagebox.showwarning(
                parent_title,
                "Dev Tunnel sign-in was not completed.\n\n"
                "Copilot Bridge will run in LAN-only mode: clients on the SAME "
                "network can connect using the Local URL, but there is no public "
                "internet URL until you sign in.\n\nYou can re-run sign-in later.")
            r.destroy()
        except Exception:  # noqa: BLE001
            pass
    return state["ok"]


def show_connection_info(public_url, local_url, api_key, provider="copilot",
                         parent_title: str = "Copilot Bridge",
                         heading: str = "Copilot Bridge is running",
                         note: str | None = None) -> None:
    """Show a window with the connection details + copy buttons.

    The same values are also in connection.json and the console banner. MUST run
    on the main thread (tkinter is not thread-safe). ``heading``/``note`` let the
    caller tailor the text (e.g. the standalone "View Connection Info" viewer).
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:  # noqa: BLE001
        return

    lan_only = not public_url or public_url == local_url
    root = tk.Tk()
    root.title(parent_title + " — Connection info")
    root.geometry("600x350")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text=heading, font=("Segoe UI", 13, "bold")).pack(anchor="w")
    if lan_only:
        ttk.Label(frm, text="LAN-only mode (no Dev Tunnel sign-in) — use the Local URL "
                            "from devices on the same network.",
                  foreground="#c33", justify="left", wraplength=560).pack(anchor="w", pady=(4, 10))
    else:
        ttk.Label(frm, text="Public URL is live — your phone can connect from anywhere.",
                  foreground="#0a7").pack(anchor="w", pady=(4, 10))

    def row(label, value):
        value = value or ""
        r = ttk.Frame(frm); r.pack(fill="x", pady=4)
        ttk.Label(r, text=label, width=12).pack(side="left")
        # Use a plain Entry with insert() (NOT a StringVar): a local StringVar can
        # be garbage-collected after this function returns, which blanks the field.
        ent = ttk.Entry(r)
        ent.insert(0, value)
        ent.configure(state="readonly")
        ent.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def copy(v=value):
            root.clipboard_clear(); root.clipboard_append(v)
            try:
                status.config(text="Copied!")
                root.after(1200, lambda: status.config(text=""))
            except Exception:  # noqa: BLE001
                pass
        ttk.Button(r, text="Copy", width=6, command=copy).pack(side="right")

    if not lan_only:
        row("Server URL", public_url)
    row("Local URL", local_url)
    row("API Key", api_key)
    row("AI Provider", provider)

    status = ttk.Label(frm, text="", foreground="#0a7")
    status.pack(anchor="w", pady=(6, 0))

    bottom = ttk.Frame(frm); bottom.pack(side="bottom", fill="x", pady=(14, 0))
    ttk.Button(bottom, text="Close", command=root.destroy).pack(side="right")
    hint = note or "Reopen anytime from Start menu \u2192 Copilot Bridge \u2192 View Connection Info."
    ttk.Label(bottom, text=hint, foreground="#777", wraplength=470,
              justify="left").pack(side="left")

    root.mainloop()


def show_message(heading, body, title: str = "Copilot Bridge", kind: str = "info") -> None:
    """Show a tiny modal info/error window with an OK button.

    Best-effort and main-thread-only; a no-op if Tk can't be opened.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:  # noqa: BLE001
        return

    root = tk.Tk()
    root.title(title)
    root.geometry("520x230")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=heading, font=("Segoe UI", 13, "bold"),
              foreground=("#c33" if kind == "error" else "#222")).pack(anchor="w")
    ttk.Label(frm, text=body, justify="left", wraplength=480).pack(anchor="w", pady=(10, 0))

    bottom = ttk.Frame(frm); bottom.pack(side="bottom", fill="x", pady=(16, 0))
    ttk.Button(bottom, text="OK", command=root.destroy).pack(side="right")

    root.mainloop()


def run_setup_wizard(cfg, tunnel, parent_title: str = "Copilot Bridge") -> str:
    """First-run GUI wizard: Dev Tunnel install/sign-in + AI-tool choice.

    Runs on the main thread. Writes the chosen ``AI_PROVIDER`` to ``.env`` (and
    ``os.environ``), performs the Dev Tunnel browser sign-in if requested, writes
    the ``.setup-done`` marker, and returns the chosen provider name. On any GUI
    failure it returns the default provider so startup still proceeds.
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:  # noqa: BLE001
        return _default_provider_name(cfg)

    from paths import app_base_dir
    from provisioning import _upsert_env_line
    from providers import available_providers

    usable = [p for p in available_providers(cfg) if p.get("available")]
    names = [p["name"] for p in usable]
    default_provider = "copilot" if "copilot" in names else (names[0] if names else "copilot")

    tn = {"obj": tunnel}  # mutable so the install/sign-in callbacks can replace it
    state = {"provider": default_provider}

    root = tk.Tk()
    root.title(parent_title + " — Setup")
    root.geometry("560x470")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Welcome to Copilot Bridge", font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Set up how your phone and other devices reach this server.",
              foreground="#555").pack(anchor="w", pady=(2, 12))

    # ---------- Dev Tunnel section ----------
    dt = ttk.LabelFrame(frm, text="1. Dev Tunnel  (public URL for your phone)", padding=12)
    dt.pack(fill="x")
    dt_status = ttk.Label(dt, text="", justify="left", wraplength=500)
    dt_status.pack(anchor="w")
    dt_btns = ttk.Frame(dt); dt_btns.pack(anchor="w", pady=(8, 0))
    install_btn = ttk.Button(dt_btns, text="Install Dev Tunnel")
    signin_btn = ttk.Button(dt_btns, text="Sign in to Dev Tunnel")

    def refresh_dt():
        install_btn.pack_forget(); signin_btn.pack_forget()
        if not cfg.TUNNEL_ENABLED:
            dt_status.config(text="Dev Tunnel is disabled — the server runs on your LAN only.",
                             foreground="#c33")
            return
        if tn["obj"] is None:
            dt_status.config(text="Dev Tunnel CLI not found. Install it for a public URL, "
                                  "or continue to run on your local network only.",
                             foreground="#c33")
            install_btn.pack(side="left")
            return
        if tn["obj"].is_logged_in():
            user = tn["obj"].logged_in_user()
            dt_status.config(text="\u2713 Signed in" + (f" as {user}" if user else "")
                             + ". Your phone can connect from anywhere.", foreground="#0a7")
        else:
            dt_status.config(text="Not signed in. Sign in (free, opens your browser) for a public "
                                  "URL, or continue for LAN-only.", foreground="#c33")
            signin_btn.pack(side="left")

    def do_install():
        install_btn.config(state="disabled")
        dt_status.config(text="Downloading Dev Tunnel\u2026", foreground="#555")
        root.update_idletasks()

        def worker():
            dest = app_base_dir() / "devtunnel.exe"
            ok = _download_devtunnel_to(dest)

            def finish():
                install_btn.config(state="normal")
                if ok:
                    os.environ["DEVTUNNEL_PATH"] = str(dest)
                    try:
                        from devtunnel import DevTunnel
                        tn["obj"] = DevTunnel(str(dest), cfg.TUNNEL_ID, cfg.PORT, cfg.TUNNEL_ANONYMOUS)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("constructing tunnel after install failed: %s", exc)
                else:
                    dt_status.config(text="Download failed. Check your connection and try again, "
                                          "or install from https://aka.ms/devtunnel.", foreground="#c33")
                refresh_dt()
            root.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    def do_signin():
        signin_btn.config(state="disabled")
        dt_status.config(text="Opening your browser\u2026 complete the sign-in there.", foreground="#555")
        root.update_idletasks()

        def worker():
            ok = bool(tn["obj"]) and tn["obj"].login()

            def finish():
                signin_btn.config(state="normal")
                if not ok:
                    dt_status.config(text="Sign-in didn't complete \u2014 the server will run LAN-only. "
                                          "You can try again.", foreground="#c33")
                refresh_dt()
            root.after(0, finish)
        threading.Thread(target=worker, daemon=True).start()

    install_btn.config(command=do_install)
    signin_btn.config(command=do_signin)
    refresh_dt()

    # ---------- AI provider section ----------
    ai = ttk.LabelFrame(frm, text="2. AI tool  (which assistant answers your prompts)", padding=12)
    ai.pack(fill="x", pady=(12, 0))
    prov_var = tk.StringVar(master=root, value=default_provider)
    if usable:
        for p in usable:
            label = p.get("displayName", p["name"])
            if p["name"] == "copilot":
                label += "   (recommended)"
            ttk.Radiobutton(ai, text=label, value=p["name"], variable=prov_var).pack(anchor="w")
    else:
        ttk.Label(ai, text="No AI tool detected. Install the GitHub Copilot CLI "
                           "(winget install GitHub.Copilot), then restart. Using 'copilot' for now.",
                  foreground="#c33", wraplength=500).pack(anchor="w")

    # ---------- bottom ----------
    bottom = ttk.Frame(frm); bottom.pack(side="bottom", fill="x", pady=(16, 0))

    def on_start():
        chosen = prov_var.get() or default_provider
        state["provider"] = chosen
        try:
            _upsert_env_line(app_base_dir() / ".env", "AI_PROVIDER", chosen)
            os.environ["AI_PROVIDER"] = chosen
            (app_base_dir() / ".setup-done").write_text("ok\n", encoding="utf-8")
        except OSError:
            pass
        root.destroy()

    ttk.Button(bottom, text="Start server  \u2192", command=on_start).pack(side="right")
    ttk.Label(bottom, text="You can change these later in the .env file.",
              foreground="#777").pack(side="left")

    root.protocol("WM_DELETE_WINDOW", on_start)
    root.mainloop()
    return state["provider"]


def _default_provider_name(cfg) -> str:
    try:
        from providers import available_providers
        names = [p["name"] for p in available_providers(cfg) if p.get("available")]
        return "copilot" if "copilot" in names else (names[0] if names else "copilot")
    except Exception:  # noqa: BLE001
        return "copilot"


_CREATE_NO_WINDOW = 0x08000000


def _force_kill_server(tunnel_id: str = "copilot-bridge") -> bool:
    """Force-stop the server (and our Dev Tunnel) when a graceful stop fails.

    Windows-only, best-effort: kills ``CopilotBridgeServer.exe`` processes that
    are NOT a Control Panel (so we never kill ourselves) plus any ``devtunnel host``
    for our tunnel id. Used only as a fallback when the shutdown endpoint doesn't
    respond (a hung server — exactly when the user needs the Control Panel).
    """
    if os.name != "nt":
        return False
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='CopilotBridgeServer.exe'\" | "
        "Where-Object { $_.CommandLine -notmatch '--control-panel' -and "
        "$_.CommandLine -notmatch '--panel' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
        "Get-CimInstance Win32_Process -Filter \"Name='devtunnel.exe'\" | "
        f"Where-Object {{ $_.CommandLine -match 'host' -and $_.CommandLine -match '{tunnel_id}' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        import subprocess
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       creationflags=_CREATE_NO_WINDOW, timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


def run_control_panel(cfg, spawn_server, parent_title: str = "Copilot Bridge") -> None:
    """Desktop Control Panel: start/stop/restart the server and the Dev Tunnel.

    Talks to a running server over its localhost control API; when the server is
    stopped, ``spawn_server`` launches a fresh detached instance. Runs on the main
    thread; network/process work happens on worker threads that push results onto
    a queue drained by the main-thread ``pump`` loop (tkinter is main-thread-only).
    """
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:  # noqa: BLE001
        return

    import json
    import queue
    import threading
    import time
    import urllib.request

    base = f"http://{getattr(cfg, 'HOST', 'localhost')}:{getattr(cfg, 'PORT', 3978)}"
    key = getattr(cfg, "CHAT_API_TOKEN", "")
    tunnel_id = getattr(cfg, "TUNNEL_ID", "copilot-bridge")
    tunnel_enabled = bool(getattr(cfg, "TUNNEL_ENABLED", True))
    # Probe 127.0.0.1 rather than "localhost": when the server is stopped this
    # refuses instantly instead of waiting on the IPv6 (::1) attempt first.
    _phost = "127.0.0.1" if getattr(cfg, "HOST", "localhost") in ("localhost", "") else cfg.HOST
    probe_base = f"http://{_phost}:{getattr(cfg, 'PORT', 3978)}"

    def http(method, path, body=None, timeout=5):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(probe_base + path, data=data, method=method)
        if key:
            req.add_header("X-API-Key", key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}

    def fetch_status():
        try:
            s = http("GET", "/api/control/status", timeout=3)
            s["server"] = "running"
            return s
        except Exception:  # noqa: BLE001
            return {"server": "stopped"}

    root = tk.Tk()
    root.title(parent_title + " — Control Panel")
    root.geometry("640x460")
    root.resizable(False, False)
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Copilot Bridge — Control Panel",
              font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Manually start, stop, or restart the server and the Dev Tunnel.",
              foreground="#555").pack(anchor="w", pady=(2, 12))

    # tkinter is main-thread-only: workers push results onto this queue and the
    # main-thread `pump` loop drains it. Never touch a widget off the main thread.
    ui_q: "queue.Queue" = queue.Queue()
    state = {"busy": False, "fetching": False, "ticks": 0}

    # ---- Server row ----
    srow = ttk.LabelFrame(frm, text="Copilot Bridge server", padding=12)
    srow.pack(fill="x")
    s_status = ttk.Label(srow, text="checking\u2026", font=("Segoe UI", 10, "bold"))
    s_status.pack(side="left")
    s_btns = ttk.Frame(srow); s_btns.pack(side="right")
    s_start = ttk.Button(s_btns, text="Start")
    s_stop = ttk.Button(s_btns, text="Stop")
    s_restart = ttk.Button(s_btns, text="Restart")
    for b in (s_start, s_stop, s_restart):
        b.pack(side="left", padx=3)

    # ---- Tunnel row ----
    trow = ttk.LabelFrame(frm, text="Dev Tunnel (public URL for your phone)", padding=12)
    trow.pack(fill="x", pady=(10, 0))
    t_status = ttk.Label(trow, text="\u2014", font=("Segoe UI", 10, "bold"))
    t_status.pack(side="left")
    t_btns = ttk.Frame(trow); t_btns.pack(side="right")
    t_start = ttk.Button(t_btns, text="Start")
    t_stop = ttk.Button(t_btns, text="Stop")
    t_restart = ttk.Button(t_btns, text="Restart")
    for b in (t_start, t_stop, t_restart):
        b.pack(side="left", padx=3)

    # ---- Connection details ----
    det = ttk.LabelFrame(frm, text="Connection", padding=12)
    det.pack(fill="x", pady=(10, 0))

    def field(label):
        r = ttk.Frame(det); r.pack(fill="x", pady=2)
        ttk.Label(r, text=label, width=11).pack(side="left")
        e = ttk.Entry(r); e.configure(state="readonly")
        e.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def copy(e=e):
            root.clipboard_clear(); root.clipboard_append(e.get())
            set_status("Copied!")
        ttk.Button(r, text="Copy", width=6, command=copy).pack(side="right")
        return e

    e_public = field("Server URL")
    e_local = field("Local URL")
    e_key = field("API Key")

    statusline = ttk.Label(frm, text="", foreground="#0a7", wraplength=600, justify="left")
    statusline.pack(anchor="w", pady=(10, 0))

    def set_status(msg, err=False):
        statusline.config(text=msg, foreground=("#c33" if err else "#0a7"))

    def set_entry(e, val):
        e.configure(state="normal"); e.delete(0, "end")
        e.insert(0, val or ""); e.configure(state="readonly")

    def render(st):
        server_up = st.get("server") == "running"
        s_status.config(text=("\u25cf Running" if server_up else "\u25cb Stopped"),
                        foreground=("#0a7" if server_up else "#999"))
        tstate = st.get("tunnel", "\u2014") if server_up else "\u2014"
        tmap = {"running": "\u25cf Public URL live", "paused": "\u23f8 Paused (stopped)",
                "down": "\u25cb Not running", "disabled": "Disabled", "\u2014": "\u25cb \u2014"}
        t_status.config(text=tmap.get(tstate, tstate),
                        foreground=("#0a7" if tstate == "running"
                                    else ("#c80" if tstate == "paused" else "#999")))
        set_entry(e_public, st.get("publicUrl") or (st.get("localUrl") if server_up else ""))
        set_entry(e_local, st.get("localUrl") or base)
        set_entry(e_key, key)
        b = state["busy"]
        s_start.config(state="disabled" if (server_up or b) else "normal")
        s_stop.config(state="normal" if (server_up and not b) else "disabled")
        s_restart.config(state="normal" if (server_up and not b) else "disabled")
        tun_ok = server_up and tunnel_enabled and tstate != "disabled"
        t_start.config(state="normal" if (tun_ok and tstate in ("paused", "down") and not b) else "disabled")
        t_stop.config(state="normal" if (tun_ok and tstate == "running" and not b) else "disabled")
        t_restart.config(state="normal" if (tun_ok and tstate in ("running", "down") and not b) else "disabled")

    def start_fetch():
        if state["fetching"]:
            return
        state["fetching"] = True

        def work():
            st = fetch_status()
            ui_q.put(("status", st))
        threading.Thread(target=work, daemon=True).start()

    def run_action(fn, msg):
        if state["busy"]:
            return
        state["busy"] = True
        set_status(msg)
        for btn in (s_start, s_stop, s_restart, t_start, t_stop, t_restart):
            btn.config(state="disabled")

        def work():
            try:
                ok, info = fn()
            except Exception as exc:  # noqa: BLE001
                ok, info = False, str(exc)
            ui_q.put(("action", ok, info))
        threading.Thread(target=work, daemon=True).start()

    def pump():
        # Main thread only: drain worker results, then schedule a periodic refresh.
        try:
            while True:
                msg = ui_q.get_nowait()
                if msg[0] == "status":
                    state["fetching"] = False
                    if not state["busy"]:
                        render(msg[1])
                elif msg[0] == "action":
                    state["busy"] = False
                    set_status(msg[2], err=not msg[1])
                    start_fetch()
        except queue.Empty:
            pass
        state["ticks"] += 1
        if state["ticks"] % 16 == 0 and not state["busy"] and not state["fetching"]:
            start_fetch()
        root.after(160, pump)

    # ---- actions (run on a worker thread) ----
    def act_start_server():
        try:
            spawn_server()
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't start the server: {exc}"
        for _ in range(60):  # ~30s for a cold start
            time.sleep(0.5)
            if fetch_status().get("server") == "running":
                return True, "Server started."
        return True, "Server is starting\u2026 (still coming up)"

    def act_stop_server():
        try:
            http("POST", "/api/control/shutdown", body={}, timeout=5)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(20):  # ~10s for a graceful stop
            time.sleep(0.5)
            if fetch_status().get("server") != "running":
                return True, "Server stopped."
        _force_kill_server(tunnel_id)
        time.sleep(1.0)
        if fetch_status().get("server") != "running":
            return True, "Server stopped (forced)."
        return False, "Couldn't stop the server. Try ending CopilotBridgeServer.exe in Task Manager."

    def act_restart_server():
        act_stop_server()
        time.sleep(1.0)
        return act_start_server()

    def tunnel_action(action, label):
        try:
            http("POST", "/api/control/tunnel", body={"action": action}, timeout=150)
            return True, f"Dev Tunnel {label}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Dev Tunnel {label} failed: {exc}"

    s_start.config(command=lambda: run_action(act_start_server, "Starting the server\u2026"))
    s_stop.config(command=lambda: run_action(act_stop_server, "Stopping the server\u2026"))
    s_restart.config(command=lambda: run_action(act_restart_server, "Restarting the server\u2026"))
    t_start.config(command=lambda: run_action(lambda: tunnel_action("resume", "started"),
                                              "Starting the Dev Tunnel\u2026"))
    t_stop.config(command=lambda: run_action(lambda: tunnel_action("pause", "stopped"),
                                             "Stopping the Dev Tunnel\u2026"))
    t_restart.config(command=lambda: run_action(lambda: tunnel_action("restart", "restarted"),
                                                "Restarting the Dev Tunnel\u2026"))

    start_fetch()
    root.after(160, pump)
    root.mainloop()


