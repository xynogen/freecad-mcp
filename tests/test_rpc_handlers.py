from collections.abc import Iterator
import importlib.util
from pathlib import Path
import sys
import types

import pytest

from test_gui_dispatch import load_gui_dispatch, ThreadedWaker


RPC_PATH = (
    Path(__file__).resolve().parents[1]
    / "addon" / "FreeCADMCP" / "rpc_server" / "rpc_server.py"
)


@pytest.fixture
def rpc_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """Exercise real RPC handlers and dispatch with only FreeCAD/Qt stubbed."""
    with load_gui_dispatch() as dispatch:
        freecad = dispatch.FreeCAD
        freecad.Console.PrintMessage = lambda _message: None
        freecad.Console.PrintWarning = lambda _message: None
        stubs = {
            "gui_dispatch": dispatch,
            "commands": types.SimpleNamespace(
                register_commands=lambda: None, schedule_toggle_sync=lambda: None
            ),
            "cad": types.SimpleNamespace(HELPERS={"pad": "pad-helper"}),
            "settings": types.SimpleNamespace(load_settings=lambda: {}, save_settings=lambda _: None),
            "view_manager": types.SimpleNamespace(
                get_displayed_3d_view=lambda: None,
                save_active_screenshot=lambda *_args: True,
            ),
        }
        with monkeypatch.context() as patch:
            for name, stub in stubs.items():
                patch.setitem(sys.modules, f"rpc_server.{name}", stub)
            spec = importlib.util.spec_from_file_location("_rpc_handler_test", RPC_PATH)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            waker = ThreadedWaker(dispatch)
            dispatch._waker = waker
            module.test_dispatch = dispatch
            try:
                yield module
            finally:
                waker.join()


def test_script_names_cannot_replace_rpc_internals(rpc_module: types.ModuleType) -> None:
    rpc = rpc_module.FreeCADRPC()
    original_dispatch = rpc_module.dispatch_to_gui
    result = rpc.execute_code(
        "dispatch_to_gui = None\n"
        "FreeCAD = None\nFreeCADGui = None\nshared_value = 41"
    )
    assert result["success"] is True
    assert rpc_module.dispatch_to_gui is original_dispatch
    result = rpc.execute_code("print(shared_value + 1)")
    assert result["success"] is True
    assert result["message"].endswith("42\n")


def test_cad_helpers_are_preloaded(rpc_module: types.ModuleType) -> None:
    result = rpc_module.FreeCADRPC().execute_code("print(pad)")
    assert result["message"].endswith("pad-helper\n")


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("import sys; sys.exit(3)", "SystemExit: 3"),
        ('raise SystemExit("stop")', "SystemExit: stop"),
        ("raise KeyboardInterrupt()", "KeyboardInterrupt:"),
    ],
)
def test_script_exit_fails_the_call_and_preserves_rpc_dispatch(
    rpc_module: types.ModuleType, code: str, error: str,
) -> None:
    # Run the Qt callback synchronously so an escaping BaseException fails
    # this test instead of merely terminating a test worker thread.
    rpc_module.test_dispatch._waker = types.SimpleNamespace(
        wake=lambda: rpc_module.test_dispatch.process_gui_tasks(reschedule=False)
    )
    rpc = rpc_module.FreeCADRPC()
    stdout = sys.stdout

    result = rpc.execute_code(code)

    assert result["success"] is False
    assert error in result["error"]
    assert sys.stdout is stdout
    assert rpc.get_rpc_status()["gui_dispatch"]["state"] == "healthy"
    followup = rpc.execute_code('print("still running")')
    assert followup["success"] is True
    assert followup["message"].endswith("still running\n")


def test_execute_code_uses_default_timeout_when_none_given(
    rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc = rpc_module.FreeCADRPC()
    seen: list[float] = []
    real = rpc_module.dispatch_to_gui

    def spy(task, timeout=60, operation_name=None):
        seen.append(timeout)
        return real(task, timeout=timeout, operation_name=operation_name)

    monkeypatch.setattr(rpc_module, "dispatch_to_gui", spy)
    assert rpc.execute_code("x = 1")["success"] is True
    assert seen == [rpc_module.FreeCADRPC.EXECUTE_CODE_TIMEOUT]


def test_execute_code_honours_caller_timeout_and_caps_it(
    rpc_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc = rpc_module.FreeCADRPC()
    seen: list[float] = []
    real = rpc_module.dispatch_to_gui

    def spy(task, timeout=60, operation_name=None):
        seen.append(timeout)
        return real(task, timeout=timeout, operation_name=operation_name)

    monkeypatch.setattr(rpc_module, "dispatch_to_gui", spy)
    assert rpc.execute_code("x = 1", 300)["success"] is True
    assert rpc.execute_code("x = 1", "240")["success"] is True
    assert rpc.execute_code("x = 1", 10**9)["success"] is True
    assert seen == [300.0, 240.0, rpc_module.FreeCADRPC.MAX_EXECUTE_CODE_TIMEOUT]


@pytest.mark.parametrize("bad", ["soon", None.__class__, 0, -5])
def test_execute_code_rejects_invalid_timeout(
    rpc_module: types.ModuleType, bad: object
) -> None:
    rpc = rpc_module.FreeCADRPC()
    if bad is None.__class__:
        bad = object()
    result = rpc.execute_code("x = 1", bad)
    assert result["success"] is False
    assert "invalid timeout" in result["error"]


