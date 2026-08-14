"""Regression checks for filesystem transport executor ownership.

Run: python tests/filesystem_transport_test.py (from the server/ directory)
"""

import asyncio
import hashlib
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from transports.filesystem import FileSystemTransport


async def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        transport = FileSystemTransport(root)
        try:
            await asyncio.get_running_loop().shutdown_default_executor()

            health = await transport.diagnose()
            assert health.ok

            key = "spaces/default/v1/devices/laptop-b/events/1-1-test.cbe"
            content = b"immutable package"
            digest = hashlib.sha256(content).hexdigest()
            await transport.put_if_absent(key, content, digest)
            assert await transport.exists(key)
            assert await transport.get(key) == content
            assert [item.key for item in await transport.list("spaces/default")] == [key]
        finally:
            transport.close()

    with tempfile.TemporaryDirectory() as root:
        transport = FileSystemTransport(root)
        failed_executor = transport._executor
        failed_executor.shutdown(wait=True, cancel_futures=True)
        health = await transport.diagnose()
        assert health.ok
        assert transport._executor is not failed_executor
        transport.close()

    with tempfile.TemporaryDirectory() as root:
        transport = FileSystemTransport(root)
        executor = transport._executor
        attempts = 0

        def fail_during_operation():
            nonlocal attempts
            attempts += 1
            raise RuntimeError("storage shutdown during operation")

        try:
            await transport._run(fail_during_operation)
        except RuntimeError as exc:
            assert str(exc) == "storage shutdown during operation"
        else:
            raise AssertionError("operation error was not propagated")
        assert attempts == 1
        assert transport._executor is executor
        transport.close()

    with tempfile.TemporaryDirectory() as root:
        transport = FileSystemTransport(root)
        executor = transport._executor
        transport.close()
        try:
            await transport.list("")
        except RuntimeError as exc:
            assert str(exc) == "filesystem transport is closed"
        else:
            raise AssertionError("explicitly closed transport accepted work")
        assert transport._executor is executor

    print("ALL FILESYSTEM TRANSPORT TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())