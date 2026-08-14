"""Contract tests for OneDrive Graph App Folder object transport.

Run: python tests/onedrive_transport_test.py (from the server/ directory)
"""

import asyncio
import hashlib
import os
import sys

from aiohttp import web
from aiohttp.test_utils import TestServer

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from transports.base import ObjectConflictError
from transports.onedrive import OneDriveGraphTransport


class TokenProvider:
    async def get_access_token(self) -> str:
        return "test-token"


class FakeDrive:
    def __init__(self):
        self.next_id = 1
        self.items = {
            "root": {"id": "root", "name": "Copilot Bridge", "folder": {}},
        }
        self.children = {"root": []}
        self.content = {}

    def child(self, parent_id, name):
        for item_id in self.children.get(parent_id, []):
            if self.items[item_id]["name"] == name:
                return self.items[item_id]
        return None

    async def handle(self, request):
        assert request.headers.get("Authorization") == "Bearer test-token"
        path = request.path
        if request.method == "GET" and path.endswith("/me/drive/special/approot"):
            return web.json_response(self.items["root"])

        marker = "/me/drive/items/"
        tail = path.split(marker, 1)[1]
        if tail.endswith("/children"):
            parent_id = tail[:-len("/children")]
            if request.method == "GET":
                values = [self.items[item_id] for item_id in self.children.get(parent_id, [])]
                return web.json_response({"value": values})
            body = await request.json()
            existing = self.child(parent_id, body["name"])
            if existing:
                return web.json_response(existing, status=409)
            item_id = f"item-{self.next_id}"
            self.next_id += 1
            item = {"id": item_id, "name": body["name"], "folder": {}}
            self.items[item_id] = item
            self.children.setdefault(parent_id, []).append(item_id)
            self.children[item_id] = []
            return web.json_response(item, status=201)

        if ":/" in tail and tail.endswith(":/content"):
            parent_id, name = tail[:-len(":/content")].split(":/", 1)
            existing = self.child(parent_id, name)
            if existing and request.headers.get("If-None-Match") == "*":
                return web.json_response({"error": "nameAlreadyExists"}, status=412)
            item_id = existing["id"] if existing else f"item-{self.next_id}"
            if not existing:
                self.next_id += 1
                item = {"id": item_id, "name": name, "file": {}, "size": 0, "eTag": item_id}
                self.items[item_id] = item
                self.children.setdefault(parent_id, []).append(item_id)
            blob = await request.read()
            self.content[item_id] = blob
            self.items[item_id]["size"] = len(blob)
            return web.json_response(self.items[item_id], status=201)

        if tail.endswith("/content") and request.method == "GET":
            item_id = tail[:-len("/content")]
            if item_id not in self.content:
                return web.Response(status=404)
            return web.Response(body=self.content[item_id])
        return web.Response(status=404)


async def main() -> None:
    drive = FakeDrive()
    app = web.Application()
    app.router.add_route("*", "/v1.0/{tail:.*}", drive.handle)
    server = TestServer(app)
    await server.start_server()
    transport = OneDriveGraphTransport(
        TokenProvider(), graph_root=str(server.make_url("/v1.0")).rstrip("/")
    )
    try:
        health = await transport.diagnose()
        assert health.ok and "Copilot Bridge" in health.detail

        key = "spaces/default/v1/devices/laptop-a/events/1-1-abc.cbe"
        content = b"immutable package"
        digest = hashlib.sha256(content).hexdigest()
        await transport.put_if_absent(key, content, digest)
        assert await transport.exists(key)
        assert await transport.get(key) == content

        # Re-uploading identical immutable content succeeds; different content
        # under the same key is rejected rather than overwritten.
        await transport.put_if_absent(key, content, digest)
        changed = b"different"
        try:
            await transport.put_if_absent(key, changed, hashlib.sha256(changed).hexdigest())
            raise AssertionError("expected object conflict")
        except ObjectConflictError:
            pass

        objects = await transport.list("spaces/default/v1/devices")
        assert [(item.key, item.size) for item in objects] == [(key, len(content))]
    finally:
        await transport.close()
        await server.close()
    print("ALL ONEDRIVE TRANSPORT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())