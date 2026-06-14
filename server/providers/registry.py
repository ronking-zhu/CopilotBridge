"""Provider registry + factory.

``build_provider(config)`` returns the single active provider chosen by the
``AI_PROVIDER`` setting (default ``copilot``). ``available_providers(config)``
constructs each known provider just to report whether it's usable on this host —
handy for the ``/api/providers`` discovery endpoint and the startup banner.

Adding a new backend is a two-line change here plus one provider module.
"""

from __future__ import annotations

import logging

from .base import AIProvider
from .claude import ClaudeProvider
from .copilot import CopilotProvider
from .openai import OpenAIProvider

logger = logging.getLogger("copilot_bridge.providers")

# name -> provider class. Insertion order is the discovery order.
_REGISTRY: dict[str, type[AIProvider]] = {
    CopilotProvider.name: CopilotProvider,
    ClaudeProvider.name: ClaudeProvider,
    OpenAIProvider.name: OpenAIProvider,
}


def provider_names() -> list[str]:
    return list(_REGISTRY)


def build_provider(config) -> AIProvider:
    """Construct the active provider selected by ``config.AI_PROVIDER``.

    Unknown names fall back to Copilot so a typo never takes the server down.
    """
    requested = (getattr(config, "AI_PROVIDER", "") or "copilot").strip().lower()
    cls = _REGISTRY.get(requested)
    if cls is None:
        logger.warning("Unknown AI_PROVIDER=%r; falling back to 'copilot'. Known: %s",
                       requested, ", ".join(_REGISTRY))
        cls = CopilotProvider
    provider = cls(config)
    if not provider.available:
        logger.warning("AI provider '%s' is selected but not available on this host "
                       "(exe/key missing). Requests will return a setup hint.", provider.name)
    return provider


def available_providers(config) -> list[dict]:
    """Return ``describe()`` for every known provider (best-effort construction)."""
    out: list[dict] = []
    for name, cls in _REGISTRY.items():
        try:
            out.append(cls(config).describe())
        except Exception as exc:  # noqa: BLE001 - discovery must never crash
            logger.debug("describe %s failed: %s", name, exc)
            out.append({"name": name, "available": False})
    return out
