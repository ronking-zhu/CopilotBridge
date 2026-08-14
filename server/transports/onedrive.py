"""Microsoft Graph transport backed by the caller's OneDrive App Folder."""

from __future__ import annotations

import hashlib
from urllib.parse import quote

import aiohttp

from .base import ObjectConflictError, RemoteObject, TransportHealth


class GraphRequestError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"Microsoft Graph returned {status}: {detail}")
        self.status = status
        self.detail = detail


class OneDriveGraphTransport:
    def __init__(
        self,
        token_provider,
        *,
        graph_root: str = "https://graph.microsoft.com/v1.0",
        session: aiohttp.ClientSession | None = None,
    ):
        self.token_provider = token_provider
        self.graph_root = graph_root.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._app_root: dict | None = None
        self._folder_cache: dict[tuple[str, str], dict] = {}

    @staticmethod
    def _parts(key: str) -> list[str]:
        normalized = str(key or "").replace("\\", "/").strip("/")
        parts = normalized.split("/") if normalized else []
        if any(part in ("", ".", "..") or "\x00" in part for part in parts):
            raise ValueError(f"unsafe remote object key: {key!r}")
        return parts

    async def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        json_body: dict | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
    ) -> tuple[int, bytes, aiohttp.typedefs.LooseHeaders]:
        token = await self.token_provider.get_access_token()
        request_headers = {"Authorization": f"Bearer {token}"}
        request_headers.update(headers or {})
        url = path if path.startswith("http://") or path.startswith("https://") else self.graph_root + path
        client = await self._client()
        async with client.request(
            method, url, json=json_body, data=content, headers=request_headers,
            allow_redirects=True,
        ) as response:
            body = await response.read()
            if response.status not in expected:
                detail = body.decode("utf-8", "replace")[:1000]
                raise GraphRequestError(response.status, detail)
            return response.status, body, response.headers

    async def _json(self, method: str, path: str, **kwargs) -> tuple[int, dict]:
        import json

        status, body, _headers = await self._request(method, path, **kwargs)
        return status, json.loads(body.decode("utf-8")) if body else {}

    async def connect(self) -> dict:
        if self._app_root is None:
            _status, self._app_root = await self._json(
                "GET", "/me/drive/special/approot",
                expected=(200,),
            )
            if not self._app_root.get("id"):
                raise GraphRequestError(200, "App Folder response has no drive item id")
        return self._app_root

    async def _children(self, parent_id: str) -> list[dict]:
        result: list[dict] = []
        path = (
            f"/me/drive/items/{quote(parent_id, safe='')}/children"
            "?$select=id,name,folder,file,size,eTag"
        )
        while path:
            _status, document = await self._json("GET", path, expected=(200,))
            result.extend(document.get("value") or [])
            path = document.get("@odata.nextLink") or ""
        return result

    async def _child(self, parent_id: str, name: str) -> dict | None:
        cached = self._folder_cache.get((parent_id, name))
        if cached is not None:
            return cached
        for item in await self._children(parent_id):
            if item.get("name") == name:
                if "folder" in item:
                    self._folder_cache[(parent_id, name)] = item
                return item
        return None

    async def _create_folder(self, parent_id: str, name: str) -> dict:
        try:
            _status, item = await self._json(
                "POST",
                f"/me/drive/items/{quote(parent_id, safe='')}/children",
                expected=(200, 201),
                json_body={
                    "name": name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
        except GraphRequestError as exc:
            if exc.status not in (409, 412):
                raise
            item = await self._child(parent_id, name)
            if item is None:
                raise
        if "folder" not in item:
            raise ObjectConflictError(f"remote path component is not a folder: {name}")
        self._folder_cache[(parent_id, name)] = item
        return item

    async def _resolve_folder(self, parts: list[str], *, create: bool) -> dict | None:
        parent = await self.connect()
        for name in parts:
            item = await self._child(parent["id"], name)
            if item is None:
                if not create:
                    return None
                item = await self._create_folder(parent["id"], name)
            if "folder" not in item:
                raise ObjectConflictError(f"remote path component is not a folder: {name}")
            parent = item
        return parent

    async def _file(self, key: str) -> dict | None:
        parts = self._parts(key)
        if not parts:
            return None
        parent = await self._resolve_folder(parts[:-1], create=False)
        return await self._child(parent["id"], parts[-1]) if parent else None

    async def list(self, prefix: str) -> list[RemoteObject]:
        parts = self._parts(prefix)
        folder = await self._resolve_folder(parts, create=False)
        if folder is None:
            return []
        base = "/".join(parts)
        result: list[RemoteObject] = []

        async def walk(parent: dict, relative: str) -> None:
            for item in await self._children(parent["id"]):
                child_relative = f"{relative}/{item['name']}" if relative else item["name"]
                if "folder" in item:
                    await walk(item, child_relative)
                elif "file" in item:
                    key = f"{base}/{child_relative}" if base else child_relative
                    result.append(RemoteObject(
                        key=key,
                        size=int(item.get("size") or 0),
                        etag=str(item.get("eTag") or ""),
                    ))

        await walk(folder, "")
        result.sort(key=lambda item: item.key)
        return result

    async def put_if_absent(self, key: str, content: bytes, sha256: str) -> None:
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256:
            raise ValueError("content hash does not match the requested SHA-256")
        parts = self._parts(key)
        if not parts:
            raise ValueError("remote object key must name a file")
        parent = await self._resolve_folder(parts[:-1], create=True)
        existing = await self._child(parent["id"], parts[-1])
        if existing is not None:
            if "file" not in existing or hashlib.sha256(await self.get(key)).hexdigest() != sha256:
                raise ObjectConflictError(f"remote object has different content: {key}")
            return
        try:
            await self._request(
                "PUT",
                f"/me/drive/items/{quote(parent['id'], safe='')}:/{quote(parts[-1], safe='')}:/content",
                expected=(200, 201),
                content=content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "If-None-Match": "*",
                },
            )
        except GraphRequestError as exc:
            if exc.status not in (409, 412):
                raise
            existing = await self._file(key)
            if existing is None or "file" not in existing:
                raise
            if hashlib.sha256(await self.get(key)).hexdigest() != sha256:
                raise ObjectConflictError(f"remote object has different content: {key}") from exc

    async def get(self, key: str) -> bytes:
        item = await self._file(key)
        if item is None or "file" not in item:
            raise FileNotFoundError(key)
        _status, body, _headers = await self._request(
            "GET", f"/me/drive/items/{quote(item['id'], safe='')}/content",
            expected=(200,),
        )
        return body

    async def exists(self, key: str) -> bool:
        item = await self._file(key)
        return bool(item and "file" in item)

    async def diagnose(self) -> TransportHealth:
        try:
            root = await self.connect()
            return TransportHealth(ok=True, detail=str(root.get("name") or "OneDrive App Folder"))
        except Exception as exc:  # noqa: BLE001
            return TransportHealth(ok=False, detail=str(exc))

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
