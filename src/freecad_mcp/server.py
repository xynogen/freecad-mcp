import functools
import logging
import xmlrpc.client
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Literal

try:
    # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP
except ImportError:
    # mcp 2.x moved mcp.server.fastmcp to mcp.server.mcpserver and renamed
    # FastMCP to MCPServer; the API surface used here is unchanged.
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent

from .freecad_client import FreeCADConnection
from .operations import (
    execute_code_headless_operation,
    execute_code_operation,
    get_rpc_status_operation,
    get_view_operation,
)
from .prompt_text import INSTRUCTIONS
from .responses import ErrorResponse, ToolResponse
from .server_state import ServerState


logging.basicConfig(
    level=logging.WARNING, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("FreeCADMCPserver")
logger.setLevel(logging.INFO)

ViewName = Literal[
    "Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"
]

state = ServerState()


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    try:
        logger.info("FreeCADMCP server starting up")
        try:
            _ = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD on startup")
        except Exception as e:
            logger.warning(f"Could not connect to FreeCAD on startup: {str(e)}")
            logger.warning(
                "Make sure the FreeCAD addon is running before using FreeCAD resources or tools"
            )
        yield {}
    finally:
        if state.freecad_connection:
            logger.info("Disconnecting from FreeCAD on shutdown")
            state.freecad_connection.disconnect()
            state.freecad_connection = None
        logger.info("FreeCADMCP server shut down")


mcp = FastMCP(
    "FreeCADMCP",
    instructions=INSTRUCTIONS,
    lifespan=server_lifespan,
)


def tool(fn: Callable[..., ToolResponse]) -> Callable[..., ToolResponse | CallToolResult]:
    """Register ``fn`` as a tool whose next reply carries a pending version warning.

    A failure returned as an ``ErrorResponse`` is sent with ``isError`` set.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> ToolResponse | CallToolResult:
        response = fn(*args, **kwargs)
        is_error = isinstance(response, ErrorResponse)
        notice, state.version_notice = state.version_notice, None
        if notice:
            response = [TextContent(type="text", text=f"Warning: {notice}"), *response]
        if is_error:
            return CallToolResult(content=list(response), isError=True)
        return response

    return mcp.tool(structured_output=False)(wrapper)


def get_freecad_connection() -> FreeCADConnection:
    """Get or create a persistent FreeCAD connection.

    The connection is cached only after FreeCAD answers, so a server started
    before FreeCAD checks the addon version on the first call that reaches it.
    """
    if state.freecad_connection is None:
        connection = FreeCADConnection(
            host=state.rpc_host, port=9875, token=state.auth_token
        )
        try:
            # ping() raises, rather than returning False, when nothing listens.
            reachable = connection.ping()
        except Exception as e:
            connection.disconnect()
            if isinstance(e, xmlrpc.client.ProtocolError) and e.errcode == 401:
                raise Exception(
                    "FreeCAD rejected the connection: the addon requires an auth "
                    "token, and none or a different one was given. Pass the token "
                    "set with 'Set Auth Token' in FreeCAD through the "
                    "FREECAD_MCP_TOKEN environment variable or --auth-token."
                ) from e
            raise Exception(
                f"Failed to connect to FreeCAD ({e}). Make sure the FreeCAD addon is running."
            ) from e
        if not reachable:
            logger.error("Failed to ping FreeCAD")
            connection.disconnect()
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
        state.version_notice = connection.check_addon_version()
        if state.version_notice:
            logger.warning(state.version_notice)
        state.freecad_connection = connection
    return state.freecad_connection


@tool
def execute_code(
    ctx: Context,
    code: str,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
    timeout: float | None = None,
) -> list[TextContent | ImageContent]:
    """Run Python in the FreeCAD GUI for live adjustments and inspection.

    Model like a CAD user: Body -> sketch on a plane or face -> fully
    constrain it -> Pad/Pocket/Revolution -> next sketch. Never build parts
    from Part primitives or raw solids. One call per feature or small group
    of features; read the error, fix, continue.
    Use execute_file for main scripts, in GUI or isolated headless mode.

    Preloaded (FreeCAD/App, FreeCADGui/Gui, Part, Sketcher, V=App.Vector,
    C=Sketcher.Constraint). Restore with `from rpc_server.cad import *`:
        body(name="Body") -> PartDesign::Body in the active document
        sketch(body, on="XY"|"XZ"|"YZ"|(feature, "FaceN"), offset=0, name="Sketch")
        rect(sk, w, h, x=0, y=0)  -> line ids; centred, fully constrained
        circle(sk, d, x=0, y=0)   -> geometry id; fully constrained
        check(sk)  -> raises unless DoF == 0 with no conflicting/redundant
                      constraints; the error lists the free geometry ids
        pad(sk, length, reversed=False, symmetric=False, name="Pad")
        pocket(sk, depth=None (through all), reversed=False, name="Pocket")
        revolve(sk, angle=360, axis="V_Axis"|"H_Axis", name="Revolution")
    pad/pocket/revolve call check() first, and remove a feature that fails
    to recompute and raise its error.

    Other profiles: sk.addGeometry(Part.LineSegment/Circle/ArcOfCircle(...))
    then sk.addConstraint([C("Coincident", g1, 2, g2, 1), C("Horizontal", g),
    C("DistanceX", -1, 1, g, 1, x), C("Radius", g, r), ...]) until check()
    passes. Point ids: 1 start, 2 end, 3 centre; geometry -1 = H axis (its
    point 1 is the sketch origin), -2 = V axis. Faces of a feature: inspect
    f.Shape.Faces[i].Surface / CenterOfMass, name is f"Face{i+1}".
    Fillet/Chamfer: b.newObject("PartDesign::Fillet", n) with
    Base = (feature, ["Edge1", ...]) and Radius, then recompute.

    print() output is returned; state persists between calls. Inspect with
    App.ActiveDocument.Objects, obj.Shape.Volume/BoundBox, obj.isValid().

    Args:
        code: Python to run. Use execute_file to run an existing file.
        include_screenshot: Return a screenshot (default True). Pass False
            for intermediate steps and queries; use get_view at the end.
        view_name: Screenshot orientation (default "Isometric").
        timeout: Seconds for each of the queue and run budgets (default 90,
            max 1800). Raise it for slow GUI work such as big STEP imports.
            Use execute_file with headless=True for OCCT work that may crash.
    """
    return execute_code_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        code,
        include_screenshot,
        view_name,
        timeout,
    )


@tool
def execute_file(
    ctx: Context,
    path: str,
    headless: bool = False,
    include_screenshot: bool = True,
    view_name: ViewName = "Isometric",
    timeout: float | None = None,
) -> list[TextContent | ImageContent]:
    """Run a main Python script or FCMacro from an absolute path.

    The file is read on every call by the FreeCAD process, not uploaded or
    copied. With headless=False it must be accessible to the GUI user on the
    machine selected by --host, including inside any Flatpak/container sandbox.
    With headless=True it must be accessible to freecadcmd on the MCP server
    machine, independently of --host.

    GUI execution shares execute_code's persistent namespace and CAD helpers.
    Headless execution starts a fresh process without GUI or those helpers;
    import FreeCAD/Part and save results yourself. Both set __file__ to path
    and __name__ to '__main__'. This saves source transfer, not computation.

    Args:
        path: Absolute path to a Python script or FCMacro readable by FreeCAD.
        headless: Use an isolated freecadcmd process (default False). Use True
            for OCCT work that may crash or block the GUI.
        include_screenshot: Return a screenshot after GUI success (default True).
            Ignored when headless=True.
        view_name: GUI screenshot orientation (default "Isometric").
        timeout: Positive finite seconds. GUI: queue and run budgets, default
            90, max 1800. Headless: process limit, default 600, kills the process
            on timeout and preserves partial output.

    Returns:
        GUI execution output and optional screenshot, or headless exit status,
        crash/timeout diagnosis and printed output.
    """
    if headless:
        return execute_code_headless_operation(
            state.freecadcmd, None, 600 if timeout is None else timeout, path
        )
    return execute_code_operation(
        get_freecad_connection(),
        state.only_text_feedback,
        None,
        include_screenshot,
        view_name,
        timeout,
        path,
    )


@tool
def get_view(
    ctx: Context,
    view_name: ViewName,
    width: int | None = None,
    height: int | None = None,
    focus_object: str | None = None,
) -> list[ImageContent | TextContent]:
    """Get a screenshot of the active view.

    Args:
        view_name: The name of the view to get the screenshot of.
        The following views are available:
        - "Isometric"
        - "Front"
        - "Top"
        - "Right"
        - "Back"
        - "Left"
        - "Bottom"
        - "Dimetric"
        - "Trimetric"
        width: The width of the screenshot in pixels. If not specified, uses the viewport width.
        height: The height of the screenshot in pixels. If not specified, uses the viewport height.
        focus_object: The name of the object to focus on. If not specified, fits all objects in the view.

    Returns:
        A screenshot of the active view.
    """
    return get_view_operation(get_freecad_connection(), view_name, width, height, focus_object)


@tool
def get_rpc_status(ctx: Context) -> list[TextContent]:
    """Get RPC and FreeCAD GUI-dispatch health.

    This tool does not use FreeCAD's GUI thread, so it remains available after
    a GUI operation times out. A ``stuck`` state identifies the operation that
    is still running and indicates that FreeCAD may need to be restarted.
    ``version_check`` is "ok" or says whether the addon or the server needs
    updating.
    """
    return get_rpc_status_operation(get_freecad_connection())
def _validate_host(value: str) -> str:
    """Validate that *value* is a valid IP address or hostname.

    Used as the ``type`` callback for the ``--host`` argparse argument.
    Raises ``argparse.ArgumentTypeError`` on invalid input.
    """
    import argparse

    import validators

    if validators.ipv4(value) or validators.ipv6(value) or validators.hostname(value):
        return value
    raise argparse.ArgumentTypeError(
        f"Invalid host: '{value}'. Must be a valid IP address or hostname."
    )


def main():
    """Run the MCP server"""
    import argparse
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--only-text-feedback", action="store_true", help="Only return text feedback")
    parser.add_argument("--host", type=_validate_host, default="localhost", help="Host address of the FreeCAD RPC server to connect to (default: localhost)")
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Auth token the FreeCAD RPC server requires (falls back to the "
        "FREECAD_MCP_TOKEN environment variable, which is preferable because "
        "other users can read command-line arguments; omit if the addon has "
        "no token set)",
    )
    parser.add_argument("--freecadcmd", default=None, help="Command that starts headless FreeCAD for execute_file(headless=True), e.g. 'flatpak run --command=freecadcmd org.freecad.FreeCAD' (default: auto-detect PATH, then Flatpak)")
    args = parser.parse_args()
    state.only_text_feedback = args.only_text_feedback
    state.rpc_host = args.host
    state.auth_token = args.auth_token or os.environ.get("FREECAD_MCP_TOKEN") or None
    from .headless import parse_command
    state.freecadcmd = parse_command(args.freecadcmd)
    logger.info(f"Only text feedback: {state.only_text_feedback}")
    logger.info(f"Connecting to FreeCAD RPC server at: {state.rpc_host}")
    if state.auth_token:
        logger.info("Auth token configured for RPC connection")
    mcp.run()
