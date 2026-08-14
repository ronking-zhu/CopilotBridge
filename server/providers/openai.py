"""OpenAI provider — calls the OpenAI Chat Completions HTTP API.

Unlike the CLI providers this one has no local sandbox or tools; it's a plain
text backend that answers from the model. It uses ``aiohttp`` (already a server
dependency) so no extra package is needed, and is only ``available`` when an API
key is configured (``OPENAI_API_KEY``). The base URL is overridable
(``OPENAI_BASE_URL``) so OpenAI-compatible endpoints (Azure OpenAI, local
servers, etc.) work too.
"""

from __future__ import annotations

import logging

import aiohttp

from .base import AIProvider, AIResult

logger = logging.getLogger("copilot_bridge.providers.openai")


class OpenAIProvider(AIProvider):
    name = "openai"
    display_name = "OpenAI API"

    def __init__(self, config):
        self.api_key = getattr(config, "OPENAI_API_KEY", "") or ""
        self.base_url = (getattr(config, "OPENAI_BASE_URL", "") or "https://api.openai.com/v1").rstrip("/")
        self.model = getattr(config, "OPENAI_MODEL", "") or "gpt-4o-mini"
        self.timeout = getattr(config, "COPILOT_TIMEOUT", 0)
        self.exe = f"{self.base_url} ({self.model})"
        self.workdir = ""
        self.available = bool(self.api_key)

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
                text="OpenAI provider needs OPENAI_API_KEY. Set it and AI_PROVIDER=openai.",
                exit_code=-1,
            )
        messages = []
        for message in history or []:
            role = message.get("role")
            text = message.get("text") or ""
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": text})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self.model, "messages": messages}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # Non-positive timeout = wait as long as needed (parity with the CLIs).
        total = self.timeout if (self.timeout and self.timeout > 0) else None
        try:
            timeout = aiohttp.ClientTimeout(total=total)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.base_url}/chat/completions",
                                        json=payload, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
                        return AIResult(ok=False, text=f"OpenAI HTTP {resp.status}: {msg or data}",
                                        exit_code=resp.status)
                    text = (data["choices"][0]["message"]["content"] or "").strip()
                    return AIResult(ok=True, text=text, exit_code=0, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - network/JSON errors become a normal failure
            logger.warning("OpenAI request failed: %s", exc)
            return AIResult(ok=False, text=f"OpenAI request failed: {exc}", exit_code=-1)
