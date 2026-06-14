"""Teams bot that bridges incoming messages to the GitHub Copilot CLI."""

import asyncio
import logging
import uuid
from typing import Dict, List, Optional

from botbuilder.core import ActivityHandler, MessageFactory, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ChannelAccount, ConversationReference

from config import DefaultConfig
from copilot_runner import CopilotRunner

logger = logging.getLogger("copilot_bridge.bot")

HELP_TEXT = (
    "**GitHub Copilot CLI bridge**\n\n"
    "Send me an instruction and I'll run it through the GitHub Copilot CLI on the host "
    "machine, then return the result.\n\n"
    "Commands:\n"
    "- `/help` — show this help\n"
    "- `/whoami` — show your identity (use this to get added to the allowlist)\n"
    "- `/reset` — start a fresh Copilot session for this chat\n"
    "- `/status` — show bridge configuration\n"
)


class CopilotBridgeBot(ActivityHandler):
    def __init__(self, adapter, config: DefaultConfig, runner: CopilotRunner):
        self.adapter = adapter
        self.config = config
        self.runner = runner
        # continue_conversation needs an app id; a random one is fine for local/emulator use.
        self.app_id = config.APP_ID or str(uuid.uuid4())
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pending_reset: set = set()

    # -- helpers ----------------------------------------------------------

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock
        return lock

    def _is_authorized(self, turn_context: TurnContext) -> bool:
        allowed = self.config.ALLOWED_USER_IDS
        if not allowed:
            return True
        sender = turn_context.activity.from_property
        candidate_ids = {
            getattr(sender, "aad_object_id", None),
            getattr(sender, "id", None),
        }
        return any(cid in allowed for cid in candidate_ids if cid)

    def _status_text(self) -> str:
        return (
            "**Bridge status**\n"
            f"- Copilot CLI: `{self.runner.exe}`\n"
            f"- Scope: `{self.config.COPILOT_SCOPE}`\n"
            f"- Working dir: `{self.runner.workdir}`\n"
            f"- Timeout: {self.config.COPILOT_TIMEOUT}s\n"
            f"- Session continuity: {self.config.COPILOT_SESSION_CONTINUITY}\n"
            f"- Allowlist entries: {len(self.config.ALLOWED_USER_IDS)}\n"
        )

    def _chunk(self, text: str) -> List[str]:
        limit = self.config.MAX_REPLY_CHARS
        if len(text) > limit:
            text = text[:limit] + "\n\n…(output truncated)"
        size = self.config.CHUNK_CHARS
        if len(text) <= size:
            return [text]
        return [text[i:i + size] for i in range(0, len(text), size)]

    # -- activity handlers ------------------------------------------------

    async def on_members_added_activity(
        self, members_added: List[ChannelAccount], turn_context: TurnContext
    ):
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                await turn_context.send_activity(MessageFactory.text(HELP_TEXT))

    async def on_message_activity(self, turn_context: TurnContext):
        TurnContext.remove_recipient_mention(turn_context.activity)
        text = (turn_context.activity.text or "").strip()
        sender = turn_context.activity.from_property
        command = text.lower()

        # /whoami works for everyone so users can discover the id to allowlist.
        if command in ("/whoami", "whoami"):
            await turn_context.send_activity(MessageFactory.text(
                f"name: {getattr(sender, 'name', '?')}\n"
                f"aadObjectId: {getattr(sender, 'aad_object_id', None) or '(none)'}\n"
                f"id: {getattr(sender, 'id', '?')}"
            ))
            return

        if not self._is_authorized(turn_context):
            logger.warning(
                "Unauthorized message from %s (%s)",
                getattr(sender, "name", "?"),
                getattr(sender, "aad_object_id", None),
            )
            await turn_context.send_activity(MessageFactory.text(
                "\u26d4 You are not authorized to use this bot.\n"
                "Send `/whoami` to get your AAD object id and ask the owner to add it to "
                "ALLOWED_USER_IDS."
            ))
            return

        if command in ("/help", "help", "/start", "start"):
            await turn_context.send_activity(MessageFactory.text(HELP_TEXT))
            return

        if command in ("/status", "status"):
            await turn_context.send_activity(MessageFactory.text(self._status_text()))
            return

        if command in ("/reset", "reset"):
            self._pending_reset.add(turn_context.activity.conversation.id)
            await turn_context.send_activity(MessageFactory.text(
                "\U0001f504 A fresh Copilot session will start on your next message."
            ))
            return

        if not text:
            await turn_context.send_activity(MessageFactory.text(
                "Send a prompt and I'll run it through GitHub Copilot CLI. Type `/help` for options."
            ))
            return

        # Acknowledge immediately, then do the (potentially slow) work in the background
        # and deliver the result proactively. This keeps the inbound HTTP request short
        # and avoids channel-side retries / duplicate runs.
        await turn_context.send_activity(Activity(type=ActivityTypes.typing))
        await turn_context.send_activity(MessageFactory.text(
            "\U0001f916 Working on it… running Copilot CLI on the host."
        ))

        reference = TurnContext.get_conversation_reference(turn_context.activity)
        asyncio.create_task(self._run_and_reply(reference, text))

    # -- background processing -------------------------------------------

    async def _run_and_reply(self, reference: ConversationReference, prompt: str):
        conversation_id = reference.conversation.id
        new_session = conversation_id in self._pending_reset
        self._pending_reset.discard(conversation_id)

        async with self._lock(conversation_id):
            try:
                result = await self.runner.run(
                    prompt, conversation_id=conversation_id, new_session=new_session
                )
                payload = result.text or "(no output)"
                if not result.ok and not result.timed_out:
                    payload = f"\u26a0\ufe0f Copilot exited with code {result.exit_code}.\n\n{payload}"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Copilot run failed")
                payload = f"\u274c Error running Copilot CLI: {exc}"

        chunks = self._chunk(payload)

        async def _send(turn_context: TurnContext):
            for chunk in chunks:
                await turn_context.send_activity(MessageFactory.text(chunk))

        await self.adapter.continue_conversation(reference, _send, self.app_id)
