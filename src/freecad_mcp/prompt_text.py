INSTRUCTIONS = """
FreeCAD over MCP. Model parts the way a CAD user does, with execute_code.

Workflow for every part:
1. Read the request; pick the base plane and the first profile.
2. b = body("Name"); sk = sketch(b, "XY"); draw the profile with rect/circle
   or addGeometry + addConstraint.
3. check(sk) must pass: zero degrees of freedom, no conflicting or redundant
   constraints. Dimension every length, radius and position from the origin
   or other geometry. Never leave a sketch under-constrained.
4. pad / pocket / revolve the sketch. They refuse an unconstrained sketch.
5. Next feature: sketch on an origin plane with an offset, or on a face of
   the last feature (sketch(b, (feat, "Face6"))). Repeat 2-4.
6. Finish with fillets/chamfers, then verify: obj.isValid(),
   b.Shape.Volume, b.Shape.BoundBox. Look with get_view.

Rules:
- No Part::Box/Cylinder primitives or raw Part solids for parts; use sketches.
- One feature (or a few) per execute_code call. On error, read the message,
  fix that step, and continue; do not rebuild everything.
- include_screenshot=False for intermediate steps; get_view at the end.
- Ask before guessing dimensions the user did not give.
- execute_code_headless for OCCT work that may crash FreeCAD.
- get_rpc_status when FreeCAD stops answering.

The execute_code description lists the preloaded helpers and Sketcher API.
"""
