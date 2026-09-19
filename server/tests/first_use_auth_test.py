"""Offline regressions for clean installs, explicit local repair, and panel auth.

All HTTP servers use temporary data and OS-assigned ports. No Microsoft sign-in,
native conversation discovery, OneDrive transport, or model call is performed.
"""

import ast
import asyncio
from contextlib import ExitStack
import importlib
import inspect
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from auth import Authenticator
import config
from control import setup_control_routes
import gui
import paths
import provisioning
import webchat


class FirstUseAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_path = self.root / ".env"
        self.stack = ExitStack()
        # Exercise real defaults, rather than masking them with AUTH_MODE in the
        # acceptance terminal or loading the developer's personal configuration.
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(paths, "app_base_dir", return_value=self.root))
        self.stack.enter_context(patch.object(provisioning, "DEFAULT_ENV_PATH", self.env_path))
        self.stack.enter_context(patch.object(webchat, "app_base_dir", return_value=self.root))
        self.clients = []

    async def asyncTearDown(self):
        try:
            for client in self.clients:
                await client.close()
        finally:
            self.stack.close()
            self.tmp.cleanup()

    def load_config(self):
        cfg = importlib.reload(config).DefaultConfig()
        cfg.SESSIONS_DIR = str(self.root / "sessions")
        cfg.SESSION_WATCHER_ENABLED = False
        cfg.ONEDRIVE_SYNC_ENABLED = False
        return cfg

    async def client_for(self, cfg):
        app = web.Application()
        runner = SimpleNamespace(name="copilot", workdir=str(self.root / "workspace"))
        webchat.setup_web_routes(app, cfg, runner)
        setup_control_routes(app, cfg)
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)
        return client

    async def test_clean_install_and_nondefault_port_need_no_web_login(self):
        key, created = provisioning.ensure_api_token(self.env_path)
        self.assertTrue(created)
        provisioning.ensure_tunnel_id(self.env_path)
        cfg = self.load_config()
        self.assertEqual(cfg.AUTH_MODE, "tunnel")
        self.assertEqual(cfg.HOST, "localhost")
        self.assertEqual(cfg.PORT, 3978)  # inspected only; never bound/contacted
        self.assertEqual(cfg.TUNNEL_AUTH, "private")
        provisioning.set_port(13978, self.env_path)
        cfg = self.load_config()
        self.assertEqual(cfg.PORT, 13978)
        client = await self.client_for(cfg)
        response = await client.get("/api/webconfig")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        self.assertEqual(await response.json(), {"authMode": "tunnel", "authRequired": False})
        for path in ("/api/sessions", "/api/settings", "/api/dashboard", "/api/sync/status"):
            self.assertEqual((await client.get(path)).status, 200, path)
        html = await (await client.get("/")).text()
        self.assertNotIn(key, html)
        self.assertNotIn("<script>window.__CB_KEY=", html)
        self.assertEqual((await client.get("/api/control/status")).status, 401)
        self.assertEqual((await client.get("/api/control/sync/status")).status, 401)
        self.assertEqual((await client.get("/api/control/sync/status", headers={"X-API-Key": key})).status, 200)

    async def test_panel_opened_before_first_server_start_picks_up_created_key(self):
        panel_config = self.load_config()
        self.assertEqual(gui._control_panel_key(panel_config), "")
        key, _ = provisioning.ensure_api_token(self.env_path)
        self.assertEqual(panel_config.CHAT_API_TOKEN, "")
        self.assertEqual(gui._control_panel_key(panel_config), key)
        # An explicit process-level credential still takes precedence over a file.
        panel_config.CHAT_API_TOKEN = "explicit-control-override"
        self.assertEqual(gui._control_panel_key(panel_config), "explicit-control-override")

    async def test_entra_web_policy_does_not_block_native_sync_controls(self):
        os.environ["AUTH_MODE"] = "entra"
        key, _ = provisioning.ensure_api_token(self.env_path)
        cfg = self.load_config()
        client = await self.client_for(cfg)
        self.assertEqual((await (await client.get("/api/webconfig")).json())["authMode"], "entra")
        for method, action in (("GET", "status"), ("POST", "connect"), ("POST", "run"), ("POST", "disconnect")):
            with self.subTest(action=action):
                public_path = f"/api/sync/{action}"
                control_path = f"/api/control/sync/{action}"
                self.assertEqual((await client.request(method, public_path, headers={"X-API-Key": key})).status, 401)
                for suffix, headers in (("", {}), ("", {"X-API-Key": "wrong"}), (f"?key={key}", {})):
                    self.assertEqual((await client.request(method, control_path + suffix, headers=headers)).status, 401)
                response = await client.request(method, control_path, headers={"X-API-Key": key})
                # Sync is intentionally disabled: authorization should reach its
                # normal status/disabled response, not perform a cloud operation.
                self.assertEqual(response.status, 200 if action == "status" else 503)
                self.assertIs((await response.json())["enabled"], False)
        self.assertEqual((await client.get("/api/settings", headers={"X-API-Key": key})).status, 401)
        with patch("auth._EntraValidator.validate", return_value=(True, {"user": "member@example.com"}, "")):
            headers = {"Authorization": "Bearer header." + "payload" * 8 + ".signature"}
            self.assertEqual((await client.get("/api/sync/status", headers=headers)).status, 200)
            self.assertEqual((await client.get("/api/control/sync/status", headers=headers)).status, 401)

    async def test_native_sync_without_a_host_key_fails_closed(self):
        client = await self.client_for(self.load_config())
        self.assertEqual((await client.get("/api/sync/status")).status, 200)
        self.assertEqual((await client.get("/api/control/sync/status")).status, 401)

    async def test_nonowner_tunnels_never_receive_the_host_key_in_html(self):
        os.environ["AUTH_MODE"] = "both"
        key, _ = provisioning.ensure_api_token(self.env_path)
        for tunnel_auth in ("tenant", "org:example", "anonymous"):
            with self.subTest(tunnel_auth=tunnel_auth):
                cfg = self.load_config()
                cfg.TUNNEL_AUTH = tunnel_auth
                client = await self.client_for(cfg)
                html = await (await client.get("/")).text()
                self.assertNotIn(key, html)
                self.assertNotIn("<script>window.__CB_KEY=", html)
                await client.close()
                self.clients.remove(client)

    async def test_explicit_repair_preserves_credentials_port_language_and_conversation(self):
        self.env_path.write_text(
            "AUTH_MODE=entra\nHOST=localhost\nPORT=13978\nTUNNEL_AUTH=private\n"
            "CHAT_API_TOKEN=preserved-host-secret\nTUNNEL_ID=private-test-tunnel\n"
            "ENTRA_CLIENT_ID=custom-client\nENTRA_ALLOWED_USERS=owner@example.com\n"
            "ONEDRIVE_TRANSPORT=local-folder\n",
            encoding="utf-8",
        )
        cfg = self.load_config()
        client = await self.client_for(cfg)
        store = client.server.app["session_store"]
        session = store.create(title="中文会话保持原文")
        store.replace_messages(session["id"], [{"role": "user", "text": "请保留中文和日本語"}])
        store.update_settings({"uiLanguage": "en", "promptPreviewLength": 300})
        await client.close()
        self.clients.remove(client)
        before = dotenv_values(self.env_path)
        provisioning.configure_local_web_access(cfg, self.env_path)
        after = dotenv_values(self.env_path)
        self.assertEqual(after, {**before, "AUTH_MODE": "tunnel"})
        self.assertEqual(os.environ["AUTH_MODE"], "tunnel")
        repaired = self.load_config()
        client = await self.client_for(repaired)
        self.assertEqual((await (await client.get("/api/webconfig")).json()), {"authMode": "tunnel", "authRequired": False})
        settings = await (await client.get("/api/settings")).json()
        self.assertEqual(settings, {"uiLanguage": "en", "promptPreviewLength": 300})
        restored = await (await client.get(f"/api/sessions/{session['id']}")).json()
        self.assertEqual(restored["title"], "中文会话保持原文")
        self.assertEqual(restored["messages"][0]["text"], "请保留中文和日本語")
        self.assertEqual(repaired.PORT, 13978)
        self.assertEqual(repaired.TUNNEL_AUTH, "private")
        self.assertEqual((await client.get("/api/control/status")).status, 401)

    async def test_repair_rejects_unsafe_bindings_and_nonprivate_tunnels(self):
        self.env_path.write_text("AUTH_MODE=entra\nCHAT_API_TOKEN=unchanged\n", encoding="utf-8")
        original = self.env_path.read_bytes()
        cfg = self.load_config()
        for field, value in (("HOST", "0.0.0.0"), ("HOST", "::"), ("HOST", ""), ("HOST", None), ("HOST", "localhost.example.com"),
                             ("TUNNEL_AUTH", "anonymous"), ("TUNNEL_AUTH", "tenant")):
            with self.subTest(field=field, value=value), patch.object(cfg, field, value):
                with self.assertRaises(ValueError):
                    provisioning.configure_local_web_access(cfg, self.env_path)
                self.assertEqual(self.env_path.read_bytes(), original)
                self.assertEqual(os.environ["AUTH_MODE"], "entra")
        # A persisted unsafe override must also reject repair, even if the
        # running panel still holds a previously safe configuration snapshot.
        self.env_path.write_text("AUTH_MODE=entra\nTUNNEL_AUTH=anonymous\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            provisioning.configure_local_web_access(cfg, self.env_path)
        self.assertEqual(dotenv_values(self.env_path)["AUTH_MODE"], "entra")
        unsafe = SimpleNamespace(AUTH_MODE="tunnel", HOST="0.0.0.0", TUNNEL_AUTH="private", CHAT_API_TOKEN="key")
        self.assertEqual(Authenticator(unsafe).mode, "apikey")
        unsafe.HOST = "localhost"
        unsafe.TUNNEL_AUTH = "anonymous"
        self.assertEqual(Authenticator(unsafe).mode, "apikey")

    async def test_explicit_web_policies_are_not_silently_migrated(self):
        for mode in ("entra", "both", "apikey"):
            with self.subTest(mode=mode):
                self.env_path.write_text(f"AUTH_MODE={mode}\nHOST=localhost\n", encoding="utf-8")
                os.environ["AUTH_MODE"] = mode
                provisioning.ensure_api_token(self.env_path)
                provisioning.ensure_tunnel_id(self.env_path)
                self.assertEqual(self.load_config().AUTH_MODE, mode)
                self.assertEqual(dotenv_values(self.env_path)["AUTH_MODE"], mode)

    def panel_callback(self, name, **scope):
        # Execute the real nested callback with fake IO, not a copied algorithm.
        # This exercises cancellation/failure paths without a GUI, subprocesses,
        # an account lookup, or any access to the production server.
        tree = ast.parse(inspect.getsource(gui.run_control_panel))
        node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(module, "panel-callback", "exec"), scope)
        return scope[name]

    async def test_panel_status_does_not_mistake_failures_for_a_stopped_server(self):
        unauthorized = RuntimeError("wrapped HTTP error")
        unauthorized.__cause__ = gui.urllib.error.HTTPError(
            "http://127.0.0.1/unused", 401, "fixture", None, None,
        )
        cases = (
            (TimeoutError("fixture"), "unresponsive"),
            (gui.urllib.error.URLError(TimeoutError("fixture")), "unresponsive"),
            (unauthorized, "unauthorized"),
            (gui.urllib.error.HTTPError("http://127.0.0.1/unused", 403, "fixture", None, None), "unauthorized"),
            (gui.urllib.error.URLError(ConnectionRefusedError("fixture")), "stopped"),
            (gui.urllib.error.URLError("unknown address"), "unknown"),
            (ValueError("invalid JSON"), "unknown"),
        )
        for error, expected in cases:
            with self.subTest(error=repr(error), expected=expected):
                http = Mock(side_effect=error)
                fetch = self.panel_callback("fetch_status", http=http, urllib=gui.urllib)
                status = fetch()
                self.assertEqual(status["server"], expected)
                self.assertEqual(status["sync"]["serverStopped"], expected == "stopped")
                self.assertTrue(status["statusError"])
                if expected != "stopped":
                    self.assertIn(status["statusError"], gui._format_sync_status(status["sync"]))
                http.assert_called_once_with("GET", "/api/control/status", timeout=3)

    async def test_panel_status_requires_a_valid_control_response(self):
        for response in ({}, [], {"server": "other"}):
            with self.subTest(response=response):
                fetch = self.panel_callback(
                    "fetch_status", http=Mock(return_value=response), urllib=gui.urllib,
                )
                self.assertEqual(fetch()["server"], "unknown")
        fetch = self.panel_callback(
            "fetch_status", urllib=gui.urllib,
            http=Mock(side_effect=[{"server": "running"}, TimeoutError(), TimeoutError()]),
        )
        self.assertEqual(fetch()["server"], "running")

    async def test_panel_buttons_do_not_offer_start_when_status_is_uncertain(self):
        widgets = {name: Mock() for name in (
            "s_status", "t_status", "e_public", "e_local", "access_status", "access_fix",
            "s_start", "s_stop", "s_restart", "t_start", "t_stop", "t_restart",
            "port_btn", "sync_status", "sync_dashboard", "sync_connect", "sync_now", "sync_disconnect",
        )}
        render = self.panel_callback(
            "render", **widgets, state={"busy": False}, cfg=SimpleNamespace(AUTH_MODE="entra"),
            tunnel_enabled=True, set_entry=Mock(), _local_url=lambda: "http://localhost:13978",
            _format_sync_status=lambda sync: sync.get("lastError", ""),
        )
        for server_state in ("running", "stopped", "unresponsive", "unauthorized", "unknown"):
            with self.subTest(server_state=server_state):
                render({"server": server_state})
                self.assertEqual(
                    widgets["s_start"].config.call_args.kwargs["state"],
                    "normal" if server_state == "stopped" else "disabled",
                )
                if server_state not in ("running", "stopped"):
                    self.assertNotIn("Stopped", widgets["s_status"].config.call_args.kwargs["text"])
                    for name in ("s_stop", "s_restart", "port_btn", "access_fix"):
                        self.assertEqual(widgets[name].config.call_args.kwargs["state"], "disabled")

    async def test_inconclusive_tcp_probe_does_not_report_a_free_port(self):
        for error, occupied in (
            (ConnectionRefusedError(), False), (TimeoutError(), True), (OSError(), True),
        ):
            with self.subTest(error=type(error).__name__), patch("socket.create_connection", side_effect=error):
                self.assertIs(gui._port_listening(13978, "::1"), occupied)
        with patch("socket.create_connection") as connect:
            self.assertTrue(gui._port_listening(13978, "::1"))
            connect.assert_called_once_with(("::1", 13978), timeout=3.0)

    async def test_port_probe_confirms_a_real_listener_and_its_release(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            self.assertNotEqual(port, 3978)
            listener.listen(1)
            self.assertTrue(await asyncio.to_thread(gui._port_listening, port))
        self.assertFalse(await asyncio.to_thread(gui._port_listening, port))

    async def test_start_does_not_spawn_for_running_uncertain_or_occupied_state(self):
        for server_state in ("running", "unresponsive", "unauthorized", "unknown", "stopped"):
            with self.subTest(server_state=server_state):
                spawn = Mock()
                action = self.panel_callback(
                    "act_start_server", spawn_server=spawn,
                    fetch_status=Mock(return_value={"server": server_state}),
                    _port_listening=Mock(return_value=True), net={"port": 13978}, _phost="127.0.0.1",
                )
                ok, _message = action()
                self.assertEqual(ok, server_state == "running")
                spawn.assert_not_called()

    async def test_start_spawns_once_and_requires_confirmed_readiness(self):
        for ready in (True, False):
            with self.subTest(ready=ready):
                spawn = Mock()
                action = self.panel_callback(
                    "act_start_server", spawn_server=spawn,
                    fetch_status=Mock(side_effect=[
                        {"server": "stopped"}, {"server": "running" if ready else "unresponsive"},
                    ]),
                    _port_listening=Mock(return_value=False), net={"port": 13978}, _phost="127.0.0.1",
                    time=SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 46]), sleep=Mock()),
                )
                ok, _message = action()
                self.assertIs(ok, ready)
                spawn.assert_called_once()

    async def test_stop_failure_never_means_success_or_force_kills_processes(self):
        for request_error in (TimeoutError(), RuntimeError("unauthorized"), None):
            with self.subTest(request_error=request_error):
                force_kill = Mock()
                action = self.panel_callback(
                    "act_stop_server", http=Mock(side_effect=request_error),
                    _port_listening=Mock(return_value=True), net={"port": 13978}, _phost="127.0.0.1",
                    _force_kill_server=force_kill, tunnel_id="unused",
                    fetch_status=Mock(return_value={"server": "unresponsive"}),
                    time=SimpleNamespace(monotonic=Mock(side_effect=[0, 16]), sleep=Mock()),
                )
                ok, _message = action()
                self.assertFalse(ok)
                force_kill.assert_not_called()

    async def test_stop_succeeds_only_when_the_scoped_port_is_released(self):
        http = Mock()
        probe = Mock(side_effect=[True, True, False])
        action = self.panel_callback(
            "act_stop_server", http=http, _port_listening=probe,
            net={"port": 13978}, _phost="::1",
            time=SimpleNamespace(monotonic=lambda: 0, sleep=Mock()),
        )
        self.assertTrue(action()[0])
        http.assert_called_once_with("POST", "/api/control/shutdown", body={}, timeout=5)
        self.assertTrue(all(call.args == (13978, "::1") for call in probe.call_args_list))

    async def test_restart_does_not_start_after_an_unconfirmed_stop(self):
        for stopped in (True, False):
            with self.subTest(stopped=stopped):
                start = Mock(return_value=(True, "started"))
                action = self.panel_callback(
                    "act_restart_server", act_start_server=start,
                    act_stop_server=Mock(return_value=(stopped, "stop result")),
                )
                self.assertEqual(action()[0], stopped)
                self.assertEqual(start.call_count, int(stopped))

    async def test_panel_repair_stops_only_its_instance_then_verifies_new_policy(self):
        self.env_path.write_text("AUTH_MODE=entra\nCHAT_API_TOKEN=kept\n", encoding="utf-8")
        cfg = self.load_config()
        http = Mock(side_effect=[{"stopping": True}, {"authMode": "tunnel", "authRequired": False}])
        start = Mock(return_value=(True, "Server started."))
        browser = Mock()
        action = self.panel_callback(
            "act_fix_local_signin", cfg=cfg, net={"port": 13978},
            _port_listening=Mock(side_effect=[True, False]), http=http,
            time=SimpleNamespace(monotonic=lambda: 0, sleep=Mock()),
            act_start_server=start, webbrowser=browser,
            _dashboard_url=lambda: "http://localhost:13978/?dashboard=1",
        )
        ok, message = action()
        self.assertTrue(ok, message)
        self.assertEqual(http.call_args_list[0].args, ("POST", "/api/control/shutdown"))
        start.assert_called_once()
        browser.open.assert_called_once()
        self.assertEqual(cfg.AUTH_MODE, "tunnel")
        self.assertEqual(dotenv_values(self.env_path)["CHAT_API_TOKEN"], "kept")

    async def test_failed_stop_does_not_change_policy_or_start_another_server(self):
        self.env_path.write_text("AUTH_MODE=entra\nCHAT_API_TOKEN=kept\n", encoding="utf-8")
        cfg = self.load_config()
        before = self.env_path.read_bytes()
        start = Mock()
        action = self.panel_callback(
            "act_fix_local_signin", cfg=cfg, net={"port": 13978},
            _port_listening=Mock(return_value=True), http=Mock(),
            time=SimpleNamespace(monotonic=Mock(side_effect=[0, 16]), sleep=Mock()),
            act_start_server=start, webbrowser=Mock(), _dashboard_url=Mock(),
        )
        ok, _message = action()
        self.assertFalse(ok)
        self.assertEqual(self.env_path.read_bytes(), before)
        self.assertEqual(cfg.AUTH_MODE, "entra")
        start.assert_not_called()

    async def test_declining_repair_confirmation_does_nothing(self):
        run_action = Mock()
        confirm = self.panel_callback(
            "confirm_fix_local_signin", state={"busy": False}, root=Mock(),
            messagebox=SimpleNamespace(askyesno=Mock(return_value=False)),
            run_action=run_action, act_fix_local_signin=Mock(),
        )
        confirm()
        run_action.assert_not_called()

    async def test_ui_repair_is_explicit_and_has_no_global_kill_fallback(self):
        tree = ast.parse(inspect.getsource(gui.run_control_panel))
        repair = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "act_fix_local_signin")
        called_names = {node.func.id for node in ast.walk(repair) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertNotIn("_force_kill_server", called_names)
        self.assertNotIn("act_stop_server", called_names)
        source = inspect.getsource(gui.run_control_panel)
        self.assertIn('messagebox.askyesno(', source)
        self.assertIn('"/api/control/shutdown"', ast.get_source_segment(source, repair))
        html = (SERVER / "webapp" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Extra application-level Microsoft sign-in is enabled", html)
        self.assertIn("Fix local sign-in", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)