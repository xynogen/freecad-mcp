"""A shared MCP client must not share an in-flight HTTP connection (#160)."""

from concurrent.futures import ThreadPoolExecutor
import threading
from typing import Any

import pytest

from freecad_mcp.freecad_client import FreeCADConnection
from test_rpc_concurrency import running_server


# Include all ordinary RPC operations, not just the create that exposed the race.
CALLS = [
    ("ping", ()),
    ("get_rpc_status", ()),
    ("get_active_screenshot", ("Top", 800, 600, "Plate")),
]


@pytest.mark.parametrize(("method", "args"), CALLS)
def test_operations_do_not_share_a_blocked_create_connection(
    method: str, args: tuple[Any, ...],
) -> None:
    entered, release = threading.Event(), threading.Event()
    received: list[tuple[str, tuple[Any, ...]]] = []

    class Interface:
        def _dispatch(self, name: str, params: tuple[Any, ...]) -> dict[str, Any]:
            received.append((name, params))
            if name == "execute_code" and params[0] == "plate()":
                entered.set()
                assert release.wait(5)
            return {"method": name, "args": list(params)}

    with running_server(Interface()) as (host, port):
        connection = FreeCADConnection(host, port, timeout=2)
        with ThreadPoolExecutor(max_workers=1) as workers:
            first = workers.submit(connection.execute_code, "plate()")
            try:
                assert entered.wait(2)
                result = getattr(connection, method)(*args)
                assert result == {"method": method, "args": list(args)}
                assert not first.done()
            finally:
                release.set()
                connection.disconnect()
            assert first.result(timeout=2) == {"method": "execute_code", "args": ["plate()"]}
    assert received == [("execute_code", ("plate()",)), (method, args)]
