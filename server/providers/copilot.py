"""Copilot CLI provider — wraps the existing :class:`CopilotRunner`."""

from __future__ import annotations

from copilot_runner import CopilotRunner

from .base import AIProvider, AIResult


class CopilotProvider(AIProvider):
    """Drives the GitHub Copilot CLI via the original runner.

    Thin adapter: it owns a :class:`CopilotRunner` and forwards calls, so all the
    battle-tested session/attachment/retry logic is reused unchanged.
    """

    name = "copilot"
    display_name = "GitHub Copilot CLI"

    def __init__(self, config):
        self._runner = CopilotRunner(config)
        self.exe = self._runner.exe
        self.workdir = self._runner.workdir
        self.available = bool(self._runner.exe)

    async def run(
        self,
        prompt: str,
        conversation_id=None,
        new_session: bool = False,
        session_id: str | None = None,
        attachments: list[str] | None = None,
        history: list[dict] | None = None,
    ) -> AIResult:
        r = await self._runner.run(
            prompt,
            conversation_id=conversation_id,
            new_session=new_session,
            session_id=session_id,
            attachments=attachments,
            history=history,
        )
        return AIResult(
            ok=r.ok, text=r.text, exit_code=r.exit_code,
            timed_out=r.timed_out, session_id=r.session_id,
        )
