"""Regression checks for installer-driven browser notification setup.

Run: python tests/notification_setup_test.py (from the server/ directory)
"""

import os
import sys
from unittest.mock import patch

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import launcher


def main() -> None:
    with patch("webbrowser.open", return_value=True) as opened:
        assert launcher._open_notification_setup("http://localhost:3978/") is True
        opened.assert_called_with(
            "http://localhost:3978/?dashboard=1&notificationSetup=1&syncSetup=1", new=1
        )
        assert launcher._open_notification_setup("http://0.0.0.0:3978") is True
        opened.assert_called_with(
            "http://localhost:3978/?dashboard=1&notificationSetup=1&syncSetup=1", new=1
        )

    root = os.path.dirname(_SERVER_DIR)
    with open(os.path.join(root, "installer", "CopilotBridgeServer.iss"), encoding="utf-8-sig") as file:
        installer = file.read()
    assert 'Parameters: "--notification-setup"' in installer

    with open(os.path.join(_SERVER_DIR, "webapp", "index.html"), encoding="utf-8") as file:
        webapp = file.read()
    assert 'id="notificationSetupNotice"' in webapp
    assert "initialParams.get('notificationSetup')" in webapp
    assert 'id="syncStatusMessage"' in webapp
    assert 'id="connectSyncBtn"' in webapp
    assert "initialParams.get('syncSetup')" in webapp

    print("ALL NOTIFICATION SETUP TESTS PASSED")


if __name__ == "__main__":
    main()