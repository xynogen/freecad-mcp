"""Sketch-first PartDesign helpers for execute_code scripts.

They follow the workflow of a CAD user: Body -> fully constrained Sketch on an
origin plane -> Pad/Pocket/Revolution. Features refuse a sketch that still has
degrees of freedom, so a model cannot skip the constraint step. Everything is
plain FreeCAD API; scripts can mix the helpers with raw calls.
"""

import FreeCAD as App
import Part
import Sketcher

V = App.Vector
C = Sketcher.Constraint
ROOT = (-1, 1)  # sketch origin point: H_Axis geometry id, start point


def body(name="Body", doc=None):
    """Add a PartDesign Body to ``doc`` (default: active document, created if missing)."""
    doc = doc or App.ActiveDocument or App.newDocument("Part")
    return doc.addObject("PartDesign::Body", name)


def sketch(body, on="XY", offset=0.0, name="Sketch"):
    """Add a sketch on an origin plane ("XY", "XZ", "YZ") moved ``offset`` mm
    along its normal, or on a face given as ``(feature, "FaceN")``."""
    sk = body.newObject("Sketcher::SketchObject", name)
    if isinstance(on, str):
        planes = {f.Role: f for f in body.Origin.OriginFeatures}
        if f"{on}_Plane" not in planes:
            sk.Document.removeObject(sk.Name)
            raise ValueError(f"on must be 'XY', 'XZ', 'YZ' or (feature, 'FaceN'), got {on!r}")
        sk.AttachmentSupport = [(planes[f"{on}_Plane"], "")]
    else:
        sk.AttachmentSupport = [tuple(on)]
    sk.MapMode = "FlatFace"
    sk.AttachmentOffset = App.Placement(V(0, 0, offset), App.Rotation())
    return sk


def rect(sk, w, h, x=0.0, y=0.0):
    """Add a fully constrained w x h rectangle centred at (x, y).

    Returns the line ids (bottom, right, top, left).
    """
    x0, y0 = x - w / 2, y - h / 2
    pts = [V(x0, y0, 0), V(x0 + w, y0, 0), V(x0 + w, y0 + h, 0), V(x0, y0 + h, 0)]
    g = sk.addGeometry([Part.LineSegment(pts[i], pts[(i + 1) % 4]) for i in range(4)])
    sk.addConstraint(
        [C("Coincident", g[i], 2, g[(i + 1) % 4], 1) for i in range(4)]
        + [C("Horizontal", g[0]), C("Horizontal", g[2]), C("Vertical", g[1]), C("Vertical", g[3])]
        + [
            C("DistanceX", g[0], 1, g[0], 2, w),
            C("DistanceY", g[1], 1, g[1], 2, h),
            C("DistanceX", *ROOT, g[0], 1, x0),
            C("DistanceY", *ROOT, g[0], 1, y0),
        ]
    )
    return g


def circle(sk, d, x=0.0, y=0.0):
    """Add a fully constrained circle of diameter ``d`` centred at (x, y); returns its id."""
    g = sk.addGeometry(Part.Circle(V(x, y, 0), V(0, 0, 1), d / 2))
    sk.addConstraint(
        [C("Diameter", g, d), C("DistanceX", *ROOT, g, 3, x), C("DistanceY", *ROOT, g, 3, y)]
    )
    return g


def check(sk):
    """Solve ``sk``; raise ValueError unless it is fully constrained and clean."""
    solved = sk.solve()
    status = {
        "dof": sk.DoF,
        # FreeCAD reports these as 1-based constraint numbers.
        "conflicting": list(sk.ConflictingConstraints),
        "redundant": list(sk.RedundantConstraints),
        "malformed": list(sk.MalformedConstraints),
    }
    if solved or any(status.values()):
        free = sorted({geo for geo, _pos in sk.getGeometryWithDependentParameters()})
        raise ValueError(
            f"Sketch '{sk.Name}' is not fully constrained: {status}; "
            f"geometry ids with free parameters: {free}"
        )
    return status


def _feature(sk, type_name, name, **props):
    check(sk)
    doc = sk.Document
    body = sk.getParentGeoFeatureGroup()
    tip = body.Tip
    feat = body.newObject(type_name, name)
    feat.Profile = sk
    for key, value in props.items():
        setattr(feat, key, value)
    sk.Visibility = False
    doc.recompute()
    if feat.isValid():
        return feat
    error = feat.getStatusString()
    # Drop the broken feature so a retry does not stack Pad001, Pad002, ...
    # and give the Body its previous tip back, or Body.Shape stays invalid.
    doc.removeObject(feat.Name)
    body.Tip = tip
    if tip is not None:
        tip.Visibility = True
    sk.Visibility = True
    doc.recompute()
    raise RuntimeError(f"{type_name} '{name}' failed: {error}")


def pad(sk, length, reversed=False, symmetric=False, name="Pad"):
    """Extrude a fully constrained sketch ``length`` mm along its normal
    (split evenly to both sides when ``symmetric``)."""
    side = "Symmetric" if symmetric else "One side"
    return _feature(
        sk, "PartDesign::Pad", name, Length=length, Reversed=reversed, SideType=side
    )


def pocket(sk, depth=None, reversed=False, name="Pocket"):
    """Cut ``depth`` mm against the sketch normal, or through all when ``depth`` is None."""
    props = {"Type": "ThroughAll"} if depth is None else {"Length": depth}
    return _feature(sk, "PartDesign::Pocket", name, Reversed=reversed, **props)


def revolve(sk, angle=360.0, axis="V_Axis", name="Revolution"):
    """Revolve a fully constrained sketch about its "V_Axis" or "H_Axis"."""
    return _feature(
        sk, "PartDesign::Revolution", name, ReferenceAxis=(sk, [axis]), Angle=angle
    )


HELPERS = {
    "Part": Part, "Sketcher": Sketcher, "V": V, "C": C, "body": body, "sketch": sketch,
    "rect": rect, "circle": circle, "check": check, "pad": pad, "pocket": pocket,
    "revolve": revolve,
}
