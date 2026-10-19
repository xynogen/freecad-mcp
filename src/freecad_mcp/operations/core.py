import logging

from mcp.types import ImageContent

from ..freecad_client import FreeCADConnection
from ..responses import (
    ToolResponse,
    add_screenshot_if_available,
    error_response,
    json_response,
    text_response,
)
from ..version import addon_version_warning, is_missing_method_fault


logger = logging.getLogger("FreeCADMCPserver")


def execute_code_operation(
    freecad: FreeCADConnection,
    only_text_feedback: bool,
    code: str,
    include_screenshot: bool = True,
    view_name: str = "Isometric",
    timeout: float | None = None,
) -> ToolResponse:
    try:
        res = freecad.execute_code(code, timeout)
        if res["success"]:
            response = text_response(f"Code executed successfully: {res['message']}")
            # Only attempt screenshot when code completed and screenshots are wanted.
            # Skipping on failure avoids a second hanging call while the worker thread
            # may still be running.
            skip_screenshot = only_text_feedback or not include_screenshot
            screenshot = None if skip_screenshot else freecad.get_active_screenshot(view_name)
            return add_screenshot_if_available(response, screenshot, skip_screenshot)
        return error_response(f"Failed to execute code: {res['error']}")
    except Exception as e:
        logger.error(f"Failed to execute code: {str(e)}")
        return error_response(f"Failed to execute code: {str(e)}")


def format_headless_result(res: dict) -> str:
    if res.get("success"):
        text = "Headless FreeCAD script finished (exit 0)."
    else:
        text = f"Headless FreeCAD script FAILED: {res.get('error', 'unknown error')}"
    if res.get("output"):
        text += "\nOutput:\n" + str(res["output"]).rstrip()
    if res.get("success"):
        text += (
            "\nIf the script saved a document that is open in the GUI, reopen it with "
            "execute_code: App.closeDocument(name); App.openDocument(path)."
        )
    return text


def execute_code_headless_operation(
    command: list[str] | None, code: str, timeout: float
) -> ToolResponse:
    from ..headless import run_headless

    try:
        res = run_headless(code, timeout, command)
        respond = text_response if res.get("success") else error_response
        return respond(format_headless_result(res))
    except Exception as e:
        logger.error(f"Failed to run headless code: {str(e)}")
        return error_response(f"Failed to run headless code: {str(e)}")


def get_view_operation(
    freecad: FreeCADConnection,
    view_name: str,
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> ToolResponse:
    try:
        screenshot = freecad.get_active_screenshot(view_name, width, height, focus_object)
        if screenshot is not None:
            return [ImageContent(type="image", data=screenshot, mimeType="image/png")]
        return error_response("Cannot get screenshot in the current view type (such as TechDraw or Spreadsheet)")
    except Exception as e:
        logger.error(f"Failed to get view: {str(e)}")
        return error_response(f"Failed to get view: {str(e)}")


def get_rpc_status_operation(freecad: FreeCADConnection) -> ToolResponse:
    """Get bridge health through the GUI-independent RPC status method."""
    try:
        status = freecad.get_rpc_status()
        if isinstance(status, dict):
            status["version_check"] = addon_version_warning(status) or "ok"
        return json_response(status)
    except Exception as e:
        if is_missing_method_fault(e):
            return error_response(addon_version_warning(None))
        logger.error(f"Failed to get RPC status: {str(e)}")
        return error_response(f"Failed to get RPC status: {str(e)}")
