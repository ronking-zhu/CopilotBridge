"""Regression checks for native Control Panel OneDrive controls.

Run: python tests/control_panel_sync_test.py (from the server/ directory)
"""

import inspect
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import gui


def main() -> None:
    disconnected = gui._format_sync_status({
        "enabled": True,
        "running": False,
        "pendingEventCount": 70,
        "auth": {"configured": True, "connected": False},
    })
    assert "Not connected" in disconnected
    assert "70" in disconnected

    assert "Syncing" in gui._format_sync_status({
        "enabled": True, "running": True,
        "auth": {"configured": True, "connected": True},
    })

    connected = gui._format_sync_status({
        "enabled": True,
        "running": False,
        "pendingEventCount": 0,
        "lastSuccess": 1,
        "lastResult": {"pushedEventCount": 12, "pulledEventCount": 5},
        "auth": {
            "configured": True, "connected": True,
            "username": "person@example.com",
        },
    })
    assert "person@example.com" in connected
    assert "uploaded 12" in connected
    assert "downloaded 5" in connected

    local_folder = gui._format_sync_status({
        "enabled": True,
        "running": False,
        "pendingEventCount": 0,
        "lastSuccess": 1,
        "auth": {
            "configured": True,
            "connected": True,
            "mode": "local-folder",
            "scope": "Local OneDrive folder",
            "username": "OneDrive - Microsoft",
            "root": r"C:\Users\person\OneDrive - Microsoft\Apps\Copilot Bridge",
        },
    })
    assert "Local OneDrive folder" in local_folder
    assert "OneDrive - Microsoft" in local_folder
    assert r"C:\Users\person\OneDrive - Microsoft\Apps\Copilot Bridge" in local_folder

    source = inspect.getsource(gui.run_control_panel)
    for required in (
        "/api/sync/status", "/api/sync/connect", "/api/sync/run",
        "/api/sync/disconnect", "Open AI Dashboard", "Upload + download now",
    ):
        assert required in source, required

    launcher_path = os.path.join(_SERVER_DIR, "launcher.py")
    with open(launcher_path, encoding="utf-8") as file:
        launcher = file.read()
    assert "server_thread.join()" in launcher
    assert "until the control API performs a real server shutdown" in launcher

    root = os.path.dirname(_SERVER_DIR)
    with open(
        os.path.join(root, "installer", "CopilotBridgeServer.iss"),
        encoding="utf-8-sig",
    ) as file:
        installer = file.read()
    assert 'Name: "{autodesktop}\\Copilot Bridge Control Panel"' in installer
    assert 'Parameters: "--control-panel"' in installer
    assert "Open Copilot Bridge Control Panel" in installer

    print("ALL CONTROL PANEL SYNC TESTS PASSED")


if __name__ == "__main__":
    main()