"""Tests OneDrive MSAL lifecycle and Windows-protected token persistence.

Run: python tests/onedrive_auth_test.py (from the server/ directory)
"""

import asyncio
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from onedrive_auth import DpapiFileStore, OneDriveAuth, OneDriveNotConnected


class MemoryStore:
    def __init__(self):
        self.value = b""

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def clear(self):
        self.value = b""


class FakeApp:
    def __init__(self, client_id, authority, token_cache):
        self.client_id = client_id
        self.authority = authority
        self.token_cache = token_cache
        self.accounts = []
        self.interactive_calls = 0

    def get_accounts(self):
        return list(self.accounts)

    def acquire_token_silent(self, scopes, account):
        if account not in self.accounts:
            return None
        return {"access_token": "silent-token", "account": account}

    def acquire_token_interactive(self, scopes, **kwargs):
        self.interactive_calls += 1
        account = {"username": "person@example.com", "home_account_id": "account-1"}
        self.accounts[:] = [account]
        return {"access_token": "interactive-token", "account": account}

    def remove_account(self, account):
        self.accounts.remove(account)


async def test_auth_lifecycle():
    holder = {}

    def factory(client_id, authority, token_cache):
        holder["app"] = FakeApp(client_id, authority, token_cache)
        return holder["app"]

    auth = OneDriveAuth(
        "client-id", tenant_id="common", secure_store=MemoryStore(),
        app_factory=factory,
    )
    try:
        await auth.get_access_token()
        raise AssertionError("expected disconnected error")
    except OneDriveNotConnected:
        pass

    connected = await auth.connect()
    assert connected["connected"] is True
    assert connected["username"] == "person@example.com"
    assert await auth.get_access_token() == "silent-token"
    assert holder["app"].interactive_calls == 1

    disconnected = await auth.disconnect()
    assert disconnected["connected"] is False
    assert holder["app"].accounts == []


def test_dpapi_store():
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "onedrive-token.cache")
        store = DpapiFileStore(path)
        secret = b'{"refresh_token":"must-not-be-plaintext"}'
        store.save(secret)
        assert store.load() == secret
        assert b"must-not-be-plaintext" not in open(path, "rb").read()
        store.clear()
        assert store.load() == b""


async def main():
    await test_auth_lifecycle()
    test_dpapi_store()
    print("ALL ONEDRIVE AUTH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())