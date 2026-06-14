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

    def __init__(self, exe: str, tunnel_id: str, port: int, anonymous: bool = True):
        self.exe = exe
        self.tunnel_id = tunnel_id
        self.port = port
        self.anonymous = anonymous
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._drain_task: Optional[asyncio.Task] = None

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

    def logged_in_user(self) -> str:
        """Best-effort signed-in identity (email/username), or '' if unknown."""
        try:
            result = self._run("user", "show")
        except Exception:  # noqa: BLE001
            return ""
        out = (result.stdout or "") + (result.stderr or "")
        for line in out.splitlines():
            s = line.strip()
            if "@" in s:
                return s.split()[-1] if " " in s else s
        return ""

    def ensure(self, retries: int = 3) -> bool:
        """Create the tunnel and its http port mapping, retrying transient failures.

        ``create`` returning "already exists" is success; a genuine failure
        (network / control-plane hiccup) is retried with a short backoff. Returns
        True once the tunnel is confirmed present (best-effort via ``show``); the
        definitive success signal is :meth:`host` actually getting a public URL.
        """
        import time

        last = ""
        for attempt in range(1, retries + 1):
            create_args = ["create", self.tunnel_id]
            if self.anonymous:
                create_args.append("--allow-anonymous")
            try:
                r1 = self._run(*create_args)  # already-exists is fine
                # Port must be http (see module docstring). Re-creating an existing
                # mapping is a harmless no-op.
                r2 = self._run("port", "create", self.tunnel_id, "-p", str(self.port),
                               "--protocol", "http")
                last = ((r1.stdout or "") + (r1.stderr or "")
                        + (r2.stdout or "") + (r2.stderr or "")).strip()
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
        logger.error("devtunnel failed to host '%s' after %d attempts; LAN-only.",
                     self.tunnel_id, retries)
        return None

    async def _host_once(self, url_timeout: float = 45.0) -> Optional[str]:
        """One ``devtunnel host`` attempt: spawn, read output, return URL if seen."""
        self._proc = await asyncio.create_subprocess_exec(
            self.exe, "host", self.tunnel_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        public_url: Optional[str] = None
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
            # The URL usually rides the "Connect via browser" line, but scan every
            # line so a CLI output-format change never leaves us URL-less.
            if not public_url:
                match = _URL_RE.search(line)
                if match:
                    public_url = match.group(0)
            if "Ready to accept connections" in line:
                break

        # Only keep + background-drain the process if it's actually up with a URL.
        if public_url and self._proc.returncode is None:
            self._drain_task = asyncio.create_task(self._drain())
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
