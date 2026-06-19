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
import re
import threading
import urllib.request

logger = logging.getLogger("copilot_bridge.gui")

_DEVTUNNEL_DOWNLOAD = "https://aka.ms/TunnelsCliDownload"  # + /win-x64 | /win-arm64


def app_version() -> str:
    """Return the product version string (best-effort), e.g. '1.6.3'.

    Reads it from the tiny dependency-free ``version`` module so this never has to
    import the full application graph. Falls back to '' if unavailable.
    """
    try:
        from version import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


def _load_icon_image(size: int = 48):
    """Return a Tk ``PhotoImage`` of the app icon at ~``size`` px, or ``None``.

    Prefers the bundled ``icon-256.png`` (works without Pillow via Tk's PNG
    support, subsampled to roughly ``size``). Located via ``resource_dir()`` so it
    resolves both from source and from the frozen build. Best-effort: any failure
    returns ``None`` and the caller simply shows text only.
    """
    try:
        import tkinter as tk
        from paths import resource_dir
    except Exception:  # noqa: BLE001
        return None
    candidates = []
    try:
        candidates.append(resource_dir() / "assets" / "icon-256.png")
    except Exception:  # noqa: BLE001
        pass
    try:
        from paths import app_base_dir
        candidates.append(app_base_dir() / "assets" / "icon-256.png")
    except Exception:  # noqa: BLE001
        pass
    # Source tree: assets/ sits next to the server/ dir.
    try:
        from pathlib import Path
        candidates.append(Path(__file__).resolve().parent.parent / "assets" / "icon-256.png")
    except Exception:  # noqa: BLE001
        pass
    for png in candidates:
        try:
            if not png.is_file():
                continue
            img = tk.PhotoImage(file=str(png))
            # icon-256.png is 256px; subsample to about the requested size.
            factor = max(1, round(256 / max(1, size)))
            if factor > 1:
                img = img.subsample(factor, factor)
            return img
        except Exception:  # noqa: BLE001
            continue
    return None


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
    root.geometry("600x500")
    root.resizable(True, True)
    try:
        root.minsize(600, 440)
    except Exception:  # noqa: BLE001
        pass
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

    # --- Optional Microsoft Entra ID sign-in: configure WHO may use this server ---
    # Loaded lazily so this window also works on a fresh install. The same dialogs
    # are used by the Control Panel; both write straight to .env.
    def _load_cfg():
        import importlib
        import config as _c
        importlib.reload(_c)
        return _c.DefaultConfig()
    try:
        _cfg0 = _load_cfg()
    except Exception:  # noqa: BLE001
        _cfg0 = None
    sec = ttk.LabelFrame(frm, text="Microsoft sign-in (Entra ID)", padding=10)
    sec.pack(fill="x", pady=(10, 0))
    sec_status = ttk.Label(sec, text=(_identity_summary(_cfg0) if _cfg0 else ""),
                           foreground="#555")
    sec_status.pack(side="left")

    def _on_id_saved(mode):
        try:
            sec_status.config(text=f"Mode: {mode} \u2014 saved; restart the server to apply")
            status.config(text="Saved Microsoft sign-in settings. Restart the server to apply.")
        except Exception:  # noqa: BLE001
            pass

    def _on_users_saved(users):
        try:
            status.config(text=f"Saved allowed users ({len(users)}). Restart the server to apply.")
        except Exception:  # noqa: BLE001
            pass

    def _open_identity():
        try:
            _identity_dialog(root, _load_cfg(), on_saved=_on_id_saved)
        except Exception as exc:  # noqa: BLE001
            status.config(text=f"Couldn't open sign-in settings: {exc}")

    def _open_users():
        try:
            _users_dialog(root, _load_cfg(), on_saved=_on_users_saved)
        except Exception as exc:  # noqa: BLE001
            status.config(text=f"Couldn't open user manager: {exc}")

    secbtns = ttk.Frame(sec); secbtns.pack(side="right")
    ttk.Button(secbtns, text="Manage users\u2026", command=_open_users).pack(side="right", padx=(6, 0))
    ttk.Button(secbtns, text="Configure\u2026", command=_open_identity).pack(side="right")

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


def _identity_summary(cfg) -> str:
    """One-line description of the current sign-in mode for the Control Panel."""
    mode = (getattr(cfg, "AUTH_MODE", "apikey") or "apikey").strip().lower()
    if mode == "apikey":
        return "Mode: shared API key"
    tid = (getattr(cfg, "ENTRA_TENANT_ID", "") or "").strip()
    short = (tid[:8] + "\u2026") if tid else "(no tenant set)"
    label = {"entra": "Microsoft only", "both": "API key or Microsoft"}.get(mode, mode)
    return f"Mode: {label} \u00b7 tenant {short}"


def _identity_dialog(parent, cfg, on_saved=None) -> None:
    """Modal editor for the Microsoft Entra ID sign-in settings.

    Writes the chosen values straight to ``.env`` (via
    :func:`provisioning.set_identity_config`) so it works whether or not the server
    is currently running; the caller is told a restart is needed to apply them.
    """
    import tkinter as tk
    from tkinter import ttk, messagebox

    from provisioning import (build_access_request_mailto, detect_current_user,
                              detect_entra_tenant, make_request_code, make_share_code,
                              parse_share_code, read_connection_card, set_identity_config)

    def _join(v):
        return ", ".join(v) if isinstance(v, (list, tuple)) else (v or "")

    win = tk.Toplevel(parent)
    win.title("Microsoft sign-in (Entra ID)")
    win.transient(parent)
    win.resizable(False, False)
    try:
        win.grab_set()
    except Exception:  # noqa: BLE001
        pass

    f = ttk.Frame(win, padding=16)
    f.pack(fill="both", expand=True)
    f.columnconfigure(1, weight=1)

    ttk.Label(f, text="Microsoft Entra ID sign-in",
              font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(f, text="Let people sign in with their Microsoft work account instead of "
                      "sharing one API key.", foreground="#555", wraplength=540,
              justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 12))

    # Heads-up when the built-in TEST Entra ID (the maintainer's) is still in use,
    # so nobody mistakes the pre-filled defaults for their own app registration.
    try:
        from config import (TEST_ENTRA_TENANT_ID, TEST_ENTRA_CLIENT_ID,
                            TEST_ENTRA_ADMIN_CONTACT)
    except Exception:  # noqa: BLE001
        TEST_ENTRA_TENANT_ID = TEST_ENTRA_CLIENT_ID = TEST_ENTRA_ADMIN_CONTACT = ""
    _cur_tid = (getattr(cfg, "ENTRA_TENANT_ID", "") or "").strip().lower()
    _cur_cid = (getattr(cfg, "ENTRA_CLIENT_ID", "") or "").strip().lower()
    if TEST_ENTRA_TENANT_ID and _cur_tid == TEST_ENTRA_TENANT_ID.lower() \
            and _cur_cid == TEST_ENTRA_CLIENT_ID.lower():
        tk.Label(win,
                 text=("\u26a0  These fields are pre-filled with "
                       f"{TEST_ENTRA_ADMIN_CONTACT or 'the maintainer'}\u2019s "
                       "TEST Entra ID. Try Microsoft sign-in with it, or replace it "
                       "with your own app registration for production."),
                 bg="#FFF4CE", fg="#5C4400", justify="left", anchor="w",
                 wraplength=560, padx=12, pady=8).pack(side="top", fill="x", before=f)

    # Sign-in mode -----------------------------------------------------------
    ttk.Label(f, text="Sign-in mode").grid(row=2, column=0, sticky="w", pady=4)
    mode_var = tk.StringVar(value=(getattr(cfg, "AUTH_MODE", "apikey") or "apikey").strip().lower())
    ttk.Combobox(f, textvariable=mode_var, state="readonly", width=14,
                 values=["apikey", "both", "entra"]).grid(row=2, column=1, sticky="w", pady=4)
    ttk.Label(f, text="apikey = shared key \u00b7 both = either \u00b7 entra = Microsoft only",
              foreground="#888", font=("Segoe UI", 8)).grid(row=2, column=2, sticky="w", padx=6)

    # Tenant id (+ Detect) ---------------------------------------------------
    ttk.Label(f, text="Tenant ID").grid(row=3, column=0, sticky="w", pady=4)
    e_tenant = ttk.Entry(f, width=40)
    e_tenant.insert(0, getattr(cfg, "ENTRA_TENANT_ID", "") or "")
    e_tenant.grid(row=3, column=1, sticky="we", pady=4)
    msg = ttk.Label(f, text="", foreground="#0a7", wraplength=540, justify="left")

    def do_detect():
        tid, tname = detect_entra_tenant()
        if tid:
            e_tenant.delete(0, "end")
            e_tenant.insert(0, tid)
            msg.config(text=f"Detected this PC's tenant: {tname or tid}", foreground="#0a7")
        else:
            msg.config(text="Couldn't detect an Entra tenant on this PC "
                            "(is it Microsoft Entra joined?).", foreground="#c80")

    ttk.Button(f, text="Detect", width=8, command=do_detect).grid(row=3, column=2, sticky="w", padx=6)
    ttk.Label(f, text="Your Entra directory (tenant) GUID. Use Detect to read this device's tenant.",
              foreground="#888", font=("Segoe UI", 8), wraplength=540,
              justify="left").grid(row=4, column=1, columnspan=2, sticky="w")

    def field_row(r, label, value, hint):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        e = ttk.Entry(f, width=46)
        e.insert(0, value or "")
        e.grid(row=r, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Label(f, text=hint, foreground="#888", font=("Segoe UI", 8), wraplength=540,
                  justify="left").grid(row=r + 1, column=1, columnspan=2, sticky="w")
        return e

    e_client = field_row(5, "Client ID", getattr(cfg, "ENTRA_CLIENT_ID", ""),
                         "Application (client) ID of the Copilot Bridge app registration.")
    e_aud = field_row(7, "Audience", _join(getattr(cfg, "ENTRA_AUDIENCE", "")),
                      "Access-token audience. Leave blank to use api://<client-id>.")
    e_scopes = field_row(9, "Scopes", _join(getattr(cfg, "ENTRA_SCOPES", "")),
                         "Scope the client requests. Blank = api://<client-id>/access_as_user.")
    e_users = field_row(11, "Allow users", _join(getattr(cfg, "ENTRA_ALLOWED_USERS", "")),
                        "Emails / UPNs or object ids, comma-separated.")
    e_groups = field_row(13, "Allow groups", _join(getattr(cfg, "ENTRA_ALLOWED_GROUPS", "")),
                         "Security-group object ids, comma-separated.")
    e_roles = field_row(15, "Allow roles", _join(getattr(cfg, "ENTRA_ALLOWED_ROLES", "")),
                        "App-role values, comma-separated. Recommended.")

    msg.grid(row=17, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def do_save():
        mode = mode_var.get().strip().lower()
        tenant = e_tenant.get().strip()
        client = e_client.get().strip()
        aud = e_aud.get().strip()
        users = e_users.get().strip()
        groups = e_groups.get().strip()
        roles = e_roles.get().strip()
        if mode in ("entra", "both"):
            if not tenant:
                messagebox.showwarning("Microsoft sign-in",
                                       "Enter (or Detect) a Tenant ID first.", parent=win)
                return
            if not client and not aud:
                messagebox.showwarning("Microsoft sign-in",
                                       "Enter the Client ID (or an Audience) from your "
                                       "app registration.", parent=win)
                return
            if not aud and client:
                aud = f"api://{client}"
            if not (users or groups or roles):
                if not messagebox.askyesno(
                        "Microsoft sign-in",
                        "No allow-list is set. Microsoft sign-in is fail-closed, so EVERY "
                        "user will be denied until you add at least one allowed user, "
                        "group, or role.\n\nSave anyway?", parent=win):
                    return
        set_identity_config(
            auth_mode=mode, tenant_id=tenant, client_id=client, audience=aud,
            scopes=e_scopes.get().strip(), allowed_users=users,
            allowed_groups=groups, allowed_roles=roles,
            admin_contact=_admin["contact"],
        )
        win.destroy()
        if on_saved:
            on_saved(mode)

    # Admin's email travels inside a share code so a colleague's “Request access”
    # can address the approval mail automatically. Held here, persisted on Save.
    _admin = {"contact": (getattr(cfg, "ENTRA_ADMIN_CONTACT", "") or "").strip()}

    # --- Share / import the sign-in config (so a colleague needn't retype it) ---
    def do_share():
        tenant = e_tenant.get().strip()
        client = e_client.get().strip()
        if not tenant or not client:
            messagebox.showwarning("Share sign-in config",
                                   "Enter (or Detect) the Tenant ID and Client ID first.",
                                   parent=win)
            return
        aud = e_aud.get().strip() or f"api://{client}"
        scopes = e_scopes.get().strip() or f"api://{client}/access_as_user"
        # Ask for the admin contact email (pre-filled) so colleagues can request access.
        pop = tk.Toplevel(win)
        pop.title("Share sign-in config")
        pop.transient(win)
        try:
            pop.grab_set()
        except Exception:  # noqa: BLE001
            pass
        pf = ttk.Frame(pop, padding=14)
        pf.pack(fill="both", expand=True)
        ttk.Label(pf, text="Your email (so colleagues can email you an access request):",
                  wraplength=460, justify="left").pack(anchor="w")
        e_admin = ttk.Entry(pf, width=52)
        e_admin.insert(0, _admin["contact"])
        e_admin.pack(fill="x", pady=(4, 8))
        ttk.Label(pf, text="Send the code below to your colleague. They paste it with "
                           "\u201cImport code\u201d (or run setup-entra.ps1 -FromConfig <code>).",
                  wraplength=460, justify="left").pack(anchor="w")
        ttk.Label(pf, text="It contains only tenant / client / audience / scopes (+ your "
                           "email) \u2014 no API key, no allow-list, no secret.",
                  foreground="#888", font=("Segoe UI", 8), wraplength=460,
                  justify="left").pack(anchor="w", pady=(2, 8))
        box = tk.Text(pf, height=3, width=56, wrap="char")
        box.pack(fill="x")
        status2 = ttk.Label(pf, text="", foreground="#0a7")
        status2.pack(anchor="w", pady=(6, 0))

        def regen():
            admin = e_admin.get().strip()
            _admin["contact"] = admin
            code = make_share_code(tenant, client, aud, scopes, admin)
            box.configure(state="normal"); box.delete("1.0", "end")
            box.insert("1.0", code); box.configure(state="disabled")
            try:
                win.clipboard_clear(); win.clipboard_append(code)
            except Exception:  # noqa: BLE001
                pass
            status2.config(text="Copied to clipboard.")
        regen()
        brow = ttk.Frame(pf); brow.pack(fill="x", pady=(10, 0))
        ttk.Button(brow, text="Close", command=pop.destroy).pack(side="right", padx=4)
        ttk.Button(brow, text="Update / copy", command=regen).pack(side="right", padx=4)

    def do_import():
        pop = tk.Toplevel(win)
        pop.title("Import sign-in config")
        pop.transient(win)
        try:
            pop.grab_set()
        except Exception:  # noqa: BLE001
            pass
        pf = ttk.Frame(pop, padding=14)
        pf.pack(fill="both", expand=True)
        ttk.Label(pf, text="Paste the code your colleague sent you "
                           "(starts with CBCFG1.):", wraplength=440,
                  justify="left").pack(anchor="w")
        box = tk.Text(pf, height=3, width=54, wrap="char")
        box.pack(fill="x", pady=(6, 8))
        box.focus_set()

        def apply_code():
            code = box.get("1.0", "end").strip()
            try:
                cfg2 = parse_share_code(code)
            except ValueError as exc:
                messagebox.showwarning("Import sign-in config",
                                       f"That doesn\u2019t look like a valid code.\n\n{exc}",
                                       parent=pop)
                return
            mode_var.set("entra")
            for entry, val in ((e_tenant, cfg2["tenant_id"]), (e_client, cfg2["client_id"]),
                               (e_aud, cfg2["audience"]), (e_scopes, cfg2["scopes"])):
                entry.delete(0, "end")
                entry.insert(0, val)
            if cfg2.get("admin_contact"):
                _admin["contact"] = cfg2["admin_contact"]
            pop.destroy()
            tail = (" You can now use \u201cRequest access\u201d to email the admin."
                    if _admin["contact"] else "")
            msg.config(text="Imported sign-in config. Add yourself under \u201cAllow users\u201d, "
                            "then Save." + tail, foreground="#0a7")

        row = ttk.Frame(pf); row.pack(fill="x")
        ttk.Button(row, text="Cancel", command=pop.destroy).pack(side="right", padx=4)
        ttk.Button(row, text="Import", command=apply_code).pack(side="right", padx=4)

    def do_request():
        """Colleague: email the admin a pre-filled access request (allow-list + redirect)."""
        import webbrowser
        admin = _admin["contact"]
        if not admin:
            messagebox.showinfo(
                "Request access",
                "No admin email is set. Import the code your admin sent (it carries "
                "their email), or ask them for it.", parent=win)
            return
        # The full allow-list the colleague wants granted, read straight from the
        # “Allow users” field; who is asking = first entry, else this PC's account.
        users = [u.strip() for u in e_users.get().split(",") if u.strip()]
        account = users[0] if users else detect_current_user()
        if not account and not users:
            messagebox.showinfo("Request access",
                                "Add your account under \u201cAllow users\u201d first.", parent=win)
            return
        card = read_connection_card() or {}
        public_url = card.get("publicUrl") or card.get("serverUrl") or ""
        tenant = e_tenant.get().strip()
        rcode = make_request_code(account, public_url, tenant, users)
        mailto = build_access_request_mailto(admin, account, public_url, rcode,
                                             allowed_users=users)
        try:
            webbrowser.open(mailto)
            n = len(users) or 1
            msg.config(text=f"Opened an email to {admin} with your allow-list "
                            f"({n} account{'s' if n != 1 else ''}) and redirect URL. "
                            "Review and Send it.", foreground="#0a7")
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("Request access",
                                   f"Couldn\u2019t open your mail client: {exc}\n\n"
                                   f"Send this to {admin} manually:\n\n{rcode}", parent=win)

    btns = ttk.Frame(f)
    btns.grid(row=18, column=0, columnspan=3, sticky="we", pady=(14, 0))
    ttk.Button(btns, text="Share code\u2026", command=do_share).pack(side="left", padx=2)
    ttk.Button(btns, text="Import code\u2026", command=do_import).pack(side="left", padx=2)
    ttk.Button(btns, text="Request access\u2026", command=do_request).pack(side="left", padx=2)
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=4)
    ttk.Button(btns, text="Save", command=do_save).pack(side="right", padx=4)

    win.update_idletasks()
    try:
        win.geometry(f"+{parent.winfo_rootx() + 40}+{parent.winfo_rooty() + 30}")
    except Exception:  # noqa: BLE001
        pass


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USER_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _valid_user_id(value: str) -> bool:
    """True if ``value`` looks like an email / UPN or an Entra object id (GUID)."""
    value = (value or "").strip()
    return bool(_EMAIL_RE.match(value) or _USER_GUID_RE.match(value))


def _users_dialog(parent, cfg, on_saved=None) -> None:
    """Manage WHO may use this server: the ``ENTRA_ALLOWED_USERS`` allow-list.

    A focused editor (add / remove individual people) so the owner can grant access
    to specific Microsoft work accounts without hand-editing a comma-separated
    string. Adding their own account here means only they are authorized, even
    though anyone in the tenant can *authenticate* — the server is fail-closed and
    denies any signed-in user who isn't on this list. Writes straight to ``.env``.
    """
    import tkinter as tk
    from tkinter import ttk, messagebox

    from provisioning import detect_current_user, get_allowed_users, set_allowed_users

    win = tk.Toplevel(parent)
    win.title("Allowed users — who can access this server")
    win.transient(parent)
    win.resizable(False, False)
    try:
        win.grab_set()
    except Exception:  # noqa: BLE001
        pass

    f = ttk.Frame(win, padding=16)
    f.pack(fill="both", expand=True)
    f.columnconfigure(0, weight=1)

    ttk.Label(f, text="Allowed users", font=("Segoe UI", 12, "bold")).grid(
        row=0, column=0, columnspan=2, sticky="w")
    ttk.Label(f, text="Only these Microsoft work accounts may use this server. Anyone "
                      "else in your organization can sign in but will be denied "
                      "(fail-closed). Add your own account to keep it private to you.",
              foreground="#555", wraplength=520, justify="left").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(2, 12))

    # Current allow-list, read fresh from .env so repeated opens stay accurate.
    lb = tk.Listbox(f, height=7, activestyle="dotbox")
    lb.grid(row=2, column=0, sticky="we", pady=(0, 2))
    sb = ttk.Scrollbar(f, orient="vertical", command=lb.yview)
    sb.grid(row=2, column=1, sticky="ns", pady=(0, 2))
    lb.config(yscrollcommand=sb.set)

    def _seed():
        for u in get_allowed_users():
            lb.insert("end", u)
    _seed()

    msg = ttk.Label(f, text="", foreground="#0a7", wraplength=520, justify="left")

    def _current_items():
        return [lb.get(i) for i in range(lb.size())]

    def _add_value(value: str):
        value = (value or "").strip()
        if not value:
            return
        if not _valid_user_id(value):
            messagebox.showwarning(
                "Allowed users",
                f"\u201c{value}\u201d doesn\u2019t look like an email / UPN "
                "(name@domain) or an object id (GUID).", parent=win)
            return
        if value.lower() in {u.lower() for u in _current_items()}:
            msg.config(text=f"{value} is already on the list.", foreground="#c80")
            return
        lb.insert("end", value)
        lb.see("end")
        msg.config(text=f"Added {value}.", foreground="#0a7")

    # Add-by-typing row -----------------------------------------------------
    addrow = ttk.Frame(f)
    addrow.grid(row=3, column=0, columnspan=2, sticky="we", pady=(8, 0))
    addrow.columnconfigure(0, weight=1)
    e_add = ttk.Entry(addrow)
    e_add.grid(row=0, column=0, sticky="we")
    e_add.bind("<Return>", lambda _e: (_add_value(e_add.get()), e_add.delete(0, "end")))

    def do_add():
        _add_value(e_add.get())
        e_add.delete(0, "end")
        e_add.focus_set()

    ttk.Button(addrow, text="Add", width=8, command=do_add).grid(row=0, column=1, padx=(8, 0))
    ttk.Label(f, text="Enter an email / UPN (alice@contoso.com) or an object id (GUID), then Add.",
              foreground="#888", font=("Segoe UI", 8)).grid(
        row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))

    # Convenience + remove row ---------------------------------------------
    tools = ttk.Frame(f)
    tools.grid(row=5, column=0, columnspan=2, sticky="we", pady=(8, 0))

    def do_add_me():
        upn = detect_current_user()
        if upn:
            _add_value(upn)
        else:
            messagebox.showinfo(
                "Allowed users",
                "Couldn\u2019t detect this PC\u2019s Microsoft account "
                "(is it signed in with a work account?). Type it in manually.",
                parent=win)

    def do_remove():
        sel = list(lb.curselection())
        if not sel:
            msg.config(text="Select a user in the list to remove.", foreground="#c80")
            return
        for i in reversed(sel):
            lb.delete(i)
        msg.config(text="Removed.", foreground="#0a7")

    ttk.Button(tools, text="Add this PC\u2019s account", command=do_add_me).pack(side="left")
    ttk.Button(tools, text="Remove selected", command=do_remove).pack(side="left", padx=(8, 0))

    msg.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def do_save():
        users = _current_items()
        mode = (getattr(cfg, "AUTH_MODE", "apikey") or "apikey").strip().lower()
        if not users and mode in ("entra", "both"):
            if not messagebox.askyesno(
                    "Allowed users",
                    "The list is empty. With Microsoft sign-in on, the server is "
                    "fail-closed, so EVERY user (including you) will be denied until "
                    "you add at least one account.\n\nSave an empty list anyway?",
                    parent=win):
                return
        set_allowed_users(users)
        win.destroy()
        if on_saved:
            on_saved(users)

    btns = ttk.Frame(f)
    btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(14, 0))
    ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=4)
    ttk.Button(btns, text="Save", command=do_save).pack(side="right", padx=4)

    e_add.focus_set()
    win.update_idletasks()
    try:
        win.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 40}")
    except Exception:  # noqa: BLE001
        pass


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
    root.geometry("660x640")
    root.resizable(True, True)
    try:
        root.minsize(640, 560)
    except Exception:  # noqa: BLE001
        pass
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)

    # ---- Header: app icon + title + version -------------------------------
    header = ttk.Frame(frm)
    header.pack(fill="x")
    _icon_img = _load_icon_image(48)
    if _icon_img is not None:
        # Keep a reference on the widget so Tk doesn't garbage-collect the image.
        ico = ttk.Label(header, image=_icon_img)
        ico.image = _icon_img
        ico.pack(side="left", padx=(0, 12))
        try:
            root.iconphoto(True, _icon_img)
        except Exception:  # noqa: BLE001
            pass
    titlebox = ttk.Frame(header)
    titlebox.pack(side="left", fill="x", expand=True)
    ttk.Label(titlebox, text="Copilot Bridge — Control Panel",
              font=("Segoe UI", 14, "bold")).pack(anchor="w")
    ttk.Label(titlebox, text="Manually start, stop, or restart the server and the Dev Tunnel.",
              foreground="#555").pack(anchor="w", pady=(2, 0))
    _ver = app_version()
    ttk.Label(header, text=(f"v{_ver}" if _ver else ""),
              foreground="#888", font=("Segoe UI", 10)).pack(side="right", anchor="n")

    ttk.Separator(frm, orient="horizontal").pack(fill="x", pady=(12, 12))

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

    # ---- Identity / Microsoft sign-in ----
    idrow = ttk.LabelFrame(frm, text="Identity / Microsoft sign-in", padding=12)
    idrow.pack(fill="x", pady=(10, 0))
    id_status = ttk.Label(idrow, text=_identity_summary(cfg), font=("Segoe UI", 10))
    id_status.pack(side="left")

    def _on_identity_saved(mode):
        id_status.config(text=f"Mode: {mode} \u00b7 saved \u2014 click Restart to apply")
        set_status("Saved Microsoft sign-in settings. Click Restart to apply them.")

    def _on_users_saved(users):
        n = len(users)
        who = "no users (server locked)" if n == 0 else (
            f"{n} user" + ("s" if n != 1 else ""))
        set_status(f"Saved allowed users: {who}. Click Restart to apply.")

    ttk.Button(idrow, text="Configure\u2026",
               command=lambda: _identity_dialog(root, cfg, on_saved=_on_identity_saved)
               ).pack(side="right")
    ttk.Button(idrow, text="Manage users\u2026",
               command=lambda: _users_dialog(root, cfg, on_saved=_on_users_saved)
               ).pack(side="right", padx=(0, 6))

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

    def _tunnel_reason_msg(reason, detail):
        detail = (detail or "").strip()
        if len(detail) > 240:
            detail = detail[:240].rstrip() + "\u2026"
        if reason == "not-signed-in":
            return ("Dev Tunnel isn't signed in on this PC. Open Start menu \u2192 "
                    "Copilot Bridge and run the setup wizard to sign in (free), "
                    "then try again.")
        if reason == "cli-missing":
            return ("Dev Tunnel isn't installed. Reinstall Copilot Bridge, or install "
                    "the Dev Tunnel CLI, then try again.")
        if reason == "tunnel-unavailable":
            return "Dev Tunnel isn't available on this server."
        if reason == "host-failed":
            low = detail.lower()
            # A globally-unique tunnel id owned by someone else -> can't host it.
            if "expected" in low and "host" in low and ("unauthorized" in low or "scope" in low):
                return ("This Dev Tunnel name is already in use by another account, so "
                        "this PC can't host it. Delete TUNNEL_ID from your .env (or set a "
                        "unique one) and restart \u2014 Copilot Bridge will pick a fresh, "
                        "unique tunnel automatically." + ("\n\n" + detail if detail else ""))
            base = ("Dev Tunnel couldn't connect. This is usually a network or "
                    "firewall block \u2014 allow access to *.devtunnels.ms (and any "
                    "corporate proxy).")
            return base + ("\n\n" + detail if detail else "")
        return "Dev Tunnel couldn't start." + ("\n\n" + detail if detail else "")

    def tunnel_action(action, label):
        try:
            r = http("POST", "/api/control/tunnel", body={"action": action}, timeout=180)
            if isinstance(r, dict) and r.get("ok") is False:
                return False, _tunnel_reason_msg(r.get("reason"), r.get("detail"))
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


