import FreeCAD
import FreeCADGui

import contextlib
import base64
import io
import math
import os
import tempfile
import threading
from typing import Any

from PySide import QtCore

from rpc_server.cad import HELPERS as CAD_HELPERS
from rpc_server.commands import register_commands, schedule_toggle_sync
from rpc_server.gui_dispatch import (
    cleanup_waker,
    dispatch_to_gui,
    get_dispatch_status,
    init_waker,
    process_gui_tasks,
    request_shutdown,
)
from rpc_server.ip_filter import FilteredXMLRPCServer, validate_allowed_ips
from rpc_server.settings import load_settings, save_settings
from rpc_server.version import PROTOCOL_VERSION, __version__ as ADDON_VERSION
from rpc_server.view_manager import get_displayed_3d_view, save_active_screenshot

rpc_server_thread = None
rpc_server_instance = None
_stop_thread = None  # drains shutdown off the GUI thread; see stop_rpc_server

# Persistent namespace for execute_code, preloaded with the sketch-first CAD
# helpers. A dedicated dict (instead of this module's globals()) keeps user
# code from shadowing server internals like dispatch_to_gui while preserving
# the documented pattern of sharing variables between successive calls.
_EXEC_NAMESPACE: dict[str, Any] = {
    "FreeCAD": FreeCAD,
    "App": FreeCAD,
    "FreeCADGui": FreeCADGui,
    "Gui": FreeCADGui,
    **CAD_HELPERS,
}


def _ok(res) -> bool:
    """True when a GUI-thread handler returned success."""
    return res is True


def _err(res) -> dict:
    """Convert any non-True result (error string or timeout dict) to a failure dict."""
    if isinstance(res, dict):
        return res
    return {"success": False, "error": str(res)}


class FreeCADRPC:
    """RPC server for FreeCAD"""
    TIMEOUT = 60               # generous wait for GUI thread to become free
    EXECUTE_CODE_TIMEOUT = 90  # GUI-thread execution; execute_code_headless runs heavy OCCT ops
    # Ceiling for a caller-supplied execute_code timeout. A GUI task cannot be
    # cancelled once started, so an unbounded wait would hide a wedged GUI
    # thread from the caller indefinitely.
    MAX_EXECUTE_CODE_TIMEOUT = 1800

    def ping(self):
        return True

    def get_rpc_status(self) -> dict[str, Any]:
        """Report server and GUI-dispatch health without using the GUI thread."""
        return {
            "success": True,
            "rpc_server": "running",
            "gui_dispatch": get_dispatch_status(),
            "addon_version": ADDON_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "execute_code_timeout": self.EXECUTE_CODE_TIMEOUT,
            "max_execute_code_timeout": self.MAX_EXECUTE_CODE_TIMEOUT,
        }

    def execute_code(self, code: str, timeout: Any = None) -> dict[str, Any]:
        """Execute Python code on the GUI thread and wait for the result.

        Runs on the GUI thread so that FreeCAD document operations
        (addObject, recompute, save) are safe and correctly ordered.
        Use execute_code_headless for heavy OCCT work that may crash or
        block the GUI thread too long.

        ``timeout`` overrides EXECUTE_CODE_TIMEOUT for this call only, capped at
        MAX_EXECUTE_CODE_TIMEOUT. Raise it for genuinely slow GUI-thread work
        that cannot move off the GUI thread, such as importing or exporting a
        large STEP assembly. Without it such a call reports a timeout while the
        task keeps running, and its result is discarded even though the work
        completes.
        """
        timeout_s = self.EXECUTE_CODE_TIMEOUT
        if timeout is not None:
            try:
                timeout_s = float(timeout)
            except (TypeError, ValueError, OverflowError):
                return {"success": False, "error": f"invalid timeout: {timeout!r}"}
            if isinstance(timeout, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
                return {"success": False, "error": f"invalid timeout: {timeout!r}"}
            timeout_s = min(timeout_s, self.MAX_EXECUTE_CODE_TIMEOUT)

        output_buffer = io.StringIO()

        def task():
            with contextlib.redirect_stdout(output_buffer):
                exec(code, _EXEC_NAMESPACE)
            return True

        res = dispatch_to_gui(
            task,
            timeout=timeout_s,
            operation_name="execute_code",
        )
        if _ok(res):
            FreeCAD.Console.PrintMessage("Python code executed successfully.\n")
            return {
                "success": True,
                "message": "Python code executed successfully.\nOutput: " + output_buffer.getvalue(),
            }
        # Log the offending code (truncated) to make errors traceable
        code_preview = code if len(code) <= 800 else code[:800] + "\n...(truncated)"
        FreeCAD.Console.PrintError(
            f"Error executing Python code: {res}\n"
            f"--- code ---\n{code_preview}\n--- end ---\n"
        )
        return _err(res)

    def get_active_screenshot(
        self,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ) -> str | None:
        """Get a screenshot of the active view as a base64-encoded PNG string.

        Returns None if the active view does not support screenshots
        (e.g., TechDraw or Spreadsheet workbench).
        """
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)

        def task() -> bool | str:
            active_view = get_displayed_3d_view()
            if active_view is None:
                try:
                    shown = FreeCADGui.activeView()
                except Exception:
                    shown = None
                view_type = type(shown).__name__ if shown is not None else "None"
                FreeCAD.Console.PrintWarning(
                    f"MCP RPC: view type '{view_type}' does not support screenshots\n"
                )
                return False
            return save_active_screenshot(tmp_path, view_name, width, height, focus_object)

        try:
            res = dispatch_to_gui(task, operation_name="get_active_screenshot")
            if _ok(res):
                with open(tmp_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            if res is False:
                return None
            FreeCAD.Console.PrintWarning(f"MCP RPC: screenshot failed: {res}\n")
            return None
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _save_active_screenshot(
        self,
        save_path: str,
        view_name: str = "Isometric",
        width: int | None = None,
        height: int | None = None,
        focus_object: str | None = None,
    ):
        return save_active_screenshot(save_path, view_name, width, height, focus_object)


def start_rpc_server(port: int = 9875) -> str:
    global rpc_server_thread, rpc_server_instance

    if rpc_server_instance:
        host, bound_port = rpc_server_instance.server_address
        return f"RPC Server already running at {host}:{bound_port} (PID {os.getpid()})."

    # A previous stop may still be draining an in-flight request off-thread;
    # binding before its server_close() would hit the old socket.
    if _stop_thread is not None and _stop_thread.is_alive():
        _stop_thread.join(timeout=5.0)
        if _stop_thread.is_alive():
            return ("RPC Server is still stopping (a request is draining); "
                    "try again in a few seconds.")

    settings = load_settings()
    remote_enabled = settings.get("remote_enabled", False)
    allowed_ips = settings.get("allowed_ips", "127.0.0.1")
    auth_token = settings.get("auth_token", "")

    if remote_enabled:
        host = "0.0.0.0"
        if not auth_token:
            FreeCAD.Console.PrintWarning(
                "MCP RPC: remote connections are enabled WITHOUT an auth token. "
                "Anyone on an allowed IP can execute code in FreeCAD. "
                "Set a token via 'Set Auth Token' in the FreeCAD MCP menu.\n"
            )
    else:
        host = "127.0.0.1"

    server = FilteredXMLRPCServer(
        (host, port),
        allowed_ips_str=allowed_ips,
        auth_token=auth_token,
        allow_none=True,
        logRequests=False,
    )
    try:
        server.register_instance(FreeCADRPC())
        init_waker()
        QtCore.QTimer.singleShot(500, process_gui_tasks)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    except Exception:
        # A failed start must not retain a listening socket or appear running.
        # This allows the toolbar command to be retried without restarting FreeCAD.
        server.server_close()
        request_shutdown()
        cleanup_waker()
        raise

    rpc_server_instance = server
    rpc_server_thread = thread
    bound_host, bound_port = server.server_address
    msg = f"RPC Server started at {bound_host}:{bound_port} (PID {os.getpid()})."
    if remote_enabled:
        msg += f" Allowed IPs: {allowed_ips}"
    if auth_token:
        msg += " Auth token required."
    return msg


def stop_rpc_server():
    global rpc_server_instance, rpc_server_thread, _stop_thread

    if not rpc_server_instance:
        return "RPC Server was not running."

    server = rpc_server_instance
    thread = rpc_server_thread
    rpc_server_instance = None
    rpc_server_thread = None

    request_shutdown()
    cleanup_waker()

    def _shutdown_and_close():
        # shutdown() only stops the accept loop; in-flight requests run in
        # their own daemon threads and are not waited for. Kept off the GUI
        # thread so a menu command cannot block the UI. server_close() must
        # always follow, or the listening socket stays bound and Stop -> Start
        # fails with EADDRINUSE.
        try:
            server.shutdown()
            if thread is not None:
                thread.join(timeout=10.0)
                if thread.is_alive():
                    FreeCAD.Console.PrintWarning(
                        "MCP RPC: server thread still draining a request; "
                        "socket closes when it finishes.\n"
                    )
        finally:
            server.server_close()
        FreeCAD.Console.PrintMessage("RPC Server stopped.\n")

    _stop_thread = threading.Thread(target=_shutdown_and_close, daemon=True)
    _stop_thread.start()
    return "RPC Server stopping…"


register_commands()
schedule_toggle_sync()
