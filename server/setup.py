"""First-run interactive setup wizard for the Copilot Bridge server .exe.

Shown the first time the packaged server is launched **with a console** (i.e.
double-clicked, not the hidden background launch). It:

* checks for the Dev Tunnel CLI — if missing, offers to download it from the
  official site, or to open that site, or to skip; if already present it's
  detected and skipped;
* detects the available AI tools and lets the user pick one — defaulting to
  GitHub Copilot when present, otherwise the first available tool — with Enter
  accepting the default and a numbered menu for the alternatives.

The choices are written to ``.env`` next to the executable, and a ``.setup-done``
marker is created so the wizard doesn't reappear on every launch (``--setup``
forces it again). When run non-interactively (no console / piped output) the
wizard is skipped entirely so the background service starts unattended.
"""

from __future__ import annotations

import os
import platform
import sys
import urllib.request
import webbrowser
from pathlib import Path

from paths import app_base_dir
from provisioning import _upsert_env_line  # reuse the .env writer

# Official Dev Tunnel resources.
_DEVTUNNEL_DOWNLOAD = "https://aka.ms/TunnelsCliDownload"  # + /win-x64 | /win-arm64
_DEVTUNNEL_DOCS = "https://learn.microsoft.com/azure/developer/dev-tunnels/get-started"

_MARKER = ".setup-done"


def _marker_path() -> Path:
    return app_base_dir() / _MARKER


def is_interactive() -> bool:
    """True only when a real console is attached (so we can prompt the user)."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


def should_run(force: bool) -> bool:
    if force:
        return is_interactive()
    if _marker_path().exists():
        return False
    return is_interactive()


def _ask(prompt: str, default: str = "") -> str:
    """input() that never crashes; returns ``default`` on EOF/interrupt."""
    try:
        ans = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    ans = ans.strip()
    return ans or default


def _arch_slug() -> str:
    machine = (platform.machine() or "").lower()
    return "win-arm64" if "arm" in machine else "win-x64"


# ---- Dev Tunnel step -------------------------------------------------------

def _download_devtunnel(dest: Path) -> bool:
    url = f"{_DEVTUNNEL_DOWNLOAD}/{_arch_slug()}"
    print(f"  Downloading Dev Tunnel ({_arch_slug()}) from {url} …")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
            fh.write(resp.read())
        print(f"  Installed: {dest}")
        return True
    except Exception as exc:  # noqa: BLE001 - network/IO failures are non-fatal
        print(f"  Download failed: {exc}")
        print(f"  Please install it manually from {_DEVTUNNEL_DOCS}")
        return False


def _setup_devtunnel() -> None:
    from devtunnel import discover_devtunnel

    found = discover_devtunnel(os.environ.get("DEVTUNNEL_PATH", ""))
    print("\n[1/2] Dev Tunnel (gives your phone a public URL to reach this server)")
    if found:
        print(f"  \u2713 Detected Dev Tunnel: {found}")
        print("  Skipping installation.")
        return

    print("  \u2717 Dev Tunnel was not found on this machine.")
    print("    It's required for a public URL (without it the server is local-only).")
    print("    [Y] download & install it now (recommended)")
    print("    [O] open the official website to install it yourself")
    print("    [S] skip (run local-only for now)")
    choice = _ask("  Choose [Y/o/s] (default Y): ", "y").lower()

    if choice.startswith("s"):
        print("  Skipped. You can install Dev Tunnel later and restart.")
        return
    if choice.startswith("o"):
        print(f"  Opening {_DEVTUNNEL_DOCS} …")
        try:
            webbrowser.open(_DEVTUNNEL_DOCS)
        except Exception:  # noqa: BLE001
            print(f"  Could not open a browser. Visit: {_DEVTUNNEL_DOCS}")
        return

    # Default: download next to the executable so discovery finds it.
    dest = app_base_dir() / ("devtunnel.exe" if os.name == "nt" else "devtunnel")
    if _download_devtunnel(dest):
        # Make this run pick it up immediately.
        os.environ["DEVTUNNEL_PATH"] = str(dest)
        print("  Note: a one-time sign-in is needed for the public URL:")
        print(f"        \"{dest}\" user login")


# ---- AI provider step ------------------------------------------------------

def _setup_provider(config) -> None:
    from providers import available_providers

    print("\n[2/2] AI tool (which assistant answers your prompts)")
    infos = available_providers(config)
    usable = [p for p in infos if p.get("available")]

    if not usable:
        print("  No AI tool detected (Copilot / Claude / OpenAI).")
        print("  Install GitHub Copilot CLI (winget install GitHub.Copilot) or set up")
        print("  another provider, then restart. Defaulting to 'copilot' for now.")
        _upsert_env_line(app_base_dir() / ".env", "AI_PROVIDER", "copilot")
        return

    # Default: Copilot if available, else the first available tool.
    names = [p["name"] for p in usable]
    default = "copilot" if "copilot" in names else names[0]

    if "copilot" in names:
        print("  \u2713 Detected GitHub Copilot — default is Copilot.")
    else:
        print(f"  Copilot not detected. Default is the first available tool: {default}.")

    print("  Available tools (press Enter for the default, or type a number):")
    for i, p in enumerate(usable, 1):
        mark = "  \u2190 default" if p["name"] == default else ""
        print(f"    {i}. {p.get('displayName', p['name'])}  [{p['name']}]{mark}")

    raw = _ask(f"  Your choice [1-{len(usable)}] (default {default}): ", "")
    chosen = default
    if raw:
        if raw.isdigit() and 1 <= int(raw) <= len(usable):
            chosen = usable[int(raw) - 1]["name"]
        elif raw.lower() in names:
            chosen = raw.lower()
        else:
            print(f"  Unrecognized choice '{raw}', using default '{default}'.")

    _upsert_env_line(app_base_dir() / ".env", "AI_PROVIDER", chosen)
    os.environ["AI_PROVIDER"] = chosen
    print(f"  Using AI provider: {chosen}")


def run_setup(force: bool = False) -> None:
    """Run the wizard if appropriate (interactive + first run, or forced)."""
    if not should_run(force):
        return

    bar = "=" * 62
    print(bar)
    print("  Copilot Bridge — first-run setup")
    print("  Press Enter to accept the [default] at each step.")
    print(bar)

    _setup_devtunnel()

    # Import config lazily and AFTER devtunnel setup so any freshly written
    # values are visible. Config reads .env at import time.
    try:
        import config  # noqa: PLC0415
        cfg = config.DefaultConfig()
    except Exception:  # noqa: BLE001 - never let setup block startup
        cfg = None
    if cfg is not None:
        _setup_provider(cfg)

    try:
        _marker_path().write_text("ok\n", encoding="utf-8")
    except OSError:
        pass

    print("\n" + bar)
    print("  Setup complete — starting the server …")
    print(bar + "\n")
