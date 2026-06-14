"""Pluggable AI backends for the Copilot Bridge app server.

The bridge originally drove only the GitHub Copilot CLI. This package abstracts
"the thing that turns a prompt into a reply" behind a small interface so other
tools (Claude CLI, OpenAI, …) can be plugged in and selected with the
``AI_PROVIDER`` setting — without touching the web/chat layer.

A provider must expose the same surface the rest of the server already relies on
(see :class:`AIProvider`): an async ``run(...)`` returning an :class:`AIResult`,
plus ``exe`` and ``workdir`` attributes used by ``/whoami`` and uploads.
"""

from .base import AIProvider, AIResult
from .registry import available_providers, build_provider

__all__ = ["AIProvider", "AIResult", "build_provider", "available_providers"]
