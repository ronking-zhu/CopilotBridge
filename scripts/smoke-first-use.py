"""Exercise frozen first use and the real repair callback in a disposable sandbox.

Run with the server virtual environment and --exe pointing at a frozen payload.
Never installs the app, contacts port 3978, starts a tunnel, syncs, or calls a model.
"""

import argparse
import ast
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import gui
import provisioning
from version import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    args = parser.parse_args()
    executable = args.exe.resolve(strict=True)
    if any((executable.parent / name).exists() for name in (".env", "portable")):
        raise SystemExit("Refusing to run a payload with portable/personal configuration")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port != 3978
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("ENTRA_", "MS_ENTRA_", "ONEDRIVE_", "KNOWLEDGE_", "TUNNEL_", "SESSION_WATCHER_", "COPILOT_")) or name in {
            "AUTH_MODE", "HOST", "PORT", "CHAT_API_TOKEN", "SESSIONS_DIR", "AI_PROVIDER", "CB_NO_GUI",
        }:
            env.pop(name, None)
    state = {"process": None, "key": "", "launches": 0}

    def request(method, path, body=None, *, keyed=False, timeout=5):
        headers = {"Content-Type": "application/json"}
        if keyed:
            headers["X-API-Key"] = state["key"]
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(base + path, headers=headers, data=data, method=method)
        try:
            response = urlopen(req, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw)

    def panel_http(method, path, body=None, timeout=5):
        status, result = request(method, path, body, keyed=True, timeout=timeout)
        if status >= 400:
            raise RuntimeError(f"Isolated {path}: HTTP {status}")
        return result

    def port_listening(value, host="127.0.0.1"):
        assert value == port
        assert host == "127.0.0.1"
        return gui._port_listening(value, host)

    def stop():
        process = state["process"]
        if process is not None and process.poll() is None:
            panel_http("POST", "/api/control/shutdown", {})
            process.wait(timeout=20)

    with tempfile.TemporaryDirectory(prefix="cb-frozen-first-use-") as temporary:
        sandbox = Path(temporary)
        data_dir = sandbox / "LocalAppData" / "CopilotBridge"
        data_dir.mkdir(parents=True)
        env_path = data_dir / ".env"
        env_path.write_text(
            f"HOST=127.0.0.1\nPORT={port}\nTUNNEL_ENABLED=false\n"
            "SESSION_WATCHER_ENABLED=false\nONEDRIVE_SYNC_ENABLED=false\n"
            f"KNOWLEDGE_EXTRACTION_ENABLED=false\nCOPILOT_WORKDIR={sandbox / 'workspace'}\n",
            encoding="utf-8",
        )
        env.update(LOCALAPPDATA=str(sandbox / "LocalAppData"), CB_NO_GUI="1", PYTHONUTF8="1")

        def start():
            old_process = state["process"]
            if old_process is not None:
                old_process.wait(timeout=20)
            ready = threading.Event()
            process = subprocess.Popen(
                [str(executable)], cwd=sandbox, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            state["process"] = process
            state["launches"] += 1

            def read_ready():
                with process.stdout:
                    for line in process.stdout:
                        if "Copilot Bridge is READY" in line:
                            ready.set()

            threading.Thread(target=read_ready, daemon=True).start()
            if not ready.wait(timeout=30):
                raise RuntimeError("Frozen sandbox did not signal readiness (logs not printed to protect generated keys)")
            card = json.loads((data_dir / "connection.json").read_text(encoding="utf-8"))
            state["key"] = card["apiKey"]
            assert request("GET", "/health")[1]["version"] == __version__
            return True, "Server started."

        try:
            start()
            assert request("GET", "/api/webconfig")[1] == {"authMode": "tunnel", "authRequired": False}
            status, conversation = request("POST", "/api/sessions", {"title": "中文验收 · 日本語"})
            assert status == 201
            assert request("PATCH", "/api/settings", {"uiLanguage": "en"})[0] == 200
            original_key = state["key"]
            stop()

            # Reproduce the screenshot's extra app-layer Entra policy without an
            # account login. This is synthetic state, never the user's settings.
            with patch.dict(os.environ), patch.object(provisioning, "DEFAULT_ENV_PATH", env_path):
                provisioning.set_identity_config(env_path, auth_mode="entra")
                start()
                assert request("GET", "/api/webconfig")[1]["authMode"] == "entra"
                assert request("GET", "/api/sync/status", keyed=True)[0] == 401
                assert request("GET", "/api/control/sync/status", keyed=True)[0] == 200
                assert request("GET", "/api/control/sync/status")[0] == 401
                tree = ast.parse(inspect.getsource(gui.run_control_panel))
                callbacks = [node for node in tree.body[0].body if isinstance(node, ast.FunctionDef) and node.name in {
                    "fetch_status", "act_start_server", "act_stop_server", "act_fix_local_signin",
                }]
                browser = Mock()
                scope = dict(
                    cfg=SimpleNamespace(HOST="127.0.0.1", TUNNEL_AUTH="private", AUTH_MODE="entra"),
                    net={"port": port}, _port_listening=port_listening, http=panel_http,
                    time=time, spawn_server=start, webbrowser=browser,
                    _phost="127.0.0.1", urllib=gui.urllib,
                    _dashboard_url=lambda: base + "/?dashboard=1",
                )
                exec(compile(ast.Module(body=callbacks, type_ignores=[]), "real-panel-callbacks", "exec"), scope)
                original_pid = state["process"].pid
                assert scope["act_start_server"]()[0]
                assert state["process"].pid == original_pid
                assert state["launches"] == 2
                ok, message = scope["act_fix_local_signin"]()
                assert ok, message
                assert state["launches"] == 3
                browser.open.assert_called_once()

            assert request("GET", "/api/webconfig")[1] == {"authMode": "tunnel", "authRequired": False}
            assert request("GET", "/api/settings")[1]["uiLanguage"] == "en"
            assert request("GET", f"/api/sessions/{conversation['id']}")[1]["title"] == "中文验收 · 日本語"
            assert state["key"] == original_key
            assert request("GET", "/api/control/status")[0] == 401
            assert request("GET", "/api/control/sync/status")[0] == 401
            assert request("GET", "/api/knowledge/generation-jobs")[1]["count"] == 0
            ok, message = scope["act_stop_server"]()
            assert ok, message
            state["process"].wait(timeout=20)
            assert not port_listening(port)
            print(json.dumps({"frozenVersion": __version__, "launches": state["launches"], "cleanFirstUse": "passed", "entraReproduction": "passed", "realRepairCallback": "passed", "realStartGuard": "passed", "realScopedStop": "passed", "dataAndKeyPreserved": True, "isolatedPortClosed": True}))
        finally:
            process = state["process"]
            if process is not None and process.poll() is None:
                try:
                    stop()
                finally:
                    if process.poll() is None:
                        process.kill()  # only the process created by this test
                        process.wait(timeout=10)


if __name__ == "__main__":
    main()