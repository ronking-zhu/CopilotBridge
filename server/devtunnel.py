"""Dev Tunnel discovery + lifecycle helper for the orchestrated launcher.

The launcher uses this to:
  * locate the ``devtunnel`` binary (bundled in ``tools/`` by scripts/install.ps1,
    or found on PATH / the winget link),
  * make sure the named tunnel + an **http** port mapping exist, and
  * host the tunnel as a child process, returning the public HTTPS URL.

Important: the tunnel port protocol must be ``http`` because the local app server
speaks plain HTTP. Using ``https`` makes the relay attempt a TLS handshake against
the HTTP port and every request fails with 502 Bad Gateway.
"""

import asyncio
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("copilot_bridge.devtunnel")

# Matches a devtunnel public hostname, e.g. https://abc123de-3978.usw2.devtunnels.ms
_URL_RE = re.compile(r"https://[A-Za-z0-9\-]+\.[A-Za-z0-9\-]+\.devtunnels\.ms")


def _repo_root() -> Path:
    # server/devtunnel.py -> repo root is the parent of server/
    return Path(__file__).resolve().parent.parent


def _bundled_exe() -> Path:
    name = "devtunnel.exe" if os.name == "nt" else "devtunnel"
    return _repo_root() / "tools" / name


def discover_devtunnel(explicit: str = "") -> Optional[str]:
    """Locate the devtunnel executable.

    Preference: explicit path -> next to the (frozen) exe -> bundled tools/ binary
    -> PATH -> winget link. Returns the path, or None if nothing is found.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)

    # A copy installed/bundled next to the executable (the setup wizard downloads
    # devtunnel here, and the packaged exe ships it alongside).
    try:
        from paths import app_base_dir
        name = "devtunnel.exe" if os.name == "nt" else "devtunnel"
        candidates.append(str(app_base_dir() / name))
    except Exception:  # noqa: BLE001 - paths import is best-effort
        pass

    candidates.append(str(_bundled_exe()))

    on_path = shutil.which("devtunnel")
    if on_path:
        candidates.append(on_path)

    if os.name == "nt":
        candidates.append(
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\devtunnel.exe")
        )

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return on_path  # may be None


class DevTunnel:
    """Manage one named Dev Tunnel that fronts the local app server port."""

    def __init__(self, exe: str, tunnel_id: str, port: int, tunnel_auth: str = "private"):
        self.exe = exe
        self.tunnel_id = tunnel_id
        self.port = port
        # Tunnel-layer access control: 'private' (owner-only Entra), 'tenant'
        # (whole Entra tenant), 'anonymous', or 'org:<name>' (GitHub). Accept a
        # legacy bool for back-compat (True => anonymous, False => private).
        if isinstance(tunnel_auth, bool):
            tunnel_auth = "anonymous" if tunnel_auth else "private"
        self.tunnel_auth = (tunnel_auth or "private").strip().lower()
        self.anonymous = (self.tunnel_auth == "anonymous")
        # Effective access as last applied/read back (for the UI + connection card).
        self.access_code: str = ""
        self.access_human: str = ""
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._drain_task: Optional[asyncio.Task] = None
        # Last few lines the devtunnel CLI printed on a failed host attempt, kept so
        # the Control Panel / logs can show the *real* reason it wouldn't start.
        self.last_error: str = ""

    @property
    def _org_name(self) -> str:
        """For 'org:<name>' auth, the GitHub org; '' otherwise."""
        if self.tunnel_auth.startswith("org:"):
            return self.tunnel_auth.split(":", 1)[1].strip()
        return ""

    # -- sync helpers (quick CLI calls) ----------------------------------

    def _run(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

    def is_logged_in(self) -> bool:
        try:
            result = self._run("user", "show")
        except Exception as exc:  # noqa: BLE001
            logger.warning("devtunnel user show failed: %s", exc)
            return False
        out = (result.stdout or "") + (result.stderr or "")
        return "Logged in" in out

    def diagnose(self) -> tuple[bool, str]:
        """Return ``(ready, reason)`` describing whether hosting can succeed.

        ``reason`` is a short machine code the UI maps to a friendly message:
        ``"cli-missing"`` (devtunnel.exe not found), ``"not-signed-in"`` (needs
        ``devtunnel user login``), or ``""`` when everything looks ready. This is
        the first thing the Control Panel checks so a failure is explained rather
        than silently retried.
        """
        exe = self.exe
        found = bool(exe) and (os.path.isfile(exe) or shutil.which(exe) is not None)
        if not found:
            return False, "cli-missing"
        if not self.is_logged_in():
            return False, "not-signed-in"
        return True, ""

    def login(self, timeout: int = 300) -> bool:
        """Run ``devtunnel user login`` (browser auth) and return success.

        Opens the system browser for sign-in. Returns True if afterwards the user
        is logged in. Safe to call when already signed in (it's a no-op success).
        """
        if self.is_logged_in():
            return True
        try:
            # Browser auth is the default and the friendliest for a desktop app.
            self._run("user", "login", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - treat any failure as "not logged in"
            logger.warning("devtunnel user login failed: %s", exc)
            return False
        return self.is_logged_in()

    def relogin(self, timeout: int = 300) -> str:
        """Force a fresh Microsoft sign-in: log out, then browser login.

        Used by the Control Panel's "Re-sign in" button so the user can switch or
        refresh the Microsoft account that gates (and hosts) the tunnel. Blocks until
        the browser sign-in completes or times out. Returns the now signed-in
        user (email/UPN), or '' if it didn't complete.
        """
        try:
            self._run("user", "logout", timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.warning("devtunnel user logout failed: %s", exc)
        try:
            self._run("user", "login", timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("devtunnel user login failed: %s", exc)
        return self.logged_in_user()

    def logged_in_user(self) -> str:
        """Best-effort signed-in identity (email/username), or '' if unknown."""
        try:
            result = self._run("user", "show")
        except Exception:  # noqa: BLE001
            return ""
        out = (result.stdout or "") + (result.stderr or "")
        # Prefer the token that looks like an email/UPN (e.g. "Logged in as
        # roz@microsoft.com using Microsoft." -> roz@microsoft.com).
        for line in out.splitlines():
            for tok in line.replace(",", " ").split():
                t = tok.strip().strip(".")
                if "@" in t and "." in t.split("@")[-1]:
                    return t
        return ""

    def access_summary(self) -> tuple[str, str]:
        """Return ``(code, human)`` describing the tunnel's effective access control.

        ``code`` is one of ``'tenant' | 'anonymous' | 'org' | 'private' | 'unknown'``;
        ``human`` is a short phrase for logs / UI. Read back from ``access list`` so it
        reflects what actually stuck (not just what we asked for).
        """
        try:
            r = self._run("access", "list", self.tunnel_id)
        except Exception:  # noqa: BLE001
            return "unknown", "unknown"
        out = (r.stdout or "") + (r.stderr or "")
        low = out.lower()
        if "tenant" in low:
            m = re.search(r"\(([0-9a-fA-F-]{36})\)", out)
            tid = m.group(1) if m else ""
            return "tenant", "Microsoft Entra tenant" + (f" {tid}" if tid else "")
        if "anonymous" in low:
            return "anonymous", "anyone with the URL (anonymous)"
        if "org" in low or "organization" in low:
            return "org", "GitHub organization members"
        return "private", "only the signed-in host account (Microsoft)"

    def _apply_access(self) -> str:
        """Set the tunnel's access-control entry to match ``self.tunnel_auth``.

        Idempotent: clears any stale entries first (so switching away from a prior
        ``--allow-anonymous`` run actually takes effect), then adds the single entry
        for the current mode. The tunnel owner always keeps access regardless. Refreshes
        ``self.access_code`` / ``self.access_human`` and returns combined CLI output.
        """
        out: list[str] = []
        try:
            r0 = self._run("access", "reset", self.tunnel_id)
            out.append((r0.stdout or "") + (r0.stderr or ""))
            if self.tunnel_auth == "anonymous":
                r = self._run("access", "create", self.tunnel_id, "--anonymous")
            elif self._org_name:
                r = self._run("access", "create", self.tunnel_id, "--org", self._org_name)
            elif self.tunnel_auth == "tenant":
                r = self._run("access", "create", self.tunnel_id, "--tenant")
            else:
                # 'private' (default): owner-only. The reset above already cleared
                # any anonymous/tenant entries, leaving just the host account, which
                # still requires a Microsoft sign-in at the relay to connect.
                r = None
            if r is not None:
                out.append((r.stdout or "") + (r.stderr or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("devtunnel access setup (%s) failed: %s", self.tunnel_auth, exc)
        # Read back what actually stuck so callers can show/log the truth.
        self.access_code, self.access_human = self.access_summary()
        if self.tunnel_auth == "tenant" and self.access_code != "tenant":
            logger.error(
                "Tunnel auth 'tenant' did not apply (effective: %s). The devtunnel host "
                "is likely signed in with a PERSONAL Microsoft account; run "
                "`devtunnel user login` with a WORK/SCHOOL account for Entra-gated access.",
                self.access_human)
        else:
            logger.info("Tunnel access control: %s", self.access_human)
        return "\n".join(s for s in out if s).strip()

    def ensure(self, retries: int = 3) -> bool:
        """Create the tunnel + its http port mapping and apply access control.

        ``create`` returning "already exists" is success; a genuine failure
        (network / control-plane hiccup) is retried with a short backoff. The tunnel
        is always created *private*, then :meth:`_apply_access` sets the one
        access-control entry for the configured mode (Entra tenant / anonymous /
        GitHub org). Returns True once the tunnel is confirmed present.
        """
        import time

        last = ""
        for attempt in range(1, retries + 1):
            try:
                # Always create private; the access-control entry below decides who
                # may connect. (Re-creating an existing tunnel/port is a no-op.)
                r1 = self._run("create", self.tunnel_id)  # already-exists is fine
                # Port must be http (see module docstring).
                r2 = self._run("port", "create", self.tunnel_id, "-p", str(self.port),
                               "--protocol", "http")
                r3 = self._apply_access()
                last = ((r1.stdout or "") + (r1.stderr or "")
                        + (r2.stdout or "") + (r2.stderr or "")
                        + ("\n" + r3 if r3 else "")).strip()
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                logger.warning("devtunnel ensure attempt %d/%d errored: %s",
                               attempt, retries, exc)
            else:
                if self._tunnel_ready():
                    return True
            if attempt < retries:
                logger.warning("devtunnel ensure attempt %d/%d incomplete; retrying. %s",
                               attempt, retries, last[:200])
                time.sleep(min(2.0 * attempt, 6.0))
        logger.error("devtunnel ensure for '%s' not confirmed after %d attempts: %s",
                     self.tunnel_id, retries, last[:300])
        return False

    def _tunnel_ready(self) -> bool:
        """Best-effort check that the tunnel exists and lists our port."""
        try:
            r = self._run("show", self.tunnel_id)
        except Exception:  # noqa: BLE001
            return False
        out = (r.stdout or "") + (r.stderr or "")
        return self.tunnel_id in out and str(self.port) in out

    # -- async host ------------------------------------------------------

    async def host(self, url_timeout: float = 45.0, retries: int = 3,
                   backoff: float = 3.0) -> Optional[str]:
        """Ensure the tunnel exists and host it, retrying until a public URL is up.

        Hosting can fail transiently (network blips, control-plane hiccups, a
        missing port mapping). We retry the whole ensure -> spawn -> wait-for-URL
        cycle with a short backoff so a freshly-installed server reliably comes up
        with a public URL instead of silently falling back to LAN-only on the first
        failure. Returns the public HTTPS URL, or None if every attempt failed.
        """
        for attempt in range(1, retries + 1):
            try:
                self.ensure()
                url = await self._host_once(url_timeout)
            except Exception as exc:  # noqa: BLE001
                url = None
                logger.warning("devtunnel host attempt %d/%d errored: %s",
                               attempt, retries, exc)
            if url:
                if attempt > 1:
                    logger.info("devtunnel '%s' came up on attempt %d/%d",
                                self.tunnel_id, attempt, retries)
                return url
            # Failed: tear down the half-started process before trying again.
            await self._stop_proc()
            if attempt < retries:
                wait = backoff * attempt
                logger.warning("devtunnel host attempt %d/%d got no public URL; "
                               "retrying in %.0fs", attempt, retries, wait)
                await asyncio.sleep(wait)
        logger.error("devtunnel failed to host '%s' after %d attempts; LAN-only.%s",
                     self.tunnel_id, retries,
                     (" Last output: " + self.last_error) if self.last_error else "")
        return None

    async def _host_once(self, url_timeout: float = 45.0) -> Optional[str]:
        """One ``devtunnel host`` attempt: spawn, read output, return URL if seen."""
        self._proc = await asyncio.create_subprocess_exec(
            self.exe, "host", self.tunnel_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        public_url: Optional[str] = None
        captured: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + url_timeout

        assert self._proc.stdout is not None
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=deadline - loop.time())
            except asyncio.TimeoutError:
                break
            if not raw:
                break  # process exited
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                logger.debug("devtunnel: %s", line)
                captured.append(line)
            # The URL usually rides the "Connect via browser" line, but scan every
            # line so a CLI output-format change never leaves us URL-less.
            if not public_url:
                match = _URL_RE.search(line)
                if match:
                    public_url = match.group(0)
            if "Ready to accept connections" in line:
                break

        if public_url and self._proc.returncode is None:
            # Up: keep + background-drain the process; clear any prior error.
            self._drain_task = asyncio.create_task(self._drain())
            self.last_error = ""
        else:
            # Failed: remember what the CLI actually said so the UI can show it.
            self.last_error = "\n".join(captured[-8:]).strip()
        return public_url

    async def _drain(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                logger.debug("devtunnel: %s", raw.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        await self._stop_proc()

    def is_hosting(self) -> bool:
        """True if a ``devtunnel host`` child process is currently running."""
        return self._proc is not None and self._proc.returncode is None

    async def _stop_proc(self) -> None:
        """Terminate the current host process (on shutdown or between retries)."""
        if self._drain_task:
            self._drain_task.cancel()
            self._drain_task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None
