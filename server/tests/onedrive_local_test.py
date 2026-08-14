"""Tests local OneDrive folder fallback without Graph application login.

Run: python tests/onedrive_local_test.py (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from onedrive_local import LocalOneDriveConnection, detect_onedrive_root


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "OneDrive - Contoso"
        root.mkdir()
        marker = Path(tmp) / "onedrive-local.enabled"

        detected = detect_onedrive_root(str(root))
        assert detected == root

        connection = LocalOneDriveConnection(root, marker_path=marker)
        before = connection.describe()
        assert before["configured"] is True
        assert before["connected"] is False
        assert before["mode"] == "local-folder"
        assert before["root"] == str(root / "Apps" / "Copilot Bridge")

        connected = await connection.connect()
        assert connected["connected"] is True
        assert marker.exists()
        assert Path(connected["root"]).is_dir()

        # A fresh process observes the persisted opt-in, but disconnecting keeps
        # the synchronized files and only removes automatic-sync authorization.
        restored = LocalOneDriveConnection(root, marker_path=marker)
        assert restored.describe()["connected"] is True
        await restored.disconnect()
        assert restored.describe()["connected"] is False
        assert Path(connected["root"]).is_dir()

    print("ALL LOCAL ONEDRIVE FALLBACK TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())