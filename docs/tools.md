# Tools

[Back to README](../README.md) · [Configuration](configuration.md) · [Code execution](execution.md)

## Available tools

The server keeps the tool list minimal. Models build parts with Python through
`execute_code`; the other tools cover what a script on the GUI thread cannot do.

| Tool | Purpose |
| --- | --- |
| `execute_code` | Run Python on FreeCAD's GUI thread. The modelling tool. |
| `execute_code_headless` | Run a script in a separate `freecadcmd` process, so an OpenCascade crash cannot take down the GUI. |
| `get_view` | Get a screenshot of the active view. |
| `get_rpc_status` | Report RPC and GUI-dispatch health, addon version, and version check without using the GUI thread. |

See [code execution](execution.md) for shared script state, headless scripts, and
timeout handling.

## Modelling workflow

The server sends this workflow to clients as its MCP instructions, so models
model the way a CAD user does:

1. Create a PartDesign Body.
2. Add a sketch on an origin plane or on a face of an earlier feature.
3. Fully constrain the sketch: zero degrees of freedom, no conflicting or
   redundant constraints.
4. Pad, pocket, or revolve the sketch.
5. Repeat for the next feature, then add fillets and chamfers and verify the
   result with `obj.isValid()`, `Shape.Volume`, and `get_view`.

## Sketch helpers

`execute_code` scripts start with these names preloaded from
`addon/FreeCADMCP/rpc_server/cad.py`. They are plain FreeCAD API and mix freely
with raw calls. If a script overwrites one, restore it with
`from rpc_server.cad import *`.

| Helper | Purpose |
| --- | --- |
| `body(name="Body")` | Add a `PartDesign::Body` to the active document (created if missing). |
| `sketch(body, on="XY", offset=0, name="Sketch")` | Add a sketch on `"XY"`, `"XZ"`, or `"YZ"` moved `offset` mm along its normal, or on a face given as `(feature, "FaceN")`. |
| `rect(sk, w, h, x=0, y=0)` | Add a fully constrained rectangle centred at `(x, y)`; returns the line ids. |
| `circle(sk, d, x=0, y=0)` | Add a fully constrained circle of diameter `d`; returns its geometry id. |
| `check(sk)` | Solve the sketch and raise unless it is fully constrained; the error lists the geometry ids that are still free. |
| `pad(sk, length, reversed=False, symmetric=False, name="Pad")` | Extrude the sketch. |
| `pocket(sk, depth=None, reversed=False, name="Pocket")` | Cut `depth` mm, or through all when `depth` is `None`. |
| `revolve(sk, angle=360, axis="V_Axis", name="Revolution")` | Revolve the sketch about its `"V_Axis"` or `"H_Axis"`. |

`pad`, `pocket`, and `revolve` call `check` first, so they refuse a sketch that
is not fully constrained. A feature that fails to recompute is removed, the Body
gets its previous tip back, and the error is raised, so a retry does not leave
broken objects behind.

`Part`, `Sketcher`, `V` (`FreeCAD.Vector`), and `C` (`Sketcher.Constraint`) are
also preloaded for profiles the helpers do not cover:

```python
b = body("Bracket")
sk = sketch(b, "XY")
rect(sk, 40, 20)
circle(sk, 6, x=-12)
plate = pad(sk, 5)

top = sketch(b, (plate, "Face6"), name="Boss")
g = top.addGeometry(Part.Circle(V(0, 0, 0), V(0, 0, 1), 2))
top.addConstraint([C("Radius", g, 2), C("DistanceX", -1, 1, g, 3, 0), C("DistanceY", -1, 1, g, 3, 0)])
pad(top, 2, name="BossPad")
```

## Screenshot options

`execute_code` returns an optional screenshot after a successful run.

| Parameter | Default | Purpose |
| --- | --- | --- |
| `include_screenshot` | `true` | Set to `false` for text-only feedback, such as queries or intermediate steps. |
| `view_name` | `"Isometric"` | Orient the returned screenshot, for example `"Front"`, `"Top"`, or `"Right"`. |

The [`--only-text-feedback` flag](configuration.md#text-feedback-and-screenshots)
suppresses these optional screenshots regardless of `include_screenshot`.

Use `get_view` to request a screenshot explicitly; it is available even with
`--only-text-feedback`. It takes `view_name` and optional `width`, `height`, and
`focus_object` parameters. Supported views are `Isometric`, `Front`, `Top`,
`Right`, `Back`, `Left`, `Bottom`, `Dimetric`, and `Trimetric`.
