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
    assert isinstance(execute_code_headless_operation(None, "pass", 10), ErrorResponse)

    monkeypatch.setattr(headless, "run_headless", lambda *_args: {"success": True})
    assert not isinstance(execute_code_headless_operation(None, "pass", 10), ErrorResponse)


@pytest.mark.parametrize("timeout", [None, 12, 0])
def test_headless_file_does_not_connect_to_gui(monkeypatch, timeout) -> None:
    from unittest.mock import Mock
    from freecad_mcp import headless

    connection = Mock(side_effect=AssertionError("headless must not connect to GUI"))
    run = Mock(return_value={"success": True})
    monkeypatch.setattr(server, "get_freecad_connection", connection)
    monkeypatch.setattr(headless, "run_headless", run)
    monkeypatch.setattr(server, "state", ServerState())
    assert not _error(server.execute_file(None, "/model.py", headless=True, timeout=timeout))
    connection.assert_not_called()
    assert run.call_args.args[1:] == (600 if timeout is None else timeout, None)



def test_stdio_client_receives_error_flags_and_original_content(tmp_path: Path) -> None:
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
                    tools = await session.list_tools()
                    assert "execute_code_headless" not in {t.name for t in tools.tools}
                    schema = next(t.inputSchema for t in tools.tools if t.name == "execute_code")
                    assert "path" not in schema["properties"]
                    assert "headless" not in schema["properties"]
                    assert "code" in schema["required"]
                    invalid = await session.call_tool("execute_code", {})
                    assert _error(invalid)
                    schema = next(t.inputSchema for t in tools.tools if t.name == "execute_file")
                    assert "path" in schema["required"]
                    assert "code" not in schema["properties"]
                    assert schema["properties"]["headless"]["default"] is False
                    invalid = await session.call_tool("execute_file", {})
                    assert _error(invalid)
                    script = tmp_path / "model's file.FCMacro"
                    script.write_text(
                        "if __name__ == '__main__':\n    print('loaded:', __file__)\n",
                        encoding="utf-8",
                    )
                    loaded = await session.call_tool(
                        "execute_file", {"path": str(script), "headless": True, "timeout": 10}
                    )
                    assert not _error(loaded)
                    assert f"loaded: {script}" in loaded.content[0].text
                    for source, error in [
                        ("raise AssertionError('Base/Lid: collision 154.274 mm3')",
                         "AssertionError: Base/Lid: collision 154.274 mm3"),
                        ("def broken(:", "SyntaxError"),
                    ]:
                        script.write_text(source, encoding="utf-8")
                        failed = await session.call_tool(
                            "execute_file", {"path": str(script), "headless": True, "timeout": 10}
                        )
                        assert _error(failed)
                        assert error in failed.content[0].text
                        assert str(script) in failed.content[0].text
                        assert script.read_text(encoding="utf-8") == source
                    script.unlink()
                    missing = await session.call_tool(
                        "execute_file", {"path": str(script), "headless": True, "timeout": 10}
                    )
                    assert _error(missing)
                    assert "accessible to FreeCAD" in missing.content[0].text

    asyncio.run(exercise())


@pytest.mark.parametrize("headless", [False, True])
def test_path_runs_in_freecad_namespace(tmp_path, monkeypatch, headless) -> None:
    script = tmp_path / "model's file.FCMacro"
    script.write_bytes(b"# coding: latin-1\nif __name__ == '__main__':\n    result = base + 'caf\xe9'\n")
    namespace = {"base": "FreeCAD: "}

    def execute(code, *_args):
        exec(code, namespace)
        return {"success": True, "message": "ok"}

    conn = _Connection()
    monkeypatch.setattr(conn, "execute_code", execute)
    monkeypatch.setattr(server, "get_freecad_connection", lambda: conn)
    monkeypatch.setattr("freecad_mcp.headless.run_headless", execute)

    def run():
        return server.execute_file(
            None, str(script), headless=headless, include_screenshot=False
        )

    assert not _error(run())
    assert namespace["result"] == "FreeCAD: café"
    assert namespace["__file__"] == str(script)
    script.write_text("result = 'updated'", encoding="utf-8")
    assert not _error(run())
    assert namespace["result"] == "updated"
    if not headless:
        assert not _error(server.execute_code(None, "result += ': live'", include_screenshot=False))
        assert namespace["result"] == "updated: live"


@pytest.mark.parametrize("arguments", [{}, {"code": "pass", "path": "/unused"}, {"code": " "}, {"path": " "}])
def test_invalid_source_is_not_executed(arguments) -> None:
    from freecad_mcp.operations.core import _load_code

    with pytest.raises(ValueError):
        _load_code(**{"code": None, "path": None, **arguments})


def test_path_is_read_only_in_freecad_process(tmp_path) -> None:
    from freecad_mcp.operations.core import _load_code

    script = tmp_path / "late.py"
    code = _load_code(None, str(script))  # Does not read the server filesystem.
    script.write_text("value = 42", encoding="utf-8")
    namespace = {}
    exec(code, namespace)
    assert namespace["value"] == 42


@pytest.mark.parametrize("case", ["relative", "missing", "directory"])
def test_inaccessible_path_is_rejected_in_freecad(tmp_path, case) -> None:
    from freecad_mcp.operations.core import _load_code

    path = {
        "relative": "model.py",
        "missing": str(tmp_path / "missing.py"),
        "directory": str(tmp_path),
    }[case]
    with pytest.raises(ValueError, match="FreeCAD"):
        exec(_load_code(None, path), {})


def test_script_error_retains_filename(tmp_path) -> None:
    from freecad_mcp.operations.core import _load_code

    script = tmp_path / "broken.py"
    script.write_text("raise ValueError('boom')", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        exec(_load_code(None, str(script)), {})
    assert error.traceback[-1].path == script


if __name__ == "__main__":
    # Only FreeCAD is stubbed; the real MCP server and client use stdio.
    server.state.freecad_connection = _Connection()
    server.state.version_notice = "addon is old"
    server.state.freecadcmd = [sys.executable]
    server.mcp.run()
