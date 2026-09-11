# -*- coding: utf-8 -*-
"""
build_from_ir.py - deterministic FreeCAD builder for the part IR.

Runs INSIDE freecadcmd.  Given the JSON IR (see src/core/ir.py), it builds the
solid and writes the same artefacts the legacy generated scripts wrote:

    <output_dir>/<title>.step
    <output_dir>/<title>.obj                 (per-face meshes, as before)
    <output_dir>/<title>_geometry.json       (volume / mass, read by the drawing)
    <output_dir>/metadata.json               (threads, slots, bends - read by the drawing)
    <output_dir>/<title>_ir.json             (the IR that was built, for traceability)

Geometry rules live in ir_frames.py (pure python, shared with the API side);
this file only turns frames into Part shapes:

    blank  : Part.Face -> extrude              (rect / stadium / disc or sector / polygon)
    bends  : SheetMetal SMBendWall, one call per group of identical bends on the
             same parent (AutoMiter closes CAPOT corners, like makeTub did);
             SheetMetal fold-on-a-line for bend lines across the blank (disc L/U/Z);
             revolved profile for a rim all around a disc (round cover, collar neck)
    feature: tool built in the face's local (u, v, n) frame, moved with a Matrix,
             all tools cut in one compound cut (per-tool fallback if that fails)

Local run (outside the worker):

    set TOLERY_IR_JOB=job.json      # {"ir": {...}, "title": "x", "output_dir": "...", "material": "steel", "report": "out.json"}
    freecadcmd FreeCadUtil/build_from_ir.py
"""
import os
import sys
import json
import math
import time

import FreeCAD as App
import Part

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from FreeCadUtil import ir_frames as F                                   # noqa: E402
from FreeCadUtil.PlateFunction import (_ensure_sheetmetal, makeOblong, makeKeyhole,   # noqa: E402
                                       makeHexagon, makeCountersink, makeDiagonalCornerCut)
from FreeCadUtil.TubeFunction import (makeRectangularTube, makeCircularTube,          # noqa: E402
                                      create_square_tube_angled_cuts)
from FreeCadUtil.GeometryAnalyzer import export_geometry_to_json          # noqa: E402

MARGIN = 1.0          # mm of tool overshoot outside the material
EDGE_TOL = 0.25       # mm tolerance when looking up a bend edge by its end points


class BuildError(RuntimeError):
    pass


def V(t):
    return App.Vector(float(t[0]), float(t[1]), float(t[2]))


def _frame_matrix(face):
    u, v, n, o = face.u, face.v, face.n, face.origin
    return App.Matrix(u[0], v[0], n[0], o[0],
                      u[1], v[1], n[1], o[1],
                      u[2], v[2], n[2], o[2],
                      0.0, 0.0, 0.0, 1.0)


def _bbox(shape):
    try:
        b = shape.optimalBoundingBox(True, False)   # revolved surfaces get a loose BoundBox otherwise
    except Exception:
        b = shape.BoundBox
    return [round(b.XMin, 3), round(b.XMax, 3), round(b.YMin, 3), round(b.YMax, 3), round(b.ZMin, 3), round(b.ZMax, 3)]


# ---------------------------------------------------------------- blank

def _corner_edges(solid, x, y, t):
    """Vertical thickness edges of a plate at plan corner (x, y)."""
    out = []
    for e in solid.Edges:
        if abs(e.Length - t) > 1e-3:
            continue
        p0, p1 = e.Vertexes[0].Point, e.Vertexes[-1].Point
        if abs(p0.x - x) < 1e-3 and abs(p0.y - y) < 1e-3 and abs(p1.x - x) < 1e-3 and abs(p1.y - y) < 1e-3:
            out.append(e)
    return out


def make_blank(ir, plan):
    t = ir["thickness"]
    blank = ir["blank"]
    base = plan.faces["base"]
    if blank["type"] == "rect":
        X, Y = blank["x"], blank["y"]
        sxm, sxp, sym, syp = base.shrink["u-"], base.shrink["u+"], base.shrink["v-"], base.shrink["v+"]
        solid = Part.makeBox(X - sxm - sxp, Y - sym - syp, t, App.Vector(sxm, sym, 0))
        # corner treatments only on corners where both adjacent edges are free
        free = {"x-y-": sxm == 0 and sym == 0, "x+y-": sxp == 0 and sym == 0,
                "x-y+": sxm == 0 and syp == 0, "x+y+": sxp == 0 and syp == 0}
        xy = {"x-y-": (0.0, 0.0), "x+y-": (X, 0.0), "x-y+": (0.0, Y), "x+y+": (X, Y)}
        fillets, chamfers = {}, {}
        for c, ok in free.items():
            if not ok:
                continue
            spec = blank["corners"].get(c, {})
            r = spec.get("radius", blank.get("corner_radius"))
            ch = spec.get("chamfer", blank.get("corner_chamfer"))
            if r:
                fillets.setdefault(float(r), []).append(c)
            elif ch:
                chamfers.setdefault(float(ch), []).append(c)
        for r, cs in fillets.items():
            edges = [e for c in cs for e in _corner_edges(solid, *xy[c], t=t)]
            if edges:
                solid = solid.makeFillet(r, edges)
        for ch, cs in chamfers.items():
            edges = [e for c in cs for e in _corner_edges(solid, *xy[c], t=t)]
            if edges:
                solid = solid.makeChamfer(ch, edges)
        perf = next((i["feature"] for i in plan.instances if i["feature"]["type"] == "perforation"), None)
        if perf is not None:
            solid = _perforated_solid(solid, ir, perf)
        return solid
    if blank["type"] == "stadium":
        return makeOblong(blank["x"], blank["y"], t, App.Vector(0, 0, 0), App.Vector(0, 0, 1))
    if blank["type"] == "disc":
        if plan.rims:
            return make_rim_solid(ir, plan)
        R = blank["diameter"] / 2.0
        cx, cy = blank["center"]
        arc = blank["arc"]
        centre, axis = App.Vector(cx, cy, 0), App.Vector(0, 0, 1)
        solid = Part.makeCylinder(R, t, centre, axis, arc) if arc < 360.0 - 1e-9 else Part.makeCylinder(R, t, centre, axis)
        if blank.get("inner_diameter"):
            solid = solid.cut(Part.makeCylinder(blank["inner_diameter"] / 2.0, t + 2 * MARGIN, App.Vector(cx, cy, -MARGIN), axis))
        # a flange on the straight edge of a half / quarter disc consumes s_corner of the blank at that edge
        big = 4.0 * R + 10.0
        if base.shrink["v-"] > 0:
            solid = solid.cut(Part.makeBox(big, base.shrink["v-"] + MARGIN, t + 2 * MARGIN, App.Vector(-R - MARGIN, -MARGIN, -MARGIN)))
        if base.shrink["u-"] > 0:
            solid = solid.cut(Part.makeBox(base.shrink["u-"] + MARGIN, big, t + 2 * MARGIN, App.Vector(-MARGIN, -R - MARGIN, -MARGIN)))
        return solid
    if blank["type"] == "polygon":
        pts = F.shrink_polygon(base.poly, {k: v for k, v in base.shrink.items() if k.startswith("e")})
        vecs = [App.Vector(p[0], p[1], 0.0) for p in pts]
        face = Part.Face(Part.makePolygon(vecs + [vecs[0]]))
        return face.extrude(App.Vector(0, 0, t))
    raise BuildError("unknown blank type %r" % blank["type"])


def _hole_wire(f, cx, cy):
    """Closed 2D outline of one perforation hole at z = 0, counter-clockwise."""
    z = App.Vector(0, 0, 1)
    c = App.Vector(cx, cy, 0)
    w, l = f["hole_w"], f["hole_l"]
    if f["shape"] == "R":
        return Part.Wire(Part.makeCircle(w / 2.0, c, z))
    if f["shape"] == "C":
        h2 = w / 2.0
        return Part.makePolygon([c + App.Vector(-h2, -h2, 0), c + App.Vector(h2, -h2, 0), c + App.Vector(h2, h2, 0),
                                 c + App.Vector(-h2, h2, 0), c + App.Vector(-h2, -h2, 0)])
    if f["shape"] == "LC":
        hl, hw = l / 2.0, w / 2.0
        return Part.makePolygon([c + App.Vector(-hl, -hw, 0), c + App.Vector(hl, -hw, 0), c + App.Vector(hl, hw, 0),
                                 c + App.Vector(-hl, hw, 0), c + App.Vector(-hl, -hw, 0)])
    r, dx = w / 2.0, (l - w) / 2.0                       # LR stadium, long axis along x
    c1, c2 = c + App.Vector(-dx, 0, 0), c + App.Vector(dx, 0, 0)
    return Part.Wire([Part.LineSegment(c1 + App.Vector(0, -r, 0), c2 + App.Vector(0, -r, 0)).toShape(),
                      Part.makeCircle(r, c2, z, -90, 90),
                      Part.LineSegment(c2 + App.Vector(0, r, 0), c1 + App.Vector(0, r, 0)).toShape(),
                      Part.makeCircle(r, c1, z, 90, 270)])


def _perforated_solid(solid, ir, f):
    """Plate with its perforation: top-face outer wire + one REVERSED wire per hole -> Part.Face -> extrude.

    The 3D boolean cut of thousands of holes is superlinear (minutes for 10k holes);
    the 2D face route is ~100x faster and gives the same solid.  The volume is checked
    and a boolean cut is used as a fallback if the face route ever disagrees.
    """
    t = ir["thickness"]
    lay = f["layout"]
    if not lay["points"]:
        return solid
    top = None
    for face in solid.Faces:
        if face.Surface.__class__.__name__ == "Plane" and face.normalAt(0, 0).z > 0.95 and abs(face.CenterOfMass.z - t) < 1e-6:
            if top is None or face.Area > top.Area:
                top = face
    expected = solid.Volume - lay["count"] * f["hole_area"] * t
    try:
        outer = top.OuterWire.copy()
        outer.translate(App.Vector(0, 0, -t))
        inners = []
        for (cx, cy) in lay["points"]:
            wire = _hole_wire(f, cx, cy)
            wire.reverse()
            inners.append(wire)
        plate = Part.Face([outer] + inners).extrude(App.Vector(0, 0, t))
        if plate.isValid() and len(plate.Solids) == 1 and abs(plate.Volume - expected) <= max(1.0, 1e-4 * expected):
            return plate
        print("[IR_BUILD] perforation face route gave volume %.1f (expected %.1f), falling back to a boolean cut" % (plate.Volume, expected))
    except Exception as exc:
        print("[IR_BUILD] perforation face route failed (%s), falling back to a boolean cut" % exc)
    tools = []
    for (cx, cy) in lay["points"]:
        wire = _hole_wire(f, cx, cy)
        wire.translate(App.Vector(0, 0, -MARGIN))
        tools.append(Part.Face(wire).extrude(App.Vector(0, 0, t + 2 * MARGIN)))
    return solid.cut(Part.makeCompound(tools))


def make_profile(ir):
    """T / I structural profile: flange(s) + web as fused boxes along y, optional inner fillets at the junctions."""
    p = ir["profile"]
    W, H, L, t = p["width"], p["height"], p["length"], p["thickness"]
    xw = (W - t) / 2.0
    parts = [Part.makeBox(W, L, t), Part.makeBox(t, L, H, App.Vector(xw, 0, t))]
    if p["section"] == "I":
        parts.append(Part.makeBox(W, L, t, App.Vector(0, 0, t + H)))
    shape = parts[0].fuse(parts[1:]).removeSplitter()
    r = p.get("radius") or 0.0
    if r > 0:
        targets = [(xw, t), (xw + t, t)] + ([(xw, t + H), (xw + t, t + H)] if p["section"] == "I" else [])
        edges = [e for e in shape.Edges if abs(e.Length - L) < 1e-6
                 and any(abs(e.CenterOfMass.x - x) < 1e-6 and abs(e.CenterOfMass.z - z) < 1e-6 for x, z in targets)]
        try:
            res = shape.makeFillet(r, edges)
            if res.isValid():
                shape = res
        except Exception:
            pass
    return shape


def _revolved_profile_faces(ir, plan, w):
    """Bend arc + flange of a rim wall as faces in the half-plane through the disc axis (y = cy, x >= cx)."""
    t = ir["thickness"]
    b = w.bend
    cx, cy = b["center"]
    r_b = max(float(b["radius"]), 0.05)
    rho = float(b["rho"])
    d = -1.0 if b["invert"] else 1.0
    outward = 1.0 if b["rim"] == "outer" else -1.0
    x0 = b["x0"]
    Cz = t + r_b if d > 0 else -r_b
    v0 = -90.0 if d > 0 else 90.0
    sweep = outward * d
    v1 = v0 + sweep * rho

    def P(rad, ang):
        a = math.radians(ang)
        return App.Vector(cx + x0 + rad * math.cos(a), cy, Cz + rad * math.sin(a))

    mid = (v0 + v1) / 2.0
    arc = Part.Face(Part.Wire([Part.LineSegment(P(r_b, v0), P(r_b + t, v0)).toShape(),
                               Part.Arc(P(r_b + t, v0), P(r_b + t, mid), P(r_b + t, v1)).toShape(),
                               Part.LineSegment(P(r_b + t, v1), P(r_b, v1)).toShape(),
                               Part.Arc(P(r_b, v1), P(r_b, mid), P(r_b, v0)).toShape()]))
    a1 = math.radians(v1)
    direction = App.Vector(-math.sin(a1) * sweep, 0.0, math.cos(a1) * sweep)
    p_in, p_out = P(r_b, v1), P(r_b + t, v1)
    flap = Part.Face(Part.makePolygon([p_in, p_out, p_out + direction * b["flat"], p_in + direction * b["flat"], p_in]))
    return arc, flap


def make_rim_solid(ir, plan):
    """Disc with a flange all around its edge (round cover / cup) and/or around its bore (collar neck):
    flat annulus + revolved bend arc + revolved flange, one solid."""
    t = ir["thickness"]
    blank = ir["blank"]
    R = blank["diameter"] / 2.0
    cx, cy = blank["center"]
    A, Z = App.Vector(cx, cy, 0), App.Vector(0, 0, 1)
    ro, ri = R, (blank.get("inner_diameter") or 0.0) / 2.0
    walls = [plan.faces[n] for n in plan.rims]
    for w in walls:
        if w.bend["rim"] == "outer":
            ro = w.bend["x0"]
        else:
            ri = w.bend["x0"]
    solid = Part.makeCylinder(ro, t, A, Z)
    if ri > 0:
        solid = solid.cut(Part.makeCylinder(ri, t + 2 * MARGIN, A - Z * MARGIN, Z))
    parts = []
    for w in walls:
        for face in _revolved_profile_faces(ir, plan, w):
            parts.append(face.revolve(A, Z, 360.0))
    solid = solid.fuse(parts) if parts else solid
    if not solid.isValid() or len(solid.Solids) != 1:
        raise BuildError("the rim could not be joined to the disc")
    return solid.removeSplitter()


# ---------------------------------------------------------------- bends

def _base_edge_points(ir, plan, wall):
    """3D end points of the edge (on the parent's +n surface) that wall hangs from."""
    t = ir["thickness"]
    parent = plan.faces[wall.parent]
    if parent.kind == "base":
        blank = ir["blank"]
        if blank["type"] in ("rect", "stadium"):
            X, Y = blank["x"], blank["y"]
            s = parent.shrink
            e = wall.edge_of_parent
            if e == "x-":
                return (s["u-"], s["v-"], t), (s["u-"], Y - s["v+"], t)
            if e == "x+":
                return (X - s["u+"], s["v-"], t), (X - s["u+"], Y - s["v+"], t)
            if e == "y-":
                return (s["u-"], s["v-"], t), (X - s["u+"], s["v-"], t)
            return (s["u-"], Y - s["v+"], t), (X - s["u+"], Y - s["v+"], t)
        if blank["type"] == "polygon":
            pts = F.shrink_polygon(parent.poly, {k: v for k, v in parent.shrink.items() if k.startswith("e")})
            i = int(wall.edge_of_parent[1:])
            q1, q2 = pts[i], pts[(i + 1) % len(pts)]
            return (q1[0], q1[1], t), (q2[0], q2[1], t)
        if blank["type"] == "disc":           # straight edge of a half / quarter disc
            p1, p2 = F.disc_edge_span(blank, wall.edge_of_parent, parent.shrink)
            return (p1[0], p1[1], t), (p2[0], p2[1], t)
        raise BuildError("bends on a %s blank are not supported" % blank["type"])
    # child of a wall: tip edge on the parent's inner surface
    vt = parent.V - parent.shrink["v+"]
    return parent.point(0.0, vt, t), parent.point(parent.U, vt, t)


def _find_edge_on_segment(shape, p1, p2, tol=EDGE_TOL):
    """Name of the straight edge lying on the line p1-p2 that overlaps that segment.

    Exact end points score best; a mitered wall tip (AutoMiter extends the wall to
    the corner) is still found because only collinearity + overlap are required.
    """
    P1, P2 = V(p1), V(p2)
    d = P2 - P1
    L = d.Length
    if L < 1e-9:
        return None
    dn = App.Vector(d.x / L, d.y / L, d.z / L)
    best, best_score = None, 1e18
    for i, e in enumerate(shape.Edges, 1):
        if len(e.Vertexes) != 2:
            continue
        a, b = e.Vertexes[0].Point, e.Vertexes[1].Point
        ed = b - a
        el = ed.Length
        if el < 1e-6 or abs(abs(ed.dot(dn)) - el) > 1e-3 * max(1.0, el):
            continue                                  # not parallel
        w = a - P1
        perp = w - dn * w.dot(dn)
        if perp.Length > tol:
            continue                                  # not on the line
        ta, tb = sorted(((a - P1).dot(dn), (b - P1).dot(dn)))
        overlap = min(tb, L) - max(ta, 0.0)
        if overlap < 0.5 * L:
            continue
        score = abs(ta) + abs(tb - L)
        if score < best_score:
            best, best_score = "Edge%d" % i, score
    return best


def _top_face_name(shape, t):
    """Largest planar face lying on z = t with its normal up: the flat top the fold line is drawn on."""
    best, area = None, -1.0
    for i, f in enumerate(shape.Faces, 1):
        if f.Surface.__class__.__name__ != "Plane":
            continue
        if f.normalAt(0, 0).z > 0.95 and abs(f.CenterOfMass.z - t) < 1e-3 and f.Area > area:
            best, area = "Face%d" % i, f.Area
    return best


def apply_folds(doc, solid, ir, plan):
    """Bend lines ACROSS the blank (disc L / U / Z, folded plates): SheetMetal fold on a line.

    The flat part on the centre side stays where it is, the material beyond the line
    becomes the flange (bend zone of K = 0.5 starting at x0, like SMBendWall).  Which
    side SheetMetal keeps fixed depends on its internal split order, so the result is
    probed and the fold redone with `invertbend` when the wrong half moved.
    """
    if not plan.folds:
        return solid, None
    if not _ensure_sheetmetal():
        raise BuildError("SheetMetal workbench not available")
    from sheetmetal.SheetMetalFoldCmd import SMFoldWall
    t = ir["thickness"]
    blank = ir["blank"]
    current = doc.addObject("Part::Feature", "FoldBlank")
    current.Shape = solid
    doc.recompute()
    bb = solid.BoundBox
    L = bb.DiagonalLength + 50.0
    for i, name in enumerate(plan.folds):
        b = plan.faces[name].bend
        x0, axis, sign = b["x0"], b["axis"], b["sign"]
        # probe = centroid of the flat (fixed) region, well away from the bend zone
        fixed = F.clip_halfplane(F.blank_outline(blank), axis, x0, -sign)
        for other in plan.folds:                                   # earlier / later folds also leave the middle flat
            ob = plan.faces[other].bend
            if other != name:
                fixed = F.clip_halfplane(fixed, ob["axis"], ob["x0"], -ob["sign"])
        pc = F.polygon_centroid(fixed)
        probe = App.Vector(pc[0], pc[1], t / 2.0)
        if axis == "x":
            line = Part.LineSegment(App.Vector(x0, bb.Center.y - L, t), App.Vector(x0, bb.Center.y + L, t)).toShape()
        else:
            line = Part.LineSegment(App.Vector(bb.Center.x - L, x0, t), App.Vector(bb.Center.x + L, x0, t)).toShape()
        fname = _top_face_name(current.Shape, t)
        if not fname:
            raise BuildError("cannot find the flat top face for the fold %r" % name)
        lo = doc.addObject("Part::Feature", "FoldLine%d" % (i + 1))
        lo.Shape = line
        fo = doc.addObject("Part::FeaturePython", "Fold%d" % (i + 1))
        SMFoldWall(fo, current, [fname], lo)
        fo.radius = float(b["radius"])
        fo.angle = float(b["rho"])
        fo.kfactor = 0.5
        fo.Position = "forward"
        fo.invert = bool(b["invert"])
        fo.invertbend = False
        doc.recompute()
        shp = fo.Shape
        if shp.isNull() or not shp.isInside(probe, 1e-6, True):
            fo.invertbend = True
            doc.recompute()
            shp = fo.Shape
        if shp.isNull() or not shp.isValid() or len(shp.Solids) != 1 or not shp.isInside(probe, 1e-6, True):
            raise BuildError("fold %r produced an invalid shape" % name)
        current = fo
    return current.Shape, current


def _manual_wall(ir, wall):
    """Bend arc + flat flange on a straight edge, built as solids in the wall's own frame.

    Local frame: u along the edge, o = outward from the parent, n = into the parent's material
    (the parent's outer surface is n = 0, the outside sharp corner is o = 0).
    """
    t = ir["thickness"]
    b = wall.bend
    r = max(float(b["radius"]), 0.05)
    rho = float(b["rho"])
    rr = math.radians(rho)
    # the wall frame gives v (along the flange) and n (into the flange); recover the parent's o and n
    cos_r, sin_r = math.cos(rr), math.sin(rr)
    o_hat = V(F.v_add(F.v_mul(wall.v, cos_r), F.v_mul(wall.n, -sin_r)))
    n_par = V(F.v_add(F.v_mul(wall.v, sin_r), F.v_mul(wall.n, cos_r)))
    u_hat = V(wall.u)
    O = V(wall.origin)

    def P(o, n):
        return O + o_hat * o + n_par * n

    s_c = b["s_corner"]
    C = (-s_c, t + r)
    a0, a1 = -math.pi / 2.0, -math.pi / 2.0 + rr

    def arc_pt(rad, a):
        return P(C[0] + rad * math.cos(a), C[1] + rad * math.sin(a))

    mid = (a0 + a1) / 2.0
    arc_face = Part.Face(Part.Wire([Part.LineSegment(arc_pt(r, a0), arc_pt(r + t, a0)).toShape(),
                                    Part.Arc(arc_pt(r + t, a0), arc_pt(r + t, mid), arc_pt(r + t, a1)).toShape(),
                                    Part.LineSegment(arc_pt(r + t, a1), arc_pt(r, a1)).toShape(),
                                    Part.Arc(arc_pt(r, a1), arc_pt(r, mid), arc_pt(r, a0)).toShape()]))
    parts = [arc_face.extrude(u_hat * wall.U)]
    flat = b["sm_length"]
    if flat > 1e-6:
        d = (-math.sin(a1), math.cos(a1))
        p_in, p_out = arc_pt(r, a1), arc_pt(r + t, a1)
        step = o_hat * (d[0] * flat) + n_par * (d[1] * flat)
        flap = Part.Face(Part.makePolygon([p_in, p_out, p_out + step, p_in + step, p_in]))
        parts.append(flap.extrude(u_hat * wall.U))
    return parts


def add_manual_walls(shape, ir, plan):
    if not plan.manual_walls:
        return shape
    parts = []
    for name in plan.manual_walls:
        parts.extend(_manual_wall(ir, plan.faces[name]))
    fused = shape.fuse(parts)
    if not fused.isValid() or len(fused.Solids) != 1:
        raise BuildError("the flange(s) %s could not be joined to the plate" % (plan.manual_walls,))
    return fused.removeSplitter()


def apply_bends(doc, base_solid, ir, plan):
    if not plan.bend_groups:
        return base_solid, None
    if not _ensure_sheetmetal():
        raise BuildError("SheetMetal workbench not available")
    import SheetMetalCmd
    current = doc.addObject("Part::Feature", "Blank")
    current.Shape = base_solid
    doc.recompute()
    for gi, g in enumerate(plan.bend_groups):
        names = []
        for fname in g["faces"]:
            wall = plan.faces[fname]
            p1, p2 = _base_edge_points(ir, plan, wall)
            ename = _find_edge_on_segment(current.Shape, p1, p2)
            if not ename:
                raise BuildError("cannot find the edge for bend %r (from %s to %s)" % (fname, F.v_round(p1, 2), F.v_round(p2, 2)))
            names.append(ename)
        obj = doc.addObject("Part::FeaturePython", "Bend%d" % (gi + 1))
        SheetMetalCmd.SMBendWall(obj, current, names)
        obj.length = float(g["sm_length"])
        obj.angle = float(g["rho"])
        obj.radius = float(g["radius"])
        obj.invert = bool(g["invert"])
        doc.recompute()
        shp = obj.Shape
        if shp.isNull() or not shp.isValid() or shp.Volume <= current.Shape.Volume * 0.999:
            raise BuildError("bend group %s produced an invalid shape" % (g["faces"],))
        current = obj
    return current.Shape, current


# ---------------------------------------------------------------- features

def _local_tool(inst, t, hole_depth=None):
    """Cutting tool in the face's local frame (x=u, y=v, z=n, material n in [0,t])."""
    f = inst["feature"]
    ft = f["type"]
    u, v = inst["u"], inst["v"]
    h = t + 2 * MARGIN
    z0 = -MARGIN
    if ft in ("hole", "thread", "blind_hole"):
        r = f["diameter"] / 2.0
        depth = f.get("depth")
        if depth and depth < t:
            side = str(f.get("side") or f.get("from_side") or "").lower()
            ref_side = side in ("bottom", "outer", "reference")
            if ref_side:
                return Part.makeCylinder(r, depth + MARGIN, App.Vector(u, v, -MARGIN), App.Vector(0, 0, 1))
            return Part.makeCylinder(r, depth + MARGIN, App.Vector(u, v, t - depth), App.Vector(0, 0, 1))
        return Part.makeCylinder(r, h, App.Vector(u, v, z0), App.Vector(0, 0, 1))
    if ft == "counterbore":
        through = Part.makeCylinder(f["diameter"] / 2.0, h, App.Vector(u, v, z0), App.Vector(0, 0, 1))
        at_ref = f["cb_side"] in ("bottom", "outer", "start", "reference")
        d = min(f["cb_depth"], t - 0.1)
        zb = -MARGIN if at_ref else t - d
        bore = Part.makeCylinder(f["cb_diameter"] / 2.0, d + MARGIN, App.Vector(u, v, zb), App.Vector(0, 0, 1))
        return through.fuse(bore)
    if ft == "hex":
        return makeHexagon(f["circumradius"], h, App.Vector(u, v, z0), App.Vector(0, 0, 1))
    if ft == "countersink":
        side = f["cs_side"]
        at_ref = side in ("bottom", "outer", "start", "reference")
        return makeCountersink(f["diameter"] / 2.0, f["cs_diameter"] / 2.0, f["cs_angle"], t,
                               pnt=App.Vector(u, v, 0.0), dir=App.Vector(0, 0, 1),
                               cs_side="start" if at_ref else "end", buffer=MARGIN)
    if ft in ("slot", "rect"):
        a, b = f["size"]
        if f["orientation"] == "v":
            a, b = b, a
        pnt = App.Vector(u - a / 2.0, v - b / 2.0, z0)
        if ft == "slot":
            return makeOblong(a, b, h, pnt, App.Vector(0, 0, 1))
        return Part.makeBox(a, b, h, pnt, App.Vector(0, 0, 1))
    if ft == "keyhole":
        L, dL, dS = f["size"]
        tool = makeKeyhole(L, dL, dS, h, App.Vector(u - L / 2.0, v - dL / 2.0, z0), App.Vector(0, 0, 1))
        if f["orientation"] == "v":
            tool.rotate(App.Vector(u, v, 0), App.Vector(0, 0, 1), 90)
        return tool
    if ft == "half_moon":
        # half disc: the straight edge faces the `flat` side, the round part lies on the other side of the centre
        r = f["diameter"] / 2.0
        cyl = Part.makeCylinder(r, h, App.Vector(u, v, z0), App.Vector(0, 0, 1))
        pad = r + MARGIN
        boxes = {"v-": (App.Vector(u - pad, v - pad, z0), (2 * pad, pad, h)),
                 "v+": (App.Vector(u - pad, v, z0), (2 * pad, pad, h)),
                 "u-": (App.Vector(u - pad, v - pad, z0), (pad, 2 * pad, h)),
                 "u+": (App.Vector(u, v - pad, z0), (pad, 2 * pad, h))}
        pnt, (a, b, c) = boxes[f["flat"]]
        return cyl.cut(Part.makeBox(a, b, c, pnt))
    raise BuildError("unknown feature type %r" % ft)


def make_tools(ir, plan, sheet=True):
    t = ir["thickness"]
    tools, meta = [], {"threaded_holes": [], "oblongs": [], "holes": [], "rects": [], "bosses": [], "failed": []}
    for inst in plan.instances:
        f = inst["feature"]
        face = plan.faces[inst["face"]]
        if f["type"] in ("boss", "corner_fillet"):
            continue                                  # handled after the cuts
        if f["type"] == "perforation":
            lay = f["layout"]
            meta["perforation"] = {"notation": f["notation"], "shape": f["shape"], "hole_w": f["hole_w"], "hole_l": f["hole_l"],
                                   "pitch_type": f["pitch_type"], "pitch_x": f["pitch_x"], "pitch_y": round(f["pitch_y"], 4),
                                   "count": lay["count"], "pct_theoretical": round(f["pct_theoretical"], 2),
                                   "pct_actual": round(lay["pct_actual"], 2), "margins": [round(lay["margin_x"], 3), round(lay["margin_y"], 3)]}
            continue                                  # cut into the blank face (make_blank)
        if f["type"] == "corner_cut":
            blank = ir["blank"]
            cname = {"x-y-": "front_left", "x+y-": "front_right", "x-y+": "back_left", "x+y+": "back_right"}[f["corner"]]
            tools.append(makeDiagonalCornerCut(blank["x"], blank["y"], t, cname, f["cut_length"], f["cut_width"],
                                               distance_from_corner=f["distance_from_corner"], buffer=MARGIN))
            continue
        if face.kind == "tube_round":
            tool = _round_tube_tool(ir, inst)
        elif face.kind == "rim":
            tool = _rim_tool(ir, plan, inst)
        else:
            local_t = t
            if face.kind == "edge":
                local_t = face.span          # drilled into the plate from its edge face
            if face.kind == "tube" and f.get("through") == "both" and ir.get("tube"):
                # go through the whole section: extend the tool along n
                tube = ir["tube"]
                span = tube["height"] if face.name in ("top", "bottom") else tube["width"]
                local_t = span
            tool = _local_tool(inst, local_t)
            tool = tool.transformShape(_frame_matrix(face)) if hasattr(tool, "transformShape") else tool.transformGeometry(_frame_matrix(face))
        tools.append(tool)
        if face.kind == "tube_round":
            p = None
        elif face.kind == "rim":
            p = _rim_point(face, inst["u"], inst["v"])
        else:
            p = face.point(inst["u"], inst["v"])
        if f["type"] == "thread":
            meta["threaded_holes"].append({"thread_type": f["thread"], "diameter": round(f["diameter"], 3), "pitch": f.get("pitch"),
                                           "face": face.name, "position": {"x": p[0], "y": p[1], "z": p[2]} if p else None,
                                           "direction": {"x": face.n[0], "y": face.n[1], "z": face.n[2]},
                                           "depth": f.get("depth") or t, "u": inst["u"], "v": inst["v"]})
        elif f["type"] == "slot":
            meta["oblongs"].append({"length": f["size"][0], "width": f["size"][1], "face": face.name,
                                    "orientation": f["orientation"],
                                    "center": {"x": p[0], "y": p[1], "z": p[2]} if p else None})
        elif f["type"] in ("hole", "countersink", "counterbore", "blind_hole", "hex"):
            meta["holes"].append({"type": f["type"], "diameter": round(f["diameter"], 3), "face": face.name,
                                  "center": {"x": p[0], "y": p[1], "z": p[2]} if p else None})
        elif f["type"] in ("rect", "keyhole"):
            meta["rects"].append({"type": f["type"], "size": f["size"], "face": face.name})
    return tools, meta


def _rim_point(face, u, v):
    """3D point on a rim wall: u = distance along the circumference from +x, v = height from the bend."""
    cx, cy = face.bend["center"]
    a = u / face.bend["R_edge"]
    p = face.point(0.0, v)
    dx, dy = p[0] - cx, p[1] - cy
    return (cx + dx * math.cos(a) - dy * math.sin(a), cy + dx * math.sin(a) + dy * math.cos(a), p[2])


def _rim_tool(ir, plan, inst):
    """Tool on a rim wall: built at angle 0 in the wall frame, then turned around the disc axis."""
    face = plan.faces[inst["face"]]
    cx, cy = face.bend["center"]
    loc = dict(inst)
    loc["u"] = 0.0
    tool = _local_tool(loc, ir["thickness"]).transformShape(_frame_matrix(face))
    tool.rotate(App.Vector(cx, cy, 0), App.Vector(0, 0, 1), math.degrees(inst["u"] / face.bend["R_edge"]))
    return tool


def _round_tube_tool(ir, inst):
    tube = ir["tube"]
    f = inst["feature"]
    R = tube["diameter"] / 2.0
    a = math.radians(inst["v"])         # angle from +Z toward +X
    u = inst["u"]
    n_hat = App.Vector(-math.sin(a), 0.0, -math.cos(a))      # radially inward
    origin = App.Vector(R * math.sin(a), u, R * math.cos(a))
    u_hat = App.Vector(0, 1, 0)
    v_hat = n_hat.cross(u_hat)
    through = tube["diameter"] if f.get("through") == "both" else R      # to the axis: a big cutter carves the saddle
    fake = F.Face(name="wall", kind="tube", origin=(origin.x, origin.y, origin.z),
                  u=(0.0, 1.0, 0.0), v=(v_hat.x, v_hat.y, v_hat.z), n=(n_hat.x, n_hat.y, n_hat.z), U=tube["length"], V=1.0)
    loc = dict(inst)
    loc["u"], loc["v"] = 0.0, 0.0
    tool = _local_tool(loc, through)
    return tool.transformShape(_frame_matrix(fake))


def fuse_bosses(shape, ir, plan, meta):
    """Welded bushings / standoffs / pins: cylinders ADDED on a face, optional bore through."""
    t = ir["thickness"]
    for inst in plan.instances:
        f = inst["feature"]
        if f["type"] != "boss":
            continue
        face = plan.faces[inst["face"]]
        M = _frame_matrix(face)
        r, h = f["diameter"] / 2.0, f["height"]
        side = str(f.get("side") or ("top" if face.kind == "base" else "inner")).lower()
        far = side in ("top", "inner", "far")
        z0 = t if far else -h
        cyl = Part.makeCylinder(r, h, App.Vector(inst["u"], inst["v"], z0), App.Vector(0, 0, 1)).transformShape(M)
        # a pin standing in a hole of its own size would float: fill the hole through the plate
        hole_r = 0.0
        for hmeta in meta["holes"]:
            c = hmeta.get("center") or {}
            pf = face.point(inst["u"], inst["v"])
            if hmeta.get("face") == face.name and c and abs(c["x"] - pf[0]) < 0.5 and abs(c["y"] - pf[1]) < 0.5 and abs(c["z"] - pf[2]) < 0.5:
                hole_r = max(hole_r, hmeta["diameter"] / 2.0)
        if hole_r >= r - 1e-6:
            fill = Part.makeCylinder(hole_r + 0.05, t + 0.5, App.Vector(inst["u"], inst["v"], 0.0), App.Vector(0, 0, 1)).transformShape(M)   # flush with the far surface, overlaps the pin
            cyl = cyl.fuse(fill)
        fused = shape.fuse(cyl)
        if fused.isValid() and fused.Volume > shape.Volume and len(fused.Solids) == len(shape.Solids):
            shape = fused.removeSplitter()
        else:
            meta["failed"].append("boss")
            continue
        if f.get("inner_diameter"):
            bore = Part.makeCylinder(f["inner_diameter"] / 2.0, t + h + 2 * MARGIN, App.Vector(inst["u"], inst["v"], min(z0, 0.0) - MARGIN), App.Vector(0, 0, 1)).transformShape(M)
            cut = shape.cut(bore)
            if cut.isValid():
                shape = cut
        p = face.point(inst["u"], inst["v"])
        meta["bosses"].append({"diameter": f["diameter"], "inner_diameter": f.get("inner_diameter"), "height": h,
                               "face": face.name, "side": side, "center": {"x": p[0], "y": p[1], "z": p[2]}})
    return shape


def fillet_wall_corners(shape, ir, plan, meta):
    """Round the free-tip corners of a wall (corner_fillet feature on a wall face)."""
    t = ir["thickness"]
    for inst in plan.instances:
        f = inst["feature"]
        if f["type"] != "corner_fillet" or inst["face"] == "base":
            continue
        face = plan.faces[inst["face"]]
        edges = []
        for corner in f.get("wall_corners") or ("u-", "u+"):
            u = 0.0 if corner == "u-" else face.U
            target = V(face.point(u, face.V - face.shrink.get("v+", 0.0), t / 2.0))
            for e in shape.Edges:
                if abs(e.Length - t) > 1e-3 or len(e.Vertexes) != 2:
                    continue
                mid = (e.Vertexes[0].Point + e.Vertexes[1].Point) * 0.5
                if (mid - target).Length < 0.3:
                    edges.append(e)
        if edges:
            try:
                res = shape.makeFillet(float(f["radius"]), edges)
                if res.isValid():
                    shape = res
                else:
                    meta["failed"].append("corner_fillet")
            except Exception:
                meta["failed"].append("corner_fillet")
    return shape


def cut_all(shape, tools):
    if not tools:
        return shape, []
    before = shape.Volume
    try:
        res = shape.cut(Part.makeCompound(tools))
        if res.isValid() and not res.isNull() and res.Volume < before - 1e-6 and len(res.Solids) >= 1:
            return res, []
    except Exception as exc:  # pragma: no cover - OCC failure path
        print("[IR_BUILD] compound cut failed (%s), falling back to per-tool cuts" % exc)
    failed = []
    cur = shape
    for i, tool in enumerate(tools):
        try:
            nxt = cur.cut(tool)
            if nxt.isValid() and not nxt.isNull() and nxt.Volume < cur.Volume - 1e-6:
                cur = nxt
            else:
                failed.append(i)
        except Exception:
            failed.append(i)
    return cur, failed


# ---------------------------------------------------------------- tube

def _end_cut_angles(tube):
    """IR end-cut angle = angle between the cut plane and the tube axis; 90 (or None) = straight, skipped."""
    out = []
    for k in ("start", "end"):
        a = tube["end_cuts"].get(k)
        out.append(0.0 if a is None or a >= 89.5 else float(a))
    return out[0], out[1]


def _bevel_halfspace(R, y_end, angle_deg, sign):
    """Half-space removing the wedge of a bevelled tube end (tube along +Y, section centred on the axis).

    The cut plane contains the bottom line (z = -R) at y_end - the long side keeps the
    full length - and leans away from the tube by `angle_deg` (angle between the plane
    and the tube axis; 90 = straight).  sign=+1 for the start end, -1 for the far end.
    The legacy helper centred a slab on y_end and shortened the tube by R/tan(angle).
    """
    k = math.tan(math.radians(90.0 - angle_deg))          # dy per dz along the plane
    big = 6.0 * R + abs(y_end) + 10.0
    box = Part.makeBox(big, big, big, App.Vector(-big / 2.0, -big, -big / 2.0))   # y in [-big, 0]
    # rotate the box's y=0 face about the X axis so that y = sign * k * (z + R)
    theta = -math.degrees(math.atan(k)) * sign
    box.rotate(App.Vector(0, 0, 0), App.Vector(1, 0, 0), theta)
    if sign < 0:
        box.rotate(App.Vector(0, 0, 0), App.Vector(0, 0, 1), 180.0)           # face now at y >= 0 side
    box.translate(App.Vector(0, y_end + sign * k * R, 0))
    return box


def _tube_tabs(shape, tube, plan):
    """Tenons: the wall sheet continued beyond a tube end, centred on the face.  A tab that
    does not touch the tube (bevelled end) is skipped with a warning instead of floating."""
    L, W, H = tube["length"], tube["width"], tube["height"]
    t = tube["wall"] or min(W, H) / 2.0
    for tb in tube["tabs"]:
        ends = ["start", "end"] if tb["end"] == "both" else [tb["end"]]
        for end in ends:
            y0, y1 = (-tb["protrusion"], 0.0) if end == "start" else (L, L + tb["protrusion"])
            for face in tb["faces"]:
                a = tb["across"]
                if a is None:
                    cr = tube.get("corner_radius")
                    edge = 2.0 * t if cr is None else cr
                    a = (W if face in ("top", "bottom") else H) - 2.0 * edge
                if face in ("top", "bottom"):
                    x0 = (W - a) / 2.0
                    z0 = H - t if face == "top" else 0.0
                    box = Part.makeBox(a, y1 - y0, t, App.Vector(x0, y0, z0))
                else:
                    z0 = (H - a) / 2.0
                    x0 = 0.0 if face == "front" else W - t
                    box = Part.makeBox(t, y1 - y0, a, App.Vector(x0, y0, z0))
                fused = shape.fuse(box)
                if fused.isValid() and len(fused.Solids) == len(shape.Solids):
                    shape = fused.removeSplitter()
                else:
                    plan.warnings.append("tenon on the %s face at the %s end does not touch the tube (bevelled end?) - skipped" % (face, end))
    return shape


def make_tube(ir, plan):
    tube = ir["tube"]
    L = tube["length"]
    if tube["section"] == "rect":
        W, H, t = tube["width"], tube["height"], tube["wall"]
        if tube["solid"]:
            shape = Part.makeBox(W, L, H)
        else:
            cr = tube.get("corner_radius")
            outer = 2.0 * t if cr is None else cr
            inner = max(outer - t, 0.0)
            shape = makeRectangularTube(L, W, H, t, outer_fillet_radius=outer, inner_fillet_radius=inner)
        # helper angle = angle between the cut plane and the tube axis (90 = straight), same as the IR
        a1, a2 = _end_cut_angles(tube)
        if a1 or a2:
            shape = create_square_tube_angled_cuts(shape, L, W, H, left_cut_angle=a1, right_cut_angle=a2)
        shape = _tube_tabs(shape, tube, plan)
    else:
        D, t = tube["diameter"], tube["wall"]
        if tube["solid"]:
            shape = Part.makeCylinder(D / 2.0, L, App.Vector(0, 0, 0), App.Vector(0, 1, 0))
        else:
            shape = makeCircularTube(L, D, t)
        a1, a2 = _end_cut_angles(tube)
        if a1:
            shape = shape.cut(_bevel_halfspace(D / 2.0, 0.0, a1, +1))
        if a2:
            shape = shape.cut(_bevel_halfspace(D / 2.0, L, a2, -1))
    return shape


# ---------------------------------------------------------------- build

def build(ir, doc=None):
    """Build the part.  Returns (shape, normalized_ir, plan, report)."""
    t0 = time.time()
    if doc is None:
        doc = App.ActiveDocument or App.newDocument("IRBuild")
    ir, plan = F.plan_part(ir)
    report = {"label": plan.label, "faces": {n: plan.faces[n].as_dict() for n in plan.order}, "warnings": list(plan.warnings)}
    if ir["family"] == "tube":
        shape = make_tube(ir, plan)
        bend_obj = None
    elif ir["family"] == "profile":
        shape = make_profile(ir)
        bend_obj = None
    else:
        blank = make_blank(ir, plan)
        report["blank_volume"] = round(blank.Volume, 3)
        shape, _fold_obj = apply_folds(doc, blank, ir, plan)
        shape, bend_obj = apply_bends(doc, shape, ir, plan)
        shape = add_manual_walls(shape, ir, plan)
    tools, meta = make_tools(ir, plan)
    shape, failed = cut_all(shape, tools)
    meta["failed"] = failed
    if ir["family"] == "sheet":
        shape = fuse_bosses(shape, ir, plan, meta)
        shape = fillet_wall_corners(shape, ir, plan, meta)
    if failed:
        report["warnings"].append("%d feature tool(s) could not be cut: %s" % (len(failed), failed))
    if len(shape.Solids) > 1:
        # never ship a part in pieces: keep the body, report what was dropped
        solids = sorted(shape.Solids, key=lambda s_: s_.Volume, reverse=True)
        dropped = len(solids) - 1
        shape = solids[0]
        report["warnings"].append("%d detached piece(s) were dropped: a feature was placed off the part" % dropped)
    shape = shape.removeSplitter() if len(shape.Solids) == 1 else shape
    report.update({
        "bbox": _bbox(shape), "volume": round(shape.Volume, 3), "faces_count": len(shape.Faces),
        "solids": len(shape.Solids), "valid": bool(shape.isValid()), "tools": len(tools),
        "bends": [{"name": n, **{k: plan.faces[n].bend[k] for k in ("angle", "radius", "direction")},
                   "length": plan.faces[n].V, "on": plan.faces[n].parent} for n in plan.order[1:]] if ir["family"] == "sheet" else [],
        "metadata": meta, "build_seconds": round(time.time() - t0, 2),
    })
    return shape, ir, plan, report


def export_all(shape, ir, plan, report, sanitized_title, output_dir_abs, material=None, write_obj=True, doc=None):
    import Import
    doc = doc or App.ActiveDocument or App.newDocument("IRBuild")
    os.makedirs(output_dir_abs, exist_ok=True)
    main_object = doc.addObject("Part::Feature", "MainPart")
    main_object.Shape = shape
    main_object.Label = sanitized_title
    doc.recompute()
    material = (material or ir.get("material") or "steel")
    step_filename = os.path.join(output_dir_abs, "%s.step" % sanitized_title)
    Import.export([main_object], step_filename)
    geometry_json = os.path.join(output_dir_abs, "%s_geometry.json" % sanitized_title)
    try:
        export_geometry_to_json(shape, geometry_json, material=material,
                                additional_info={"part_name": sanitized_title, "shape_type": report["label"]})
    except Exception as exc:
        print("[IR_BUILD] geometry export failed: %s" % exc)
    files = {"step": step_filename, "geometry": geometry_json}
    if write_obj:
        import Mesh
        import MeshPart
        obj_filename = os.path.join(output_dir_abs, "%s.obj" % sanitized_title)
        mesh_objects = []
        for i, face in enumerate(shape.Faces):
            try:
                face_mesh = MeshPart.meshFromShape(Shape=face, LinearDeflection=0.5, AngularDeflection=0.523599)
            except Exception:
                continue
            mo = doc.addObject("Mesh::Feature", "Face_%02d" % (i + 1))
            mo.Mesh = face_mesh
            mo.Label = "%s_Face_%02d" % (sanitized_title, i + 1)
            mesh_objects.append(mo)
        doc.recompute()
        Mesh.export(mesh_objects, obj_filename)
        files["obj"] = obj_filename
    meta = dict(report["metadata"])
    meta.update({"shape_type": report["label"], "bending_features": report.get("bends", []),
                 "faces": report["faces"], "source": "build_from_ir"})
    with open(os.path.join(output_dir_abs, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    with open(os.path.join(output_dir_abs, "%s_ir.json" % sanitized_title), "w", encoding="utf-8") as fh:
        json.dump(ir, fh, indent=1)
    files["metadata"] = os.path.join(output_dir_abs, "metadata.json")
    return files


def build_and_export(ir, sanitized_title, output_dir_abs, material=None, write_obj=True):
    doc = App.newDocument("IRBuild")
    shape, nir, plan, report = build(ir, doc)
    if report["metadata"].get("perforation"):
        write_obj = False           # meshing thousands of holes takes minutes; the legacy scripts skipped OBJ too
    files = export_all(shape, nir, plan, report, sanitized_title, output_dir_abs, material=material, write_obj=write_obj, doc=doc)
    report["files"] = files
    print("[IR_BUILD] " + json.dumps({k: report[k] for k in ("label", "bbox", "volume", "faces_count", "solids", "valid", "tools", "build_seconds")}))
    return report


# ---------------------------------------------------------------- CLI (freecadcmd build_from_ir.py with TOLERY_IR_JOB set)

def _run_job_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        job = json.load(fh)
    results = []
    jobs = job if isinstance(job, list) else [job]
    for j in jobs:
        t0 = time.time()
        res = {"title": j.get("title")}
        try:
            rep = build_and_export(j["ir"], j.get("title") or "part", j["output_dir"], material=j.get("material"),
                                   write_obj=bool(j.get("write_obj", False)))
            res.update({"ok": True, "report": rep})
        except Exception as exc:  # report, keep going
            import traceback
            res.update({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "traceback": traceback.format_exc()[-1500:]})
        res["seconds"] = round(time.time() - t0, 2)
        results.append(res)
        try:
            App.closeDocument(App.ActiveDocument.Name)
        except Exception:
            pass
    out = jobs[0].get("report") if jobs and isinstance(jobs[0], dict) and jobs[0].get("report") else path + ".result.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results if isinstance(job, list) else results[0], fh, indent=1)
    print("[IR_BUILD] wrote %s" % out)


if os.environ.get("TOLERY_IR_JOB"):
    _run_job_file(os.environ["TOLERY_IR_JOB"])
