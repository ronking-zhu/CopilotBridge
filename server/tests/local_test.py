"""Validate the Copilot pipeline without Teams or HTTP.

Runs a prompt straight through CopilotRunner and prints the result. Useful to confirm
the CLI is installed, authenticated, and reachable before wiring up the bot.

    python local_test.py
    python local_test.py "list the files in this folder"
"""

import asyncio
import os
import sys

# Allow running from server/tests/ by putting the server package dir on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import DefaultConfig
from copilot_runner import CopilotRunner


async def main():
    prompt = " ".join(sys.argv[1:]) or "Reply with exactly: BRIDGE_PIPELINE_OK"
    config = DefaultConfig()
    runner = CopilotRunner(config)
    print(f"Copilot exe : {runner.exe}")
    print(f"Working dir : {runner.workdir}")
    print(f"Scope       : {config.COPILOT_SCOPE}")
    print(f"Prompt      : {prompt}")
    print("Running...\n")

    result = await runner.run(prompt, conversation_id="local-test")

    print(f"ok={result.ok} exit={result.exit_code} timed_out={result.timed_out}")
    print("---- output ----")
    print(result.text)


if __name__ == "__main__":
    asyncio.run(main())
