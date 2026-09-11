# -*- coding: utf-8 -*-
"""
ir_frames.py - geometry planning for the part IR, with NO FreeCAD dependency.

The IR (see src/core/ir.py for the schema) describes a part the way a sheet-metal
shop thinks about it: a flat blank, a tree of bends, and features placed on
named faces in each face's own 2D (u, v) coordinates.  This module turns that
description into numbers a builder (or a validator) can use:

  * normalize_ir(ir)  - fills defaults and canonical names, raises IRError on
                        structural problems (unknown face, bend on a missing edge).
  * plan_sheet(ir)    - every face's 3D frame (origin, u, v, n), its outside
                        extents, the material each bend consumes ("shrink"),
                        the SheetMetal parameters of each bend, and the expanded
                        list of feature instances (patterns/mirrors resolved).
  * plan_tube(ir)     - the same for the tube family.

Conventions (the contract the LLM prompt, the validator and the builder share):

  Face frame: point(u, v) = origin + u*U + v*V lies on the face's REFERENCE
  surface; N is the unit normal from that surface INTO the material, so a tool
  extruded from n=-m to n=t+m always cuts through.
    base : reference surface = bottom (z=0); u=+x, v=+y, n=+z.
           edges: "x-" (left), "x+" (right), "y-" (front), "y+" (back).
    wall : created by a bend on an edge of its parent. Reference surface = the
           OUTER (convex) face. u runs along the bend edge, v runs from the
           outside sharp corner toward the free tip, v in [0, length].
           On x-/x+ walls u runs front->back, on y-/y+ walls left->right.
  Outside dimensions everywhere: blank x/y and bend `length` are measured on the
  outside of the finished part (mould-line convention), exactly like the legacy
  helpers (makeLShape etc.), so a 100 mm base with a 40 mm flange spans
  x in [0,100] and z in [0,40].
  Bend `angle` = interior angle between the two legs as a customer says it
  ("plie a 90", "a 120 ouvert"); 0 = hem / crushed fold.  The SheetMetal
  rotation is rho = 180 - angle.
"""
import math
import re

# ---------------------------------------------------------------- tiny vec3

def v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def v_mul(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def v_len(a):
    return math.sqrt(v_dot(a, a))


def v_unit(a):
    n = v_len(a)
    if n < 1e-12:
        raise IRError("zero-length vector")
    return v_mul(a, 1.0 / n)


def v_round(a, nd=6):
    return tuple(0.0 if abs(x) < 1e-9 else round(x, nd) for x in a)


class IRError(ValueError):
    """The IR cannot be turned into geometry.  Message is user-readable."""


# ---------------------------------------------------------------- constants

EDGE_ALIASES = {
    "x-": "x-", "left": "x-", "l": "x-", "west": "x-",
    "x+": "x+", "right": "x+", "r": "x+", "east": "x+",
    "y-": "y-", "front": "y-", "bottom": "y-", "near": "y-", "south": "y-",
    "y+": "y+", "back": "y+", "rear": "y+", "top": "y+", "far": "y+", "north": "y+",
}
# on a wall the child bend can only hang from the tip in v1
WALL_EDGE_ALIASES = {"tip": "tip", "v+": "tip", "free": "tip", "top": "tip", "end": "tip", "far": "tip"}

TRIANGLE_EDGE_ALIASES = {"e0": "e0", "base": "e0", "ab": "e0", "bottom": "e0",
                         "e1": "e1", "hypotenuse": "e1", "bc": "e1", "right": "e1", "slanted": "e1",
                         "e2": "e2", "ca": "e2", "left": "e2"}

ROUND_TYPES = ("hole", "hex", "thread", "countersink", "counterbore", "blind_hole", "boss")
BOX_TYPES = ("slot", "rect", "window", "keyhole")

# ISO metric coarse: nominal -> tap drill (from rules TH_06)
TAP_DRILL = {
    "M1": 0.75, "M1.2": 0.95, "M1.4": 1.1, "M1.5": 1.2, "M1.6": 1.25, "M1.7": 1.35, "M1.8": 1.45,
    "M2": 1.6, "M2.2": 1.75, "M2.5": 2.05, "M3": 2.5, "M3.5": 2.9, "M4": 3.3, "M5": 4.2,
    "M6": 5.0, "M7": 6.0, "M8": 6.8, "M9": 7.8, "M10": 8.5, "M11": 9.5, "M12": 10.2,
    "M14": 12.0, "M16": 14.0, "M18": 15.5, "M20": 17.5, "M22": 19.5, "M24": 21.0,
    "M27": 24.0, "M30": 26.5, "M33": 29.5, "M36": 32.0, "M39": 35.0, "M42": 37.5,
    "M45": 40.5, "M48": 43.0, "M52": 47.0, "M56": 52.0, "M64": 60.0,
}
# fine pitch (TH_07): (nominal, pitch) -> tap drill
TAP_DRILL_FINE = {
    ("M3", 0.35): 2.65, ("M4", 0.5): 3.5, ("M5", 0.5): 4.5, ("M6", 0.75): 5.2, ("M7", 0.75): 6.2,
    ("M8", 0.75): 7.2, ("M8", 1.0): 7.0, ("M9", 1.0): 8.0, ("M10", 0.75): 9.2, ("M10", 1.0): 9.0,
    ("M10", 1.25): 8.8, ("M11", 1.0): 10.0, ("M12", 1.0): 11.0, ("M12", 1.25): 10.8, ("M12", 1.5): 10.5,
    ("M14", 1.0): 13.0, ("M14", 1.25): 12.8, ("M14", 1.5): 12.5, ("M16", 1.0): 15.0, ("M16", 1.5): 14.5,
    ("M18", 1.0): 17.0, ("M18", 1.5): 16.5, ("M18", 2.0): 16.0, ("M20", 1.0): 19.0, ("M20", 1.5): 18.5,
    ("M20", 2.0): 18.0, ("M22", 1.0): 21.0, ("M22", 1.5): 20.5, ("M22", 2.0): 20.0, ("M24", 1.0): 23.0,
    ("M24", 1.5): 22.5, ("M24", 2.0): 22.0, ("M27", 1.5): 25.5, ("M27", 2.0): 25.0, ("M30", 1.5): 28.5,
    ("M30", 2.0): 28.0, ("M36", 1.5): 34.5, ("M36", 2.0): 34.0, ("M36", 3.0): 33.0,
}


def tap_drill_diameter(thread, pitch=None):
    """Drill diameter for an ISO thread designation such as 'M6' or 'M8x1'."""
    if not thread:
        raise IRError("thread designation missing")
    s = str(thread).upper().replace(" ", "")
    if "X" in s and pitch is None:
        s, p = s.split("X", 1)
        try:
            pitch = float(p.replace(",", "."))
        except ValueError:
            pitch = None
    if not s.startswith("M"):
        s = "M" + s
    # normalise "M6.0" -> "M6"
    try:
        nominal = float(s[1:].replace(",", "."))
    except ValueError:
        raise IRError("unknown thread designation %r" % thread)
    key = "M%g" % nominal
    if pitch is not None:
        d = TAP_DRILL_FINE.get((key, float(pitch)))
        if d is not None:
            return d, key, float(pitch)
    d = TAP_DRILL.get(key)
    if d is None:
        # metric approximation: drill = nominal - pitch, coarse pitch ~ 0.15*nominal
        d = round(nominal - max(0.5, 0.15 * nominal), 2)
    return d, key, pitch


# ---------------------------------------------------------------- normalize

def _num(x, name, positive=True, allow_none=False):
    if x is None:
        if allow_none:
            return None
        raise IRError("%s is missing" % name)
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise IRError("%s must be a number, got %r" % (name, x))
    if positive and v <= 0:
        raise IRError("%s must be > 0, got %g" % (name, v))
    return v


_OPPOSITE = {"x-": "u+", "x+": "u-", "y-": "v+", "y+": "v-"}
_SAME = {"x-": "u-", "x+": "u+", "y-": "v-", "y+": "v+"}
_BEND_WORDS = ("bend", "bend_line", "bendline", "bend_edge", "fold", "fold_line", "pli", "root")
_TIP_WORDS = ("tip", "free", "free_edge", "opposite", "opposite_bend", "opposite_edge", "outer_edge", "far_from_bend")


def _norm_axis_ref(ref, axis, face_kind, ctx=None):
    """Map a user-facing edge/reference word to 'u-', 'u+', 'v-', 'v+' or 'center'.

    ctx: {"base_bends": [edges bent on the base], "along": base edge a wall/edge face runs along}
    lets "tip"/"bend" work on the base of a single-bend part and lets base edge names
    (front/back or left/right) address the ends of a wall.
    """
    ctx = ctx or {}
    if ref is None:
        return "center"
    r = str(ref).strip().lower().replace(" ", "_")
    if r in ("center", "centre", "middle", "mid", "centered", "centred"):
        return "center"
    if r in ("u-", "u+", "v-", "v+"):
        return r
    if face_kind == "base":
        m = {"x-": "u-", "left": "u-", "x+": "u+", "right": "u+",
             "y-": "v-", "front": "v-", "bottom": "v-", "y+": "v+", "back": "v+", "rear": "v+", "top": "v+",
             "start": axis + "-", "end": axis + "+", "near": axis + "-", "far": axis + "+"}
        bb = list(ctx.get("base_bends") or [])
        if r in _BEND_WORDS or r in _TIP_WORDS:
            if len(bb) != 1:
                raise IRError("position reference %r is ambiguous on the base plate: name the edge (left/right/front/back)" % ref)
            m[r] = _SAME[bb[0]] if r in _BEND_WORDS else _OPPOSITE[bb[0]]
    else:  # wall or edge face: u runs along the parent edge
        m = {"bend": "v-", "bend_line": "v-", "bendline": "v-", "bend_edge": "v-", "fold": "v-", "base": "v-", "bottom": "v-",
             "root": "v-", "pli": "v-",
             "tip": "v+", "free": "v+", "free_edge": "v+", "top": "v+", "outer_edge": "v+", "opposite": "v+",
             "end": "v+" if axis == "v" else "u+",
             "start": axis + "-", "left": "u-", "right": "u+", "front": "u-", "back": "u+", "rear": "u+",
             "near": axis + "-", "far": axis + "+"}
        along = ctx.get("along")
        if along in ("x-", "x+"):          # wall runs along y: its ends are front/back
            m.update({"y-": "u-", "y+": "u+"})
        elif along in ("y-", "y+"):        # wall runs along x: its ends are left/right
            m.update({"x-": "u-", "x+": "u+"})
    if r in m:
        out = m[r]
        if out[0] != axis and out != "center":
            raise IRError("position reference %r is not an edge of the %s axis" % (ref, axis))
        return out
    raise IRError("unknown position reference %r" % ref)


def normalize_ir(ir):
    """Return a deep-normalised copy of the IR with defaults filled in.

    Raises IRError for anything that can never build.  Missing REQUIRED values
    (thickness, blank size, bend lengths) also raise; the validator asks the
    user for them before we ever get here.
    """
    import copy
    ir = copy.deepcopy(ir or {})
    fam = (ir.get("family") or "sheet").lower()
    ir["family"] = fam
    mat = (ir.get("material") or "steel")
    ir["material"] = str(mat).lower()
    if fam == "tube":
        return _normalize_tube(ir)
    if fam != "sheet":
        raise IRError("unsupported family %r" % fam)

    t = _num(ir.get("thickness"), "thickness")
    ir["thickness"] = t
    ir["bend_radius"] = _num(ir.get("bend_radius"), "bend_radius", allow_none=True) or t

    blank = ir.get("blank") or {}
    btype = (blank.get("type") or "rect").lower()
    blank["type"] = btype
    if btype in ("rect", "rectangle", "plate", "sheet"):
        blank["type"] = "rect"
        blank["x"] = _num(blank.get("x"), "blank.x (length)")
        blank["y"] = _num(blank.get("y"), "blank.y (width)")
    elif btype in ("disc", "disk", "circle", "circular", "round"):
        blank["type"] = "disc"
        blank["diameter"] = _num(blank.get("diameter"), "blank.diameter")
        blank["inner_diameter"] = _num(blank.get("inner_diameter"), "blank.inner_diameter", allow_none=True)
        if blank["inner_diameter"] and blank["inner_diameter"] >= blank["diameter"]:
            raise IRError("inner diameter must be smaller than the outer diameter")
    elif btype in ("ring", "annulus"):
        blank["type"] = "disc"
        blank["diameter"] = _num(blank.get("diameter"), "blank.diameter")
        inner = blank.get("inner_diameter")
        if inner is None and blank.get("band_width") is not None:
            inner = blank["diameter"] - 2.0 * float(blank["band_width"])
        blank["inner_diameter"] = _num(inner, "blank.inner_diameter")
    elif btype in ("stadium", "oblong", "slot"):
        blank["type"] = "stadium"
        blank["x"] = _num(blank.get("x"), "blank.x (total length)")
        blank["y"] = _num(blank.get("y"), "blank.y (width)")
        if blank["y"] > blank["x"]:
            blank["x"], blank["y"] = blank["y"], blank["x"]
    elif btype in ("polygon", "triangle"):
        pts = blank.get("points")
        if not pts and btype == "triangle":
            pts = triangle_points(blank.get("triangle") or blank)
            blank["points"] = pts
        if not pts or len(pts) < 3:
            raise IRError("polygon blank needs at least 3 points")
        blank["points"] = [(float(p[0]), float(p[1])) for p in pts]
        blank["type"] = "polygon"
        if _polygon_area(blank["points"]) < 0:   # force counter-clockwise
            blank["points"].reverse()
    else:
        raise IRError("unknown blank type %r" % btype)
    dims = str(blank.get("dims") or "outside").lower()
    blank["dims"] = "inside" if dims in ("inside", "inner", "interior", "int") else "outside"
    blank["corner_radius"] = _num(blank.get("corner_radius"), "corner_radius", allow_none=True)
    blank["corner_chamfer"] = _num(blank.get("corner_chamfer"), "corner_chamfer", allow_none=True)
    corners = {}
    for k, v in (blank.get("corners") or {}).items():
        kk = _norm_corner(k)
        corners[kk] = v or {}
    blank["corners"] = corners
    ir["blank"] = blank

    # ---- bends
    faces = {"base"}
    bends = []
    for i, b in enumerate(ir.get("bends") or []):
        b = dict(b)
        name = str(b.get("name") or "bend%d" % (i + 1)).strip().lower().replace(" ", "_")
        if name in faces:
            raise IRError("duplicate face name %r" % name)
        parent = str(b.get("on") or "base").strip().lower().replace(" ", "_")
        if parent not in faces:
            raise IRError("bend %r hangs from unknown face %r" % (name, parent))
        edge = str(b.get("edge") or "").strip().lower()
        if parent == "base":
            if blank["type"] == "rect" or blank["type"] == "stadium":
                if edge not in EDGE_ALIASES:
                    raise IRError("bend %r: unknown base edge %r" % (name, b.get("edge")))
                edge = EDGE_ALIASES[edge]
            elif blank["type"] == "polygon":
                n = len(blank["points"])
                if edge in TRIANGLE_EDGE_ALIASES and n == 3:
                    edge = TRIANGLE_EDGE_ALIASES[edge]
                elif edge.startswith("e") and edge[1:].isdigit() and int(edge[1:]) < n:
                    pass
                else:
                    raise IRError("bend %r: unknown polygon edge %r" % (name, b.get("edge")))
            elif blank["type"] == "disc":
                # a fold line across the disc, offset from the centre along x
                if b.get("offset") is None:
                    raise IRError("bend %r on a disc needs 'offset' (fold line distance from centre)" % name)
                edge = "chord"
                b["offset"] = float(b["offset"])
        else:
            if edge in ("", None) or edge in WALL_EDGE_ALIASES:
                edge = "tip"
            else:
                raise IRError("bend %r: a bend on a wall can only hang from its tip edge, got %r" % (name, b.get("edge")))
        b["name"], b["on"], b["edge"] = name, parent, edge
        b["length"] = _num(b.get("length"), "bend %r length" % name)
        ang = b.get("angle")
        ang = 90.0 if ang is None else float(ang)
        if ang < 0 or ang >= 180:
            raise IRError("bend %r: angle must be in [0,180), 0 meaning a hem (got %g)" % (name, ang))
        b["angle"] = ang
        d = str(b.get("direction") or "").lower()
        if parent == "base":
            if d in ("", "up", "upward", "upwards", "+", "top"):
                d = "up"
            elif d in ("down", "downward", "downwards", "-", "bottom"):
                d = "down"
            elif d in ("in", "inside", "inward"):
                d = "up"
            elif d in ("out", "outside", "outward"):
                d = "down"
            else:
                raise IRError("bend %r: unknown direction %r" % (name, b.get("direction")))
        else:
            if d in ("", "in", "inside", "inward", "up", "toward_base", "closed"):
                d = "in"
            elif d in ("out", "outside", "outward", "down", "open"):
                d = "out"
            else:
                raise IRError("bend %r: unknown direction %r" % (name, b.get("direction")))
        b["direction"] = d
        b["radius"] = _num(b.get("radius"), "bend %r radius" % name, allow_none=True) or ir["bend_radius"]
        faces.add(name)
        bends.append(b)
    # one bend per (parent, edge)
    seen = set()
    for b in bends:
        key = (b["on"], b["edge"], b.get("offset"))
        if key in seen:
            raise IRError("two bends on the same edge %r of %r" % (b["edge"], b["on"]))
        seen.add(key)
    ir["bends"] = bends
    # inside footprint -> outside: one wall thickness per bent base edge
    if blank["type"] == "rect" and blank["dims"] == "inside" and not blank.get("_converted"):
        bent = {b["edge"] for b in bends if b["on"] == "base"}
        blank["x_inside"], blank["y_inside"] = blank["x"], blank["y"]
        blank["x"] = blank["x"] + t * len(bent & {"x-", "x+"})
        blank["y"] = blank["y"] + t * len(bent & {"y-", "y+"})
        blank["_converted"] = True

    # ---- features
    base_bends = [b["edge"] for b in bends if b["on"] == "base" and b["edge"] in _SAME]
    along = {}
    for b in bends:
        along[b["name"]] = b["edge"] if b["on"] == "base" else along.get(b["on"])
    feats = []
    for i, f in enumerate(ir.get("features") or []):
        f = dict(f)
        ftype = str(f.get("type") or "hole").strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {"circle": "hole", "circular": "hole", "round_hole": "hole", "drill": "hole", "drilling": "hole",
                   "oblong": "slot", "slotted_hole": "slot", "slot_hole": "slot",
                   "rectangle": "rect", "rectangular": "rect", "rectangular_cutout": "rect", "cutout": "rect",
                   "rect_cutout": "rect", "mortise": "rect", "notch": "rect", "square": "rect",
                   "hexagon": "hex", "hexagonal": "hex", "hex_hole": "hex",
                   "tapped": "thread", "tapped_hole": "thread", "threaded": "thread", "threaded_hole": "thread", "tap": "thread",
                   "countersunk": "countersink", "countersunk_hole": "countersink", "csk": "countersink",
                   "counterbored": "counterbore", "counterbored_hole": "counterbore", "cbore": "counterbore", "lamage": "counterbore",
                   "diagonal_cut": "corner_cut", "diagonal_corner_cut": "corner_cut", "diagonal_hole": "corner_cut",
                   "fillet": "corner_fillet", "corner_radius": "corner_fillet", "chamfer": "corner_chamfer",
                   "engraving": "engrave", "text": "engrave", "marking": "engrave",
                   "bushing": "boss", "bush": "boss", "standoff": "boss", "stand_off": "boss", "pin": "boss", "stud": "boss",
                   "spacer": "boss", "welded_bushing": "boss", "cylinder": "boss", "insert": "boss"}
        ftype = aliases.get(ftype, ftype)
        if ftype == "window":
            ftype = "rect"
        f["type"] = ftype
        face = _resolve_face_name(f.get("face"), faces, blank, bends)
        if face is None:
            raise IRError("feature %d (%s) is on unknown face %r" % (i + 1, ftype, f.get("face")))
        f["face"] = face
        if face.startswith("edge:"):
            f["side"] = "reference"          # drilled from the edge face inward
        if ftype == "corner_fillet" and face != "base":
            f["radius"] = _num(f.get("radius"), "corner radius")
            cs = f.get("corners") or "all"
            if isinstance(cs, str):
                cs = [cs]
            wc = set()
            for c in cs:
                c = str(c).lower().replace(" ", "_")
                if c in ("all", "tip", "free", "both", "free_corners"):
                    wc.update(("u-", "u+"))
                elif c in ("u-", "u-v+", "left", "front", "start"):
                    wc.add("u-")
                elif c in ("u+", "u+v+", "right", "back", "end"):
                    wc.add("u+")
                else:
                    raise IRError("unknown wall corner %r for a corner fillet (use tip / left / right)" % c)
            f["wall_corners"] = sorted(wc)
            f["at"] = None
            f["positions"] = [(0.0, 0.0)]
            feats.append(f)
            continue
        if ftype in ("corner_fillet", "corner_chamfer"):
            # folded into the blank; keep for the validator's information
            val = _num(f.get("radius") if ftype == "corner_fillet" else f.get("size"), "%s size" % ftype)
            cs = f.get("corners") or "all"
            if isinstance(cs, str):
                cs = ["all"] if cs.lower() == "all" else [cs]
            for c in cs:
                if str(c).lower() == "all":
                    for cc in ("x-y-", "x+y-", "x-y+", "x+y+"):
                        blank["corners"].setdefault(cc, {})[("radius" if ftype == "corner_fillet" else "chamfer")] = val
                else:
                    blank["corners"].setdefault(_norm_corner(c), {})[("radius" if ftype == "corner_fillet" else "chamfer")] = val
            continue
        if ftype == "engrave":
            # engraving is not modelled in the solid; keep as a note
            f["note_only"] = True
            feats.append(f)
            continue
        if ftype == "corner_cut":
            if face != "base" or blank["type"] != "rect":
                raise IRError("a diagonal corner cut is only defined on a rectangular base")
            f["corner"] = _norm_corner(f.get("corner") or "x-y-")
            f["cut_length"] = _num(f.get("cut_length") or f.get("length"), "corner_cut cut_length")
            f["cut_width"] = _num(f.get("cut_width") or f.get("width"), "corner_cut cut_width")
            f["distance_from_corner"] = _num(f.get("distance_from_corner"), "distance_from_corner", positive=False, allow_none=True) or 0.0
            feats.append(f)
            continue
        # sizes
        if ftype in ("hole", "blind_hole"):
            f["diameter"] = _num(f.get("diameter"), "hole diameter")
        elif ftype == "thread":
            drill, key, pitch = tap_drill_diameter(f.get("thread") or f.get("size") or f.get("designation"), f.get("pitch"))
            f["thread"], f["pitch"], f["diameter"] = key, pitch, drill
        elif ftype == "countersink":
            f["diameter"] = _num(f.get("diameter") or f.get("hole_diameter"), "countersink hole diameter")
            f["cs_diameter"] = _num(f.get("cs_diameter") or f.get("countersink_diameter"), "countersink diameter")
            if f["cs_diameter"] <= f["diameter"]:
                raise IRError("countersink diameter must exceed the hole diameter")
            f["cs_angle"] = _num(f.get("cs_angle"), "cs_angle", allow_none=True) or 90.0
            side = str(f.get("cs_side") or "").lower()
            f["cs_side"] = side or ("top" if face == "base" else "outer")
        elif ftype == "counterbore":
            f["diameter"] = _num(f.get("diameter") or f.get("hole_diameter"), "counterbore hole diameter")
            f["cb_diameter"] = _num(f.get("cb_diameter") or f.get("counterbore_diameter") or f.get("cs_diameter"), "counterbore diameter")
            f["cb_depth"] = _num(f.get("cb_depth") or f.get("counterbore_depth") or f.get("depth"), "counterbore depth")
            if f["cb_diameter"] <= f["diameter"]:
                raise IRError("counterbore diameter must exceed the hole diameter")
            side = str(f.get("cb_side") or f.get("cs_side") or f.get("side") or "").lower()
            f["cb_side"] = side or ("top" if face == "base" else "outer")
            f["depth"] = None
        elif ftype == "hex":
            af = f.get("across_flats") or f.get("size")
            dia = f.get("diameter")
            if af is not None:
                af = _num(af, "hex across_flats")
                f["circumradius"] = af / math.sqrt(3.0)
            elif dia is not None:
                f["circumradius"] = _num(dia, "hex diameter") / 2.0
            else:
                raise IRError("hex feature needs across_flats or diameter")
            f["diameter"] = 2.0 * f["circumradius"]
        elif ftype in ("slot", "rect"):
            size = f.get("size")
            if not size or len(size) < 2:
                raise IRError("%s feature needs size [long, short]" % ftype)
            a, b = _num(size[0], "%s size" % ftype), _num(size[1], "%s size" % ftype)
            f["size"] = [max(a, b), min(a, b)]
            f["orientation"] = "v" if str(f.get("orientation") or "u").lower() in ("v", "y", "width", "across") else "u"
        elif ftype == "keyhole":
            size = f.get("size")
            if not size or len(size) < 3:
                raise IRError("keyhole needs size [total_length, large_diameter, small_diameter]")
            f["size"] = [_num(size[0], "keyhole length"), _num(size[1], "keyhole large diameter"), _num(size[2], "keyhole small diameter")]
            f["orientation"] = "v" if str(f.get("orientation") or "u").lower() in ("v", "y", "width", "across") else "u"
        elif ftype == "boss":
            f["diameter"] = _num(f.get("diameter") or f.get("outer_diameter"), "boss outer diameter")
            f["height"] = _num(f.get("height") or f.get("length"), "boss height")
            f["inner_diameter"] = _num(f.get("inner_diameter") or f.get("hole_diameter"), "boss inner diameter", allow_none=True)
            if f["inner_diameter"] and f["inner_diameter"] >= f["diameter"]:
                raise IRError("boss inner diameter must be smaller than its outer diameter")
        else:
            raise IRError("unknown feature type %r" % f.get("type"))
        if f.get("depth") is not None:
            f["depth"] = _num(f["depth"], "depth")
        f["through"] = str(f.get("through") or "one").lower()
        # placement
        kind = "base" if face == "base" else "wall"
        if face.startswith("edge:"):
            along[face] = face[5:]
        pos = f.get("positions")
        if pos:
            f["positions"] = [(float(p[0]) if isinstance(p, (list, tuple)) else float(p["u"]),
                               float(p[1]) if isinstance(p, (list, tuple)) else float(p["v"])) for p in pos]
            f["at"] = None
        else:
            at = dict(f.get("at") or {})
            ctx = {"base_bends": base_bends, "along": along.get(face)}
            try:
                nat = _norm_at(at, kind, ftype, ctx)
            except IRError as first_err:
                # the LLM often puts an edge word in the wrong slot ("tip" for u on a wall):
                # if swapping the two axis specs resolves both, accept the swap
                try:
                    nat = _norm_at({"u": at.get("v"), "v": at.get("u")}, kind, ftype, ctx)
                    f["_swapped_axes"] = True
                except IRError:
                    raise first_err
            f["at"] = nat
        pat = f.get("pattern")
        if pat:
            pat = dict(pat)
            ptype = str(pat.get("type") or ("polar" if pat.get("circle_diameter") else ("grid" if isinstance(pat.get("count"), (list, tuple)) else "linear"))).lower()
            pat["type"] = ptype
            if ptype == "linear":
                pat["count"] = int(pat.get("count") or 1)
                pat["axis"] = "v" if str(pat.get("axis") or "u").lower() in ("v", "y", "width") else "u"
                pitch = pat.get("pitch") if pat.get("pitch") is not None else pat.get("spacing")
                if pat["count"] > 1 and (pitch in (None, "even", "auto") or pat.get("even")):
                    pat["even"] = True          # resolved from the face extent in expand_positions
                    pat["pitch"] = None
                else:
                    pat["pitch"] = _num(pitch, "the spacing (pitch) between the %d repeated features" % pat["count"]) if pat["count"] > 1 else 0.0
            elif ptype == "grid":
                c = pat.get("count") or [1, 1]
                p = pat.get("pitch") or pat.get("spacing") or [None, None]
                if not isinstance(c, (list, tuple)):
                    c = [c, 1]
                if not isinstance(p, (list, tuple)):
                    p = [p, p]
                pat["count"] = [int(c[0] or 1), int(c[1] or 1)]
                pitches, even = [], [False, False]
                for k in (0, 1):
                    if pat["count"][k] <= 1:
                        pitches.append(0.0)
                    elif p[k] in (None, "even", "auto"):
                        pitches.append(None)
                        even[k] = True
                    else:
                        pitches.append(_num(p[k], "grid pitch"))
                pat["pitch"] = pitches
                pat["even"] = even
            elif ptype == "polar":
                pat["count"] = int(pat.get("count") or 1)
                pat["circle_diameter"] = _num(pat.get("circle_diameter") or pat.get("diameter"), "polar circle diameter")
                pat["start_angle"] = float(pat.get("start_angle") or 0.0)
            else:
                raise IRError("unknown pattern type %r" % ptype)
            f["pattern"] = pat
        mir = f.get("mirror") or []
        if isinstance(mir, str):
            mir = [mir]
        f["mirror"] = [m.lower() for m in mir if m and m.lower() in ("u", "v")]
        _fix_first_instance_anchor(f)
        feats.append(f)
    ir["features"] = feats
    return ir


def _resolve_face_name(raw, faces, blank=None, bends=None):
    """Tolerant face lookup: 'left wall' -> 'left', 'base plate' -> 'base'; on a flat
    plate every face word means the base."""
    face = str(raw or "base").strip().lower().replace(" ", "_")
    if face in faces:
        return face
    for suffix in ("_wall", "_flange", "_leg", "_face", "_plate", "_side", "_return", "_tab", "_wing", "_lip"):
        if face.endswith(suffix) and face[: -len(suffix)] in faces:
            return face[: -len(suffix)]
    if face in ("base_plate", "plate", "sheet", "main", "body", "top", "top_face", "bottom", "bottom_face", "flat", "web", "back_plate"):
        return "base"
    # thickness (edge) faces of a rectangular plate: "edge:x-", "left_edge", "edge-left"...
    m = re.match(r"^(?:edge[:_-]?)?(x-|x\+|y-|y\+|left|right|front|back|rear)(?:[_-]?(?:edge|face|side))?$", face)
    if m and blank is not None and blank.get("type") == "rect":
        ek = EDGE_ALIASES[m.group(1)]
        bent = any(b.get("on", "base") == "base" and b.get("edge") == ek for b in (bends or []))
        if not bent:
            return "edge:" + ek
    # a wall named "wall" when there is exactly one bend
    walls = [x for x in faces if x != "base"]
    if len(walls) == 1 and face in ("wall", "flange", "leg", "vertical", "vertical_wall", "vertical_leg", "return", "tab", "lip", "upstand", "aile"):
        return walls[0]
    if not walls and not m:
        return "base"
    return None


def _norm_at(at, kind, ftype, ctx):
    nat = {}
    for axis in ("u", "v"):
        spec = at.get(axis)
        if spec is None:
            nat[axis] = {"from": "center", "dist": 0.0, "of": "center", "defaulted": True}
            continue
        if isinstance(spec, (int, float)):
            nat[axis] = {"from": axis + "-", "dist": float(spec), "of": "center", "defaulted": False}
            continue
        if isinstance(spec, str):
            spec = {"from": spec}
        ref = _norm_axis_ref(spec.get("from"), axis, kind, ctx)
        dist = spec.get("dist", spec.get("distance", 0.0))
        try:
            dist = float(dist) if dist is not None else 0.0
        except (TypeError, ValueError):
            raise IRError("position distance %r is not a number" % (dist,))
        of = str(spec.get("of") or "").lower()
        if of not in ("center", "edge"):
            of = "center" if ftype in ROUND_TYPES else "edge"
        nat[axis] = {"from": ref, "dist": dist, "of": of, "defaulted": False}
    return nat


def _fix_first_instance_anchor(f):
    """The LLM sometimes anchors a centred group at its FIRST instance (dist = -(n-1)*p/2).
    That offset is only ever produced that way, so read it as 'group centred'."""
    pat = f.get("pattern")
    at = f.get("at")
    if not pat or not at:
        return
    axes = []
    if pat["type"] == "linear" and pat.get("pitch"):
        axes = [(pat["axis"], pat["count"], pat["pitch"])]
    elif pat["type"] == "grid":
        axes = [("u", pat["count"][0], pat["pitch"][0]), ("v", pat["count"][1], pat["pitch"][1])]
    for axis, n, p in axes:
        spec = at.get(axis)
        if spec and spec["from"] == "center" and n and p and abs(spec["dist"] + (n - 1) * p / 2.0) < 1e-6:
            spec["dist"] = 0.0
            f["_anchor_fixed"] = True


def _norm_corner(c):
    c = str(c).strip().lower().replace(" ", "_").replace("u-", "x-").replace("u+", "x+").replace("v-", "y-").replace("v+", "y+")
    m = {"x-y-": "x-y-", "front_left": "x-y-", "bottom_left": "x-y-", "left_front": "x-y-", "left_bottom": "x-y-",
         "x+y-": "x+y-", "front_right": "x+y-", "bottom_right": "x+y-", "right_front": "x+y-", "right_bottom": "x+y-",
         "x-y+": "x-y+", "back_left": "x-y+", "top_left": "x-y+", "left_back": "x-y+", "left_top": "x-y+",
         "x+y+": "x+y+", "back_right": "x+y+", "top_right": "x+y+", "right_back": "x+y+", "right_top": "x+y+"}
    if c not in m:
        raise IRError("unknown corner %r" % c)
    return m[c]


def _polygon_area(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def triangle_points(spec):
    """Same conventions as FreeCadUtil.PlateFunction triangle helpers (A at origin, A-B along +x)."""
    kind = str(spec.get("kind") or spec.get("triangle_type") or "").lower().replace("-", "_")
    g = lambda *ks: next((float(spec[k]) for k in ks if spec.get(k) is not None), None)
    if kind == "equilateral":
        s = g("side", "side_length")
        if s is None:
            raise IRError("equilateral triangle needs side")
        return [(0.0, 0.0), (s, 0.0), (s / 2.0, math.sqrt(3.0) * s / 2.0)]
    if kind == "isosceles":
        base, side = g("base", "base_length"), g("equal_side", "side", "equal_side_length")
        if base is None or side is None:
            raise IRError("isosceles triangle needs base and equal_side")
        if 2 * side <= base:
            raise IRError("isosceles triangle: equal sides too short for the base")
        h = math.sqrt(side * side - (base / 2.0) ** 2)
        return [(0.0, 0.0), (base, 0.0), (base / 2.0, h)]
    if kind in ("right", "right_angle", "right_angled"):
        legs = spec.get("legs")
        if legs and len(legs) == 2:
            a, b = float(legs[0]), float(legs[1])
        else:
            hyp, leg = g("hypotenuse", "hypotenuse_length"), g("leg", "leg_length")
            if hyp is None or leg is None or hyp <= leg:
                raise IRError("right triangle needs two legs, or hypotenuse + one leg")
            a, b = leg, math.sqrt(hyp * hyp - leg * leg)
        return [(0.0, 0.0), (a, 0.0), (0.0, b)]
    if kind in ("right_isosceles", "isosceles_right"):
        leg = g("leg", "leg_length", "side")
        if leg is None:
            hyp = g("hypotenuse", "hypotenuse_length")
            if hyp is None:
                raise IRError("right-isosceles triangle needs leg or hypotenuse")
            leg = hyp / math.sqrt(2.0)
        return [(0.0, 0.0), (leg, 0.0), (0.0, leg)]
    if kind == "scalene":
        sides = spec.get("sides")
        if not sides or len(sides) != 3:
            raise IRError("scalene triangle needs sides [base, side_from_A, side_from_B]")
        base, sa, sb = (float(x) for x in sides)
        if not (base < sa + sb and sa < base + sb and sb < base + sa):
            raise IRError("triangle inequality violated")
        cx = (sa * sa - sb * sb + base * base) / (2.0 * base)
        cy = math.sqrt(max(sa * sa - cx * cx, 0.0))
        return [(0.0, 0.0), (base, 0.0), (cx, cy)]
    raise IRError("unknown triangle kind %r" % kind)


# ---------------------------------------------------------------- bend math

def bend_setback(angle_interior, radius, t):
    """(rho_deg, lam, s_corner) for a bend of interior angle `angle_interior`.

    rho    : SheetMetal rotation from flat (0 flat, 90 square, 180 hem).
    lam    : signed distance (<=0) from the end of the arc to the outside sharp
             corner measured along the wall; SMBendWall.length = outside_len + lam.
    s_corner: material of the PARENT's outside dimension consumed by the bend
             (the flat parent ends s_corner before the outside corner).
    """
    rho = 180.0 - float(angle_interior)
    if rho >= 175.0:            # hem / crushed fold: the parent gives up r+t so its outside height is kept
        return 180.0, 0.0, radius + t
    if rho < 5.0:
        raise IRError("bend angle %g is too flat to be a bend" % angle_interior)
    r = math.radians(rho)
    Rm = radius + t / 2.0
    lam = (-t / 2.0 - Rm * (1.0 - math.cos(r)) + (t / 2.0) * math.cos(r)) / math.sin(r)
    s_c = (Rm + t / 2.0) * math.sin(r) + lam * math.cos(r)
    return rho, lam, s_c


# ---------------------------------------------------------------- planning

class Face(object):
    __slots__ = ("name", "kind", "parent", "origin", "u", "v", "n", "U", "V", "shrink", "bend",
                 "poly", "edge_of_parent", "u_start", "children", "span")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))
        if self.shrink is None:
            self.shrink = {"u-": 0.0, "u+": 0.0, "v-": 0.0, "v+": 0.0}
        if self.children is None:
            self.children = []

    def point(self, u, v, n=0.0):
        p = v_add(self.origin, v_add(v_mul(self.u, u), v_mul(self.v, v)))
        if n:
            p = v_add(p, v_mul(self.n, n))
        return p

    def as_dict(self):
        return {"name": self.name, "kind": self.kind, "parent": self.parent,
                "origin": v_round(self.origin), "u": v_round(self.u), "v": v_round(self.v), "n": v_round(self.n),
                "U": round(self.U, 4), "V": round(self.V, 4), "shrink": dict(self.shrink),
                "bend": self.bend, "edge_of_parent": self.edge_of_parent}


class Plan(object):
    def __init__(self):
        self.faces = {}        # name -> Face
        self.order = []        # face names in build order (base first)
        self.bend_groups = []  # list of dicts: parent, faces[], rho, invert, radius, sm_length
        self.instances = []    # feature instances: dict(face, feature, u, v, hu, hv)
        self.label = "Sheet"
        self.warnings = []


def plan_sheet(ir):
    ir = normalize_ir(ir)
    t = ir["thickness"]
    blank = ir["blank"]
    plan = Plan()

    # ---- base face
    if blank["type"] in ("rect", "stadium"):
        X, Y = blank["x"], blank["y"]
    elif blank["type"] == "disc":
        X = Y = blank["diameter"]
    else:
        xs = [p[0] for p in blank["points"]]
        ys = [p[1] for p in blank["points"]]
        X, Y = max(xs) - min(xs), max(ys) - min(ys)
        # shift polygon so its bbox starts at (0,0)
        dx, dy = -min(xs), -min(ys)
        blank["points"] = [(p[0] + dx, p[1] + dy) for p in blank["points"]]
    base = Face(name="base", kind="base", parent=None, origin=(0.0, 0.0, 0.0),
                u=(1.0, 0.0, 0.0), v=(0.0, 1.0, 0.0), n=(0.0, 0.0, 1.0), U=X, V=Y,
                poly=list(blank["points"]) if blank["type"] == "polygon" else None)
    plan.faces["base"] = base
    plan.order.append("base")

    # ---- bends: base bends first (they define shrinks), then walls in order
    by_parent = {}
    for b in ir["bends"]:
        by_parent.setdefault(b["on"], []).append(b)
    for b in ir["bends"]:
        if b["on"] != "base" and b["on"] not in plan.faces:
            # parents are always defined earlier because normalize enforced the order
            pass

    # setbacks for every bend
    for b in ir["bends"]:
        rho, lam, s_c = bend_setback(b["angle"], b["radius"], t)
        b["rho"], b["lam"], b["s_corner"] = rho, lam, s_c

    # shrink of base per edge
    for b in by_parent.get("base", []):
        if blank["type"] in ("rect", "stadium"):
            side = {"x-": "u-", "x+": "u+", "y-": "v-", "y+": "v+"}[b["edge"]]
            base.shrink[side] = b["s_corner"]
        elif blank["type"] == "polygon":
            base.shrink[b["edge"]] = b["s_corner"]
        elif blank["type"] == "disc":
            raise IRError("bends on a circular blank are not supported yet - describe the part as a rectangular blank, "
                          "or ask for the flat disc without bends")
    if blank["type"] in ("rect", "stadium"):
        if base.shrink["u-"] + base.shrink["u+"] >= X or base.shrink["v-"] + base.shrink["v+"] >= Y:
            raise IRError("the bends consume the whole base plate - base dimensions are too small for the bend radius/thickness")

    # walls (breadth-first: parents before children by construction of normalize order)
    for b in ir["bends"]:
        parent = plan.faces[b["on"]]
        d = 1.0 if b["direction"] in ("up", "in") else -1.0
        rho = b["rho"]
        rr = math.radians(min(rho, 180.0))
        if parent.kind == "base":
            if blank["type"] in ("rect", "stadium"):
                if b["edge"] == "x-":
                    o_hat, e_hat = v_mul(base.u, -1.0), base.v
                    corner_uv = (0.0, parent.shrink["v-"])           # start of the wall along the edge
                    U_w = Y - parent.shrink["v-"] - parent.shrink["v+"]
                elif b["edge"] == "x+":
                    o_hat, e_hat = base.u, base.v
                    corner_uv = (X, parent.shrink["v-"])
                    U_w = Y - parent.shrink["v-"] - parent.shrink["v+"]
                elif b["edge"] == "y-":
                    o_hat, e_hat = v_mul(base.v, -1.0), base.u
                    corner_uv = (parent.shrink["u-"], 0.0)
                    U_w = X - parent.shrink["u-"] - parent.shrink["u+"]
                else:  # y+
                    o_hat, e_hat = base.v, base.u
                    corner_uv = (parent.shrink["u-"], Y)
                    U_w = X - parent.shrink["u-"] - parent.shrink["u+"]
                edge_of_parent = b["edge"]
                u_start = 0.0
            else:  # polygon edge e<i>
                pts = base.poly
                i = int(b["edge"][1:])
                p1, p2 = pts[i], pts[(i + 1) % len(pts)]
                e2 = (p2[0] - p1[0], p2[1] - p1[1])
                L_e = math.hypot(*e2)
                e_hat = (e2[0] / L_e, e2[1] / L_e, 0.0)
                o_hat = (e_hat[1], -e_hat[0], 0.0)     # outward normal of a CCW polygon
                # the shrunk polygon edge: neighbours' shrinks trim the ends
                sp = shrink_polygon(pts, {k: v for k, v in base.shrink.items() if k.startswith("e")})
                q1, q2 = sp[i], sp[(i + 1) % len(sp)]
                # wall starts where the shrunk edge starts, projected back to the outside line
                s_along = (q1[0] - p1[0]) * e_hat[0] + (q1[1] - p1[1]) * e_hat[1]
                corner_uv = (p1[0] + e_hat[0] * s_along, p1[1] + e_hat[1] * s_along)
                U_w = math.hypot(q2[0] - q1[0], q2[1] - q1[1])
                edge_of_parent = b["edge"]
                u_start = 0.0
            outer_offset = 0.0 if d > 0 else t          # outer surface of the base for this bend
            origin = base.point(corner_uv[0], corner_uv[1], outer_offset)
            n_parent = v_mul(base.n, d)
        else:
            # child of a wall: hangs from the tip (v = parent.V), same u range
            o_hat, e_hat = parent.v, parent.u
            n_parent = v_mul(parent.n, d)
            outer_offset = 0.0 if d > 0 else t
            origin = parent.point(0.0, parent.V, outer_offset)
            U_w = parent.U
            edge_of_parent = "tip"
            u_start = 0.0
            parent.shrink["v+"] = b["s_corner"]
        # wall direction / inward normal; for a hem (rho=180) this folds back over the parent
        v_hat = v_unit(v_add(v_mul(o_hat, math.cos(rr)), v_mul(n_parent, math.sin(rr))))
        n_hat = v_unit(v_add(v_mul(o_hat, -math.sin(rr)), v_mul(n_parent, math.cos(rr))))
        wall = Face(name=b["name"], kind="wall", parent=parent.name, origin=origin, u=e_hat, v=v_hat, n=n_hat,
                    U=U_w, V=b["length"], edge_of_parent=edge_of_parent, u_start=u_start,
                    bend={"angle": b["angle"], "rho": rho, "radius": b["radius"], "direction": b["direction"],
                          "lam": b["lam"], "s_corner": b["s_corner"], "invert": d < 0})
        wall.shrink["v-"] = b["s_corner"]     # nominal: bend zone at the root
        parent.children.append(b["name"])
        plan.faces[b["name"]] = wall
        plan.order.append(b["name"])

    # SMBendWall length per wall = outside length + lam - tip shrink (child bend)
    for name in plan.order[1:]:
        w = plan.faces[name]
        w.bend["sm_length"] = w.V + w.bend["lam"] - w.shrink["v+"]
        if w.bend["sm_length"] <= 0.5:
            raise IRError("flange %r is too short (%g mm) for its bend radius/thickness" % (name, w.V))

    # bend groups: one SMBendWall call per (parent, rho, invert, radius, sm_length)
    groups = {}
    for name in plan.order[1:]:
        w = plan.faces[name]
        key = (w.parent, round(w.bend["rho"], 3), w.bend["invert"], round(w.bend["radius"], 3), round(w.bend["sm_length"], 3))
        groups.setdefault(key, []).append(name)
    # keep parent order: base groups first, then by the order of the first wall
    ordered = sorted(groups.items(), key=lambda kv: plan.order.index(kv[1][0]))
    for key, names in ordered:
        plan.bend_groups.append({"parent": key[0], "faces": names, "rho": key[1], "invert": key[2],
                                 "radius": key[3], "sm_length": key[4]})

    # ---- edge (thickness) faces of a rectangular plate, created on demand
    if blank["type"] == "rect":
        X, Y = blank["x"], blank["y"]
        edge_faces = {
            "edge:x-": dict(origin=(0.0, 0.0, 0.0), u=(0.0, 1.0, 0.0), v=(0.0, 0.0, 1.0), n=(1.0, 0.0, 0.0), U=Y, V=t, span=X),
            "edge:x+": dict(origin=(X, 0.0, 0.0), u=(0.0, 1.0, 0.0), v=(0.0, 0.0, 1.0), n=(-1.0, 0.0, 0.0), U=Y, V=t, span=X),
            "edge:y-": dict(origin=(0.0, 0.0, 0.0), u=(1.0, 0.0, 0.0), v=(0.0, 0.0, 1.0), n=(0.0, 1.0, 0.0), U=X, V=t, span=Y),
            "edge:y+": dict(origin=(0.0, Y, 0.0), u=(1.0, 0.0, 0.0), v=(0.0, 0.0, 1.0), n=(0.0, -1.0, 0.0), U=X, V=t, span=Y),
        }
        for f in ir["features"]:
            fn = f.get("face", "")
            if fn in edge_faces and fn not in plan.faces:
                plan.faces[fn] = Face(name=fn, kind="edge", parent="base", **edge_faces[fn])

    # ---- feature instances
    for f in ir["features"]:
        if f.get("note_only"):
            continue
        if f["type"] == "corner_cut":
            plan.instances.append({"face": "base", "feature": f, "u": None, "v": None, "hu": None, "hv": None})
            continue
        face = plan.faces[f["face"]]
        hu, hv = feature_half_extents(f)
        for (u, v) in expand_positions(f, face.U, face.V, hu, hv):
            plan.instances.append({"face": face.name, "feature": f, "u": u, "v": v, "hu": hu, "hv": hv})

    plan.label = derive_label(ir, plan)
    return ir, plan


def shrink_polygon(pts, shrinks):
    """Move edge e<i> inward by shrinks['e<i>'] and re-intersect with neighbours."""
    n = len(pts)
    lines = []
    for i in range(n):
        p1, p2 = pts[i], pts[(i + 1) % n]
        e = (p2[0] - p1[0], p2[1] - p1[1])
        L = math.hypot(*e)
        eh = (e[0] / L, e[1] / L)
        oh = (eh[1], -eh[0])
        s = float(shrinks.get("e%d" % i, 0.0) or 0.0)
        q = (p1[0] - oh[0] * s, p1[1] - oh[1] * s)
        lines.append((q, eh))
    out = []
    for i in range(n):
        (q1, d1) = lines[(i - 1) % n]
        (q2, d2) = lines[i]
        det = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(det) < 1e-12:
            raise IRError("degenerate polygon while shrinking bent edges")
        # q1 + a d1 = q2 + b d2
        rx, ry = q2[0] - q1[0], q2[1] - q1[1]
        a = (rx * d2[1] - ry * d2[0]) / det
        out.append((q1[0] + a * d1[0], q1[1] + a * d1[1]))
    return out


def feature_half_extents(f):
    ft = f["type"]
    if ft in ("hole", "blind_hole", "thread", "boss"):
        r = f["diameter"] / 2.0
        return r, r
    if ft == "countersink":
        r = f["cs_diameter"] / 2.0
        return r, r
    if ft == "counterbore":
        r = f["cb_diameter"] / 2.0
        return r, r
    if ft == "hex":
        r = f["circumradius"]
        return r, r
    if ft in ("slot", "rect"):
        a, b = f["size"][0] / 2.0, f["size"][1] / 2.0
        return (a, b) if f["orientation"] == "u" else (b, a)
    if ft == "keyhole":
        a, b = f["size"][0] / 2.0, f["size"][1] / 2.0
        return (a, b) if f["orientation"] == "u" else (b, a)
    return 0.0, 0.0


def _anchor(spec, extent, half):
    ref, dist, of = spec["from"], spec["dist"], spec["of"]
    if ref == "center":
        return extent / 2.0 + dist, 0
    if ref.endswith("-"):
        return (dist + (half if of == "edge" else 0.0)), +1
    return (extent - dist - (half if of == "edge" else 0.0)), -1


def fit_pattern_axis(f, U, V):
    """A linear pattern whose span does not fit its axis but fits the other one is flipped.
    "Two holes 150 mm apart" cannot run across a 50 mm flange: the LLM picked the wrong axis."""
    pat = f.get("pattern")
    if not pat or pat["type"] != "linear" or pat.get("even") or not pat.get("pitch") or pat["count"] < 2:
        return
    span = (pat["count"] - 1) * pat["pitch"]
    ext = {"u": U, "v": V}
    other = "v" if pat["axis"] == "u" else "u"
    if span > ext[pat["axis"]] + 1e-6 and span <= ext[other] + 1e-6:
        pat["axis"] = other
        f["_axis_flipped"] = True


def expand_positions(f, U, V, hu, hv):
    """All feature centres (u, v) on the face after pattern + mirror expansion."""
    fit_pattern_axis(f, U, V)
    if f.get("positions"):
        pts = list(f["positions"])
    else:
        at = f["at"]
        uc, su = _anchor(at["u"], U, hu)
        vc, sv = _anchor(at["v"], V, hv)
        pat = f.get("pattern")
        pts = [(uc, vc)]
        if pat:
            if pat["type"] == "linear":
                n, p = pat["count"], pat["pitch"]
                if pat.get("even") or p is None:
                    extent = U if pat["axis"] == "u" else V
                    p = extent / (n + 1.0)
                    if pat["axis"] == "u":
                        uc, su = extent / 2.0, 0
                    else:
                        vc, sv = extent / 2.0, 0
                pts = []
                for i in range(n):
                    if pat["axis"] == "u":
                        off = (i - (n - 1) / 2.0) * p if su == 0 else su * i * p
                        pts.append((uc + off, vc))
                    else:
                        off = (i - (n - 1) / 2.0) * p if sv == 0 else sv * i * p
                        pts.append((uc, vc + off))
            elif pat["type"] == "grid":
                nu, nv = pat["count"]
                pu, pv = pat["pitch"]
                ev = pat.get("even") or [False, False]
                if ev[0] or pu is None:
                    pu, uc, su = U / (nu + 1.0), U / 2.0, 0
                if ev[1] or pv is None:
                    pv, vc, sv = V / (nv + 1.0), V / 2.0, 0
                pts = []
                for i in range(nu):
                    ou = (i - (nu - 1) / 2.0) * pu if su == 0 else su * i * pu
                    for j in range(nv):
                        ov = (j - (nv - 1) / 2.0) * pv if sv == 0 else sv * j * pv
                        pts.append((uc + ou, vc + ov))
            elif pat["type"] == "polar":
                n = pat["count"]
                R = pat["circle_diameter"] / 2.0
                a0 = math.radians(pat["start_angle"])
                pts = [(uc + R * math.cos(a0 + 2 * math.pi * i / n), vc + R * math.sin(a0 + 2 * math.pi * i / n)) for i in range(n)]
    out = list(pts)
    for m in f.get("mirror") or []:
        if m == "u":
            out = out + [(U - u, v) for (u, v) in out]
        elif m == "v":
            out = out + [(u, V - v) for (u, v) in out]
    # dedupe coincident
    uniq = []
    for p in out:
        if not any(abs(p[0] - q[0]) < 1e-6 and abs(p[1] - q[1]) < 1e-6 for q in uniq):
            uniq.append(p)
    return uniq


def derive_label(ir, plan):
    blank = ir["blank"]
    base_bends = [plan.faces[n] for n in plan.order[1:] if plan.faces[n].parent == "base"]
    nb = len(base_bends)
    if nb == 0:
        return {"rect": "Sheet", "stadium": "Sheet", "disc": "Sheet-Circular", "polygon": "Triangle" if len(blank.get("points") or []) == 3 else "Sheet"}[blank["type"]]
    if blank["type"] == "polygon":
        return "Triangle" if len(blank["points"]) == 3 else "Sheet-Bent"
    edges = [w.edge_of_parent for w in base_bends]
    dirs = set(w.bend["direction"] for w in base_bends)
    if nb == 1:
        return "L-bracket"
    if nb == 2:
        opposite = set(edges) in ({"x-", "x+"}, {"y-", "y+"})
        if opposite:
            return "U-shaped" if len(dirs) == 1 else "Z-shaped"
        return "L-bracket-corner"
    return "CAPOT" if len(dirs) == 1 else "CAPOT-mixed-direction"


# ---------------------------------------------------------------- tube

def _normalize_tube(ir):
    tube = ir.get("tube") or {}
    sec = str(tube.get("section") or "rect").lower()
    if sec in ("rect", "rectangular", "square", "rectangle"):
        tube["section"] = "rect"
        tube["width"] = _num(tube.get("width"), "tube width")
        tube["height"] = _num(tube.get("height") if tube.get("height") is not None else tube.get("width"), "tube height")
    elif sec in ("round", "circular", "circle", "cylinder", "pipe"):
        tube["section"] = "round"
        tube["diameter"] = _num(tube.get("diameter"), "tube diameter")
    else:
        raise IRError("unknown tube section %r" % sec)
    tube["length"] = _num(tube.get("length"), "tube length")
    tube["solid"] = bool(tube.get("solid"))
    if not tube["solid"]:
        tube["wall"] = _num(tube.get("wall") if tube.get("wall") is not None else ir.get("thickness"), "tube wall thickness")
        if tube["section"] == "rect" and 2 * tube["wall"] >= min(tube["width"], tube["height"]):
            raise IRError("tube wall thickness too large for the section")
        if tube["section"] == "round" and 2 * tube["wall"] >= tube["diameter"]:
            raise IRError("tube wall thickness too large for the diameter")
    else:
        tube["wall"] = None
    tube["corner_radius"] = _num(tube.get("corner_radius"), "corner_radius", allow_none=True)
    ec = tube.get("end_cuts") or {}
    out = {}
    for k in ("start", "end"):
        a = ec.get(k)
        out[k] = None if a is None else float(a)
        if out[k] is not None and not (0 < out[k] <= 90):
            raise IRError("end cut angle must be in (0, 90], 90 = straight cut")
    tube["end_cuts"] = out
    tabs = []
    face_alias = {"top": "top", "upper": "top", "bottom": "bottom", "lower": "bottom", "front": "front", "left": "front",
                  "side": "front", "back": "back", "rear": "back", "right": "back"}
    for tb in tube.get("tabs") or []:
        tb = dict(tb)
        end = str(tb.get("end") or "both").lower()
        tb["end"] = {"start": "start", "left": "start", "first": "start", "end": "end", "right": "end", "second": "end",
                     "both": "both", "each": "both"}.get(end, "both")
        size = tb.get("size") or []
        width = tb.get("width") if tb.get("width") is not None else (tb.get("across") if tb.get("across") is not None else (size[0] if len(size) > 0 else None))
        prot = tb.get("protrusion") if tb.get("protrusion") is not None else (tb.get("length") if tb.get("length") is not None else (size[1] if len(size) > 1 else None))
        tb["protrusion"] = _num(prot, "tab protrusion (length beyond the tube end)")
        tb["across"] = _num(width, "tab width across the face", allow_none=True)   # None -> full flat face width (builder)
        tb["across_defaulted"] = tb["across"] is None
        faces = tb.get("faces") or tb.get("face") or ["top", "bottom"]
        if isinstance(faces, str):
            faces = [faces]
        if tube["section"] != "rect":
            raise IRError("tabs are only supported on rectangular tubes")
        tb["faces"] = []
        for fc in faces:
            key = face_alias.get(str(fc).lower())
            if key is None:
                raise IRError("tab face %r unknown (top / bottom / front / back)" % fc)
            tb["faces"].append(key)
        tabs.append(tb)
    tube["tabs"] = tabs
    ir["tube"] = tube
    ir["thickness"] = tube["wall"] or ir.get("thickness")
    # features on tube faces
    feats = []
    for i, f in enumerate(ir.get("features") or []):
        f = dict(f)
        ftype = str(f.get("type") or "hole").lower()
        f["type"] = {"circle": "hole", "round_hole": "hole", "oblong": "slot", "rectangle": "rect", "cutout": "rect",
                     "rectangular": "rect", "tapped": "thread", "threaded": "thread", "countersunk": "countersink",
                     "tapped_hole": "thread", "threaded_hole": "thread", "rect_cutout": "rect", "window": "rect",
                     "hexagon": "hex", "hexagonal": "hex", "hex_hole": "hex", "engraving": "engrave", "text": "engrave",
                     "marking": "engrave"}.get(ftype, ftype)
        face = str(f.get("face") or "top").lower()
        if f["type"] in ("engrave", "note"):
            f["type"], f["note_only"], f["face"] = "engrave", True, face
            feats.append(f)
            continue
        if tube["section"] == "rect":
            face = {"top": "top", "bottom": "bottom", "front": "front", "back": "back", "rear": "back",
                    "left": "front", "right": "back", "side": "front", "upper": "top", "lower": "bottom"}.get(face)
            if face is None:
                raise IRError("tube feature %d: unknown face %r (use top / bottom / front / back)" % (i + 1, f.get("face")))
        else:
            ang = f.get("angle")
            if ang is None:
                ang = {"top": 0.0, "bottom": 180.0, "front": 270.0, "back": 90.0, "left": 270.0, "right": 90.0, "wall": 0.0}.get(face, 0.0)
            f["angle"] = float(ang)
            face = "wall"
        f["face"] = face
        if f["type"] in ("hole", "blind_hole"):
            f["diameter"] = _num(f.get("diameter"), "hole diameter")
        elif f["type"] == "thread":
            drill, key, pitch = tap_drill_diameter(f.get("thread") or f.get("size"), f.get("pitch"))
            f["thread"], f["pitch"], f["diameter"] = key, pitch, drill
        elif f["type"] in ("slot", "rect"):
            size = f.get("size")
            if not size or len(size) < 2:
                raise IRError("%s needs size [long, short]" % f["type"])
            a, b = _num(size[0], "size"), _num(size[1], "size")
            f["size"] = [max(a, b), min(a, b)]
            f["orientation"] = "v" if str(f.get("orientation") or "u").lower() in ("v", "across", "width") else "u"
        elif f["type"] == "countersink":
            f["diameter"] = _num(f.get("diameter"), "hole diameter")
            f["cs_diameter"] = _num(f.get("cs_diameter"), "countersink diameter")
            f["cs_angle"] = float(f.get("cs_angle") or 90.0)
            f["cs_side"] = "outer"
        elif f["type"] == "hex":
            af = f.get("across_flats") or f.get("size")
            if af is not None:
                f["circumradius"] = _num(af, "hex across_flats") / math.sqrt(3.0)
            else:
                f["circumradius"] = _num(f.get("diameter"), "hex diameter") / 2.0
            f["diameter"] = 2.0 * f["circumradius"]
        elif f["type"] == "keyhole":
            size = f.get("size")
            if not size or len(size) < 3:
                raise IRError("keyhole needs size [total_length, large_diameter, small_diameter]")
            f["size"] = [float(size[0]), float(size[1]), float(size[2])]
            f["orientation"] = "v" if str(f.get("orientation") or "u").lower() in ("v", "across", "width") else "u"
        elif f["type"] == "engrave":
            f["note_only"] = True
            feats.append(f)
            continue
        else:
            raise IRError("feature type %r not supported on tubes" % f["type"])
        f["through"] = str(f.get("through") or "one").lower()
        at = f.get("at") or {}
        nat = {}
        for axis in ("u", "v"):
            spec = at.get(axis)
            if spec is None:
                nat[axis] = {"from": "center", "dist": 0.0, "of": "center", "defaulted": True}
            elif isinstance(spec, (int, float)):
                nat[axis] = {"from": axis + "-", "dist": float(spec), "of": "center", "defaulted": False}
            else:
                ref = str(spec.get("from") or "center").lower()
                ref = {"start": "u-", "end": "u+", "left": "v-", "right": "v+", "center": "center", "centre": "center",
                       "u-": "u-", "u+": "u+", "v-": "v-", "v+": "v+", "bottom": "v-", "top": "v+"}.get(ref, "center")
                of = str(spec.get("of") or "").lower()
                if of not in ("center", "edge"):
                    of = "center" if f["type"] in ROUND_TYPES else "edge"
                nat[axis] = {"from": ref, "dist": float(spec.get("dist", 0.0) or 0.0), "of": of, "defaulted": False}
        f["at"] = nat
        pat = f.get("pattern")
        if pat:
            pat = dict(pat)
            pat["type"] = "linear"
            pat["count"] = int(pat.get("count") or 1)
            pitch = pat.get("pitch") if pat.get("pitch") is not None else pat.get("spacing")
            if pat["count"] > 1 and pitch in (None, "even", "auto"):
                pat["even"], pat["pitch"] = True, None
            else:
                pat["pitch"] = float(pitch or 0.0)
            pat["axis"] = "v" if str(pat.get("axis") or "u").lower() in ("v", "across") else "u"
            f["pattern"] = pat
        mir = f.get("mirror") or []
        f["mirror"] = [m.lower() for m in ([mir] if isinstance(mir, str) else mir) if m.lower() in ("u", "v")]
        f["positions"] = None
        feats.append(f)
    ir["features"] = feats
    return ir


def plan_tube(ir):
    ir = normalize_ir(ir)
    tube = ir["tube"]
    plan = Plan()
    L = tube["length"]
    if tube["section"] == "rect":
        W, H = tube["width"], tube["height"]
        plan.faces["top"] = Face(name="top", kind="tube", origin=(0.0, 0.0, H), u=(0, 1, 0), v=(1, 0, 0), n=(0, 0, -1), U=L, V=W)
        plan.faces["bottom"] = Face(name="bottom", kind="tube", origin=(0.0, 0.0, 0.0), u=(0, 1, 0), v=(1, 0, 0), n=(0, 0, 1), U=L, V=W)
        plan.faces["front"] = Face(name="front", kind="tube", origin=(0.0, 0.0, 0.0), u=(0, 1, 0), v=(0, 0, 1), n=(1, 0, 0), U=L, V=H)
        plan.faces["back"] = Face(name="back", kind="tube", origin=(W, 0.0, 0.0), u=(0, 1, 0), v=(0, 0, 1), n=(-1, 0, 0), U=L, V=H)
        plan.label = "Tube-Rectangular"
    else:
        D = tube["diameter"]
        plan.faces["wall"] = Face(name="wall", kind="tube_round", origin=(0.0, 0.0, 0.0), u=(0, 1, 0), v=(0, 0, 1), n=(0, 0, -1), U=L, V=math.pi * D)
        plan.label = "Tube-Circular"
    plan.order = list(plan.faces.keys())
    for f in ir["features"]:
        if f.get("note_only"):
            continue
        face = plan.faces[f["face"]]
        hu, hv = feature_half_extents(f)
        if face.kind == "tube_round":
            # v is the position along the circumference implied by f["angle"]; user gives u only
            for (u, _v) in expand_positions(f, face.U, face.V, hu, hv):
                plan.instances.append({"face": face.name, "feature": f, "u": u, "v": f["angle"], "hu": hu, "hv": hv})
        else:
            for (u, v) in expand_positions(f, face.U, face.V, hu, hv):
                plan.instances.append({"face": face.name, "feature": f, "u": u, "v": v, "hu": hu, "hv": hv})
    return ir, plan


def plan_part(ir):
    fam = (ir or {}).get("family") or "sheet"
    if str(fam).lower() == "tube":
        return plan_tube(ir)
    return plan_sheet(ir)


if __name__ == "__main__":
    # self-check: the L-bracket numbers the probes established
    ir, plan = plan_sheet({"thickness": 2, "blank": {"type": "rect", "x": 100, "y": 50},
                           "bends": [{"name": "wall", "edge": "x-", "length": 40}],
                           "features": [{"type": "hole", "face": "wall", "diameter": 6,
                                         "at": {"u": {"from": "center"}, "v": {"from": "bend", "dist": 20}}}]})
    w = plan.faces["wall"]
    assert abs(w.bend["s_corner"] - 4.0) < 1e-9 and abs(w.bend["sm_length"] - 36.0) < 1e-9, w.bend
    assert v_round(w.origin) == (0.0, 0.0, 0.0) and v_round(w.v) == (0.0, 0.0, 1.0) and v_round(w.n) == (1.0, 0.0, 0.0)
    assert plan.instances[0]["u"] == 25.0 and plan.instances[0]["v"] == 20.0
    assert plan.label == "L-bracket"
    rho, lam, s = bend_setback(120, 2, 2)
    assert abs(lam + 2.309) < 1e-3 and abs(s - 2.309) < 1e-3
    print("ir_frames self-check OK")
