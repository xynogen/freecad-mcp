"""Run the sketch-first helpers in a real headless FreeCAD; skip without one."""

from pathlib import Path

import pytest

from freecad_mcp.headless import detect_freecadcmd, run_headless

ADDON = Path(__file__).resolve().parents[1] / "addon" / "FreeCADMCP"

SCRIPT = f"""
import sys
sys.path.insert(0, {str(ADDON)!r})
from rpc_server.cad import *

b = body()
base = sketch(b, "XY")
rect(base, 40, 20)
circle(base, 6, x=-12)
assert check(base)["dof"] == 0
plate = pad(base, 5)

hole = sketch(b, "XY", offset=5)
circle(hole, 4, x=12)
pocket(hole)

loose = sketch(b, "XZ", name="Loose")
loose.addGeometry(Part.LineSegment(V(0, 0, 0), V(5, 0, 0)))
try:
    pad(loose, 3)
    raise AssertionError("pad accepted an under-constrained sketch")
except ValueError as e:
    assert "not fully constrained" in str(e), e

zero = sketch(b, "XY", offset=5, name="Zero")
rect(zero, 2, 2)
try:
    pad(zero, 0)
    raise AssertionError("pad accepted a zero length")
except RuntimeError:
    pass
assert b.Document.getObject("Pad001") is None, "failed feature was not removed"

volume = 40 * 20 * 5 - 3.14159265 * 9 * 5 - 3.14159265 * 4 * 5
assert abs(b.Shape.Volume - volume) < 1e-3, b.Shape.Volume

boss = sketch(b, (plate, "Face6"), name="Boss")  # top face of the plate
circle(boss, 4, x=0)
pad(boss, 2, name="BossPad")
assert abs(b.Shape.Volume - volume - 3.14159265 * 4 * 2) < 1e-3, b.Shape.Volume

shaft = body("Shaft")
profile = sketch(shaft, "XZ")
rect(profile, 4, 10, x=2, y=5)  # touches the V axis -> solid cylinder
revolve(profile)
assert abs(shaft.Shape.Volume - 3.14159265 * 16 * 10) < 1e-3, shaft.Shape.Volume
print("CAD_OK")
"""


def test_sketch_first_helpers_build_a_valid_body() -> None:
    command = detect_freecadcmd()
    if command is None:
        pytest.skip("freecadcmd not available")
    res = run_headless(SCRIPT, 120, command)
    assert res["success"] and "CAD_OK" in res["output"], res
