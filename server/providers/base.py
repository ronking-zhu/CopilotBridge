"""Provider interface + result type shared by every AI backend."""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class AIResult:
    """Outcome of a single provider run.

    Field names match the legacy ``CopilotResult`` so existing callers (webchat,
    bot) keep working unchanged.
    """

    ok: bool
    text: str
    exit_code: int = 0
    timed_out: bool = False
    session_id: str | None = None


class AIProvider(abc.ABC):
    """Common surface for an AI backend that turns a prompt into a reply.

    Implementations should be cheap to construct; do tool discovery in
    :meth:`__init__` and report it via :attr:`available`. ``run`` must never
    raise for an expected failure — return ``AIResult(ok=False, ...)`` instead.
    """

    #: Stable id used by the ``AI_PROVIDER`` setting (e.g. "copilot", "claude").
    name: str = "base"
    #: Human-readable label for banners / the API.
    display_name: str = "AI Provider"

    #: Path/identifier of the underlying executable or endpoint (for /whoami).
    exe: str = ""
    #: Sandbox working directory uploads are written under.
    workdir: str = ""
    #: Whether the backend was found and is usable on this host.
    available: bool = False

    @abc.abstractmethod
    async def run(
        self,
        prompt: str,
        conversation_id=None,
        new_session: bool = False,
        session_id: str | None = None,
        attachments: list[str] | None = None,
    ) -> AIResult:
        """Execute one prompt and return the reply."""
        raise NotImplementedError

    def describe(self) -> dict:
        """Small JSON-friendly summary for ``/api/providers`` and ``/health``."""
        return {
            "name": self.name,
            "displayName": self.display_name,
            "exe": self.exe,
            "available": self.available,
        }
