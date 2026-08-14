"""Claude CLI provider (Anthropic "Claude Code").

Drives the ``claude`` CLI in non-interactive print mode (``claude -p "<prompt>"``).
Discovered on PATH; if the CLI isn't installed the provider reports
``available=False`` and returns a helpful message instead of crashing.

Kept intentionally close to the Copilot runner: same ANSI scrub, the same
"non-positive timeout = wait forever" rule for long tasks, and the same sandbox
working directory. Claude manages its own session history, so the bridge's
session id is recorded but not forced onto the CLI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

from .base import AIProvider, AIResult

logger = logging.getLogger("copilot_bridge.providers.claude")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def discover_claude(explicit: str = "") -> str:
    """Locate the ``claude`` executable: explicit path -> PATH. '' if not found."""
    if explicit and os.path.isfile(explicit):
        return explicit
    return shutil.which("claude") or ""


class ClaudeProvider(AIProvider):
    name = "claude"
    display_name = "Claude CLI"

    def __init__(self, config):
        self.exe = discover_claude(getattr(config, "CLAUDE_PATH", "") or "")
        self.available = bool(self.exe)
        self.model = getattr(config, "CLAUDE_MODEL", "") or ""
        self.timeout = getattr(config, "COPILOT_TIMEOUT", 0)
        from paths import app_base_dir
        self.workdir = os.path.abspath(
            getattr(config, "COPILOT_WORKDIR", "")
            or str(app_base_dir() / "workspace")
        )
        os.makedirs(self.workdir, exist_ok=True)

    def _launcher(self) -> list[str]:
        low = self.exe.lower()
        if low.endswith(".ps1"):
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", self.exe]
        if low.endswith((".bat", ".cmd")):
            return ["cmd", "/c", self.exe]
        return [self.exe]

    def _build_args(self, prompt: str) -> list[str]:
        args = ["-p", prompt, "--output-format", "text"]
        if self.model:
            args += ["--model", self.model]
        return args

    async def run(
        self,
        prompt: str,
        conversation_id=None,
        new_session: bool = False,
        session_id: str | None = None,
        attachments: list[str] | None = None,
        history: list[dict] | None = None,
    ) -> AIResult:
        if not self.available:
            return AIResult(
                ok=False,
                text="Claude CLI is not installed on this host. Install it and set "
                     "AI_PROVIDER=claude (or CLAUDE_PATH).",
                exit_code=-1,
            )
        # Claude reads attachments from the prompt text/paths; for parity we append
        # any provided file paths so the model can see them.
        full_prompt = prompt
        if history:
            transcript = []
            for message in history:
                role = "User" if message.get("role") == "user" else "Assistant"
                text = message.get("text") or ""
                if text:
                    transcript.append(f"[{role}]\n{text}")
            if transcript:
                full_prompt = (
                    "Previous conversation context:\n\n" + "\n\n".join(transcript)
                    + f"\n\n[Current user request]\n{prompt}"
                )
        if attachments:
            joined = "\n".join(p for p in attachments if p)
            if joined:
                full_prompt = f"{prompt}\n\n[Attached files]\n{joined}"

        cmd = self._launcher() + self._build_args(full_prompt)
        logger.info("Running Claude: %s -p <prompt>", self.exe)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        timeout = self.timeout if (self.timeout and self.timeout > 0) else None
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AIResult(ok=False, text=f"\u23f1\ufe0f Claude timed out after {self.timeout}s.",
                            exit_code=-1, timed_out=True)
        text = _ANSI_RE.sub("", (stdout or b"").decode("utf-8", "replace")).strip()
        return AIResult(ok=proc.returncode == 0, text=text, exit_code=proc.returncode,
                        session_id=session_id)
