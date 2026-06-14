"""Async wrapper around the GitHub Copilot CLI.

Runs ``copilot -p "<prompt>"`` non-interactively and returns the textual result.
Designed to be driven by the Teams bot: one prompt in, one answer out, with optional
per-conversation session continuity and a hard timeout.
"""

import asyncio
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass

logger = logging.getLogger("copilot_bridge.runner")

# Strip ANSI escape sequences in case --no-color is not fully honoured.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Default location of the winget-installed copilot launcher on Windows.
_WINGET_LINK = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\copilot.exe"
)


def discover_copilot(explicit: str = "") -> str:
    """Locate the Copilot CLI executable.

    Preference order: explicit path -> a real .exe on PATH -> the winget link ->
    whatever ``copilot`` resolves to on PATH (a .bat/.ps1 shim, handled via a shell).
    """
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"COPILOT_PATH does not exist: {explicit}")

    on_path = shutil.which("copilot")
    if on_path and on_path.lower().endswith(".exe"):
        return on_path
    if os.path.isfile(_WINGET_LINK):
        return _WINGET_LINK
    if on_path:
        return on_path
    raise FileNotFoundError(
        "Could not find the GitHub Copilot CLI. Install it "
        "(winget install GitHub.Copilot) or set COPILOT_PATH in .env."
    )


@dataclass
class CopilotResult:
    ok: bool
    text: str
    exit_code: int
    timed_out: bool = False
    # The session id actually passed to Copilot via --session-id (None if none,
    # e.g. after a stateless retry).
    session_id: str | None = None


class CopilotRunner:
    def __init__(self, config):
        self.exe = discover_copilot(config.COPILOT_PATH)
        self.scope = config.COPILOT_SCOPE
        self.timeout = config.COPILOT_TIMEOUT
        self.model = config.COPILOT_MODEL
        self.session_continuity = config.COPILOT_SESSION_CONTINUITY
        from paths import app_base_dir
        self.workdir = os.path.abspath(
            config.COPILOT_WORKDIR
            or str(app_base_dir() / "workspace")
        )
        os.makedirs(self.workdir, exist_ok=True)
        # conversation id -> copilot session uuid
        self._sessions: dict[str, str] = {}

    # -- internal helpers -------------------------------------------------

    def _launcher(self) -> list[str]:
        """Return the program (and prefix args) used to start Copilot.

        A real .exe is launched directly; .bat/.cmd/.ps1 shims are launched
        through their interpreter so asyncio can spawn them on Windows.
        """
        low = self.exe.lower()
        if low.endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.exe]
        if low.endswith((".bat", ".cmd")):
            return ["cmd", "/c", self.exe]
        return [self.exe]

    def _session_for(self, conversation_id, new_session) -> str | None:
        if not (self.session_continuity and conversation_id):
            return None
        if new_session or conversation_id not in self._sessions:
            self._sessions[conversation_id] = str(uuid.uuid4())
        return self._sessions[conversation_id]

    def _build_args(self, prompt: str, session_id: str | None, attachments: list[str] | None = None) -> list[str]:
        args = ["-p", prompt, "-s", "--no-color"]
        if self.scope == "full":
            args.append("--allow-all")
        else:
            # Allow tools to run (required for non-interactive mode) but keep file
            # access limited to the sandbox working directory.
            args += ["--allow-all-tools", "-C", self.workdir, "--add-dir", self.workdir]
        if self.model:
            args += ["--model", self.model]
        if session_id:
            args += ["--session-id", session_id]
        # Image / document attachments are read by the CLI and added to the initial
        # prompt (vision). Each must be a real file path; callers save uploads under
        # the workdir so they're inside --add-dir.
        for path in attachments or []:
            if path:
                args += ["--attachment", path]
        return args

    async def _run_once(self, args: list[str]) -> CopilotResult:
        cmd = self._launcher() + args
        logger.info("Running Copilot: %s", " ".join(cmd[:-len(args)] + ["-p", "<prompt>"] + args[2:]))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        # A non-positive timeout means "no limit": long-running AI tasks may take
        # hours (or more), so we wait indefinitely and let the client display a
        # progress indicator. asyncio.wait_for(..., timeout=None) waits forever.
        timeout = self.timeout if (self.timeout and self.timeout > 0) else None
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("Copilot timed out after %ss", self.timeout)
            return CopilotResult(
                ok=False,
                text=f"\u23f1\ufe0f Copilot timed out after {self.timeout}s.",
                exit_code=-1,
                timed_out=True,
            )
        text = _ANSI_RE.sub("", (stdout or b"").decode("utf-8", "replace")).strip()
        return CopilotResult(ok=proc.returncode == 0, text=text, exit_code=proc.returncode)

    # -- public API -------------------------------------------------------

    async def run(self, prompt: str, conversation_id=None, new_session=False, session_id=None,
                  attachments: list[str] | None = None) -> CopilotResult:
        # An explicit session_id (e.g. from the persistent SessionStore) is used
        # directly as the Copilot --session-id. Otherwise fall back to the legacy
        # in-memory per-conversation mapping that bot.py / local_test.py rely on.
        if session_id:
            used = session_id
        else:
            used = self._session_for(conversation_id, new_session)

        result = await self._run_once(self._build_args(prompt, used, attachments))
        result.session_id = used

        # If resuming an existing session failed, retry once statelessly; the
        # retried result reflects that no session id was used. Attachments are
        # re-sent so the image context isn't lost on the retry.
        if not result.ok and used and not result.timed_out:
            logger.info("Retrying without session after failure (exit=%s)", result.exit_code)
            retry = await self._run_once(self._build_args(prompt, None, attachments))
            retry.session_id = None
            if retry.ok:
                if not session_id:
                    self._sessions.pop(conversation_id, None)
                return retry
        return result
