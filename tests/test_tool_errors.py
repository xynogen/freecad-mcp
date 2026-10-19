"""Failed tool calls reach MCP clients with isError set."""

import asyncio
from pathlib import Path
import sys
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from freecad_mcp import server
from freecad_mcp.operations import (
    execute_code_headless_operation,
    execute_code_operation,
    get_view_operation,
)
from freecad_mcp.responses import ErrorResponse
from freecad_mcp.server_state import ServerState


class _Connection:
    def __init__(self, notice: str = "") -> None:
        self.notice = notice

    def ping(self) -> bool:
        return True

    def check_addon_version(self) -> str:
        return self.notice

    def execute_code(self, code: str, _timeout: float | None = None) -> dict[str, Any]:
        if "raise" in code:
            return {"success": False, "error": "ValueError: boom"}
        return {"success": True, "message": "ok"}

    def get_rpc_status(self) -> dict[str, Any]:
        raise ConnectionError("RPC unavailable")

    def get_active_screenshot(self, view_name: str = "Isometric", *_args: Any) -> str | None:
        return None if view_name == "Bottom" else "cG5n"

    def disconnect(self) -> None:
        pass


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch):
    def connect(notice: str = "") -> None:
        monkeypatch.setattr(server, "state", ServerState())
        monkeypatch.setattr(server, "FreeCADConnection", lambda **_kwargs: _Connection(notice))

    return connect


def _error(content: Any) -> bool:
    # mcp 2.x renamed the field to is_error; both serialize as isError.
    return isinstance(content, CallToolResult) and content.model_dump(by_alias=True)["isError"]


def test_failed_tool_call_is_flagged(connected) -> None:
    connected()

    result = server.execute_code(None, "raise ValueError('boom')", include_screenshot=False)

    assert _error(result)
    assert result.content[0].text == "Failed to execute code: ValueError: boom"


def test_successful_tool_call_is_not_flagged(connected) -> None:
    connected()

    result = server.execute_code(None, "x = 1", include_screenshot=False)

    assert not _error(result)
    assert result[0].text == "Code executed successfully: ok"


def test_version_warning_is_kept_on_a_failed_call(connected) -> None:
    connected(notice="addon is old")

    result = server.execute_code(None, "raise ValueError('boom')", include_screenshot=False)

    assert _error(result)
    assert [c.text for c in result.content] == [
        "Warning: addon is old",
        "Failed to execute code: ValueError: boom",
    ]


def test_operations_mark_failures() -> None:
    conn = _Connection()

    assert isinstance(execute_code_operation(conn, True, "raise X"), ErrorResponse)
    assert not isinstance(execute_code_operation(conn, True, "pass"), ErrorResponse)
    assert isinstance(get_view_operation(conn, "Bottom"), ErrorResponse)
    assert not isinstance(get_view_operation(conn, "Top"), ErrorResponse)


def test_failed_headless_run_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    import freecad_mcp.headless as headless

    monkeypatch.setattr(
        headless, "run_headless", lambda *_args: {"success": False, "error": "exit 1"}
    )
    assert isinstance(execute_code_headless_operation(None, "", 10), ErrorResponse)

    monkeypatch.setattr(headless, "run_headless", lambda *_args: {"success": True})
    assert not isinstance(execute_code_headless_operation(None, "", 10), ErrorResponse)


def test_stdio_client_receives_error_flags_and_original_content() -> None:
    """Exercise SDK serialization, including MCP 1.x and 2.x normalization."""
    async def exercise() -> None:
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__))])
        with anyio.fail_after(30):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    failed = await session.call_tool(
                        "execute_code", {"code": "raise ValueError('boom')", "include_screenshot": False}
                    )
                    assert _error(failed)
                    assert [c.text for c in failed.content] == [
                        "Warning: addon is old",
                        "Failed to execute code: ValueError: boom",
                    ]
                    again = await session.call_tool(
                        "execute_code", {"code": "raise ValueError('boom')", "include_screenshot": False}
                    )
                    assert _error(again)
                    assert len(again.content) == 1  # The version notice is sent once.
                    no_view = await session.call_tool("get_view", {"view_name": "Bottom"})
                    assert _error(no_view)
                    status = await session.call_tool("get_rpc_status", {})
                    assert _error(status)
                    assert "RPC unavailable" in status.content[0].text
                    success = await session.call_tool("execute_code", {"code": "x = 1"})
                    assert not _error(success)
                    assert success.content[0].text == "Code executed successfully: ok"
                    assert success.content[1].type == "image"
                    assert success.content[1].data == "cG5n"

    asyncio.run(exercise())


if __name__ == "__main__":
    # Only FreeCAD is stubbed; the real MCP server and client use stdio.
    server.state.freecad_connection = _Connection()
    server.state.version_notice = "addon is old"
    server.mcp.run()
