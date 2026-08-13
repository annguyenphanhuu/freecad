# BendFunction.py - Functions related to bend/bracket/plate operations
# -*- coding: utf-8 -*-
import FreeCAD as App
import Part
import math
import sys
import os

# Add sheetmetal directory to path (it's at project root level)
sheetmetal_path = os.path.join(os.path.dirname(__file__), '..', 'sheetmetal')
if sheetmetal_path not in sys.path:
    sys.path.insert(0, sheetmetal_path)

from sheetmetal import SheetMetalCmd, SheetMetalBaseShapeCmd
import SheetMetalTools

def taskRestoreDefaults(obj, default_vars):
    pass
SheetMetalTools.taskRestoreDefaults = taskRestoreDefaults


# ── Helper: find edge by endpoint coordinates ────────────────────────────────
def find_edge_by_coordinates(obj, start_pt, end_pt, tolerance=1e-3):
    """Return edge name 'EdgeN' (1-based) matching start/end coords (either order).

    Parameters:
    - obj: FreeCAD Part::Feature object whose Shape.Edges will be searched
    - start_pt: tuple (x, y, z) for one endpoint
    - end_pt:   tuple (x, y, z) for the other endpoint
    - tolerance: max distance (mm) for a vertex to be considered matching

    Returns:
    - 'EdgeN' string (1-based index) or None if not found
    """
    sp = App.Vector(*start_pt)
    ep = App.Vector(*end_pt)
    for i, edge in enumerate(obj.Shape.Edges):
        if len(edge.Vertexes) < 2:
            continue
        a = edge.Vertexes[0].Point
        b = edge.Vertexes[1].Point
        if ((a.distanceToPoint(sp) < tolerance and b.distanceToPoint(ep) < tolerance) or
                (a.distanceToPoint(ep) < tolerance and b.distanceToPoint(sp) < tolerance)):
            return f"Edge{i + 1}"
    return None


# ── Helper: find flange tip edge (fully angle-aware) ───────────────────────
def find_flange_tip_edge(obj, side, dim_y, dim_x, thickness, flange_angle_deg=90.0,
                         wing_type="inside", tolerance=1.0):
    """Find the tip (top) edge of the LEFT or RIGHT flange of a U-bracket,
    robust to ANY bend angle.

    Unlike find_edge_by_coordinates(), this function does NOT rely on absolute
    (x, z) coordinates, so it works correctly when bend_angle_deg != 90°.

    Strategy
    --------
    1. Collect every straight edge that is parallel to Y (dx ≈ 0, dz ≈ 0)
       and spans the full extrusion length (|Δy| ≈ dim_y).
    2. Split into left cluster (mid_x < dim_x/2) vs right cluster (mid_x > dim_x/2).
    3. Within the cluster, take the TOP-2 edges by mid_z.
       This filters out base/fold edges which have low z regardless of angle.
       Example bug without this step: at 120°, left flange tips have x≈0~-70
       (negative!) while base fold edges have x≈0..4 — so a naive max(mid_x)
       would pick a base edge, not the flange tip.
    4. Among the 2 tip edges, pick inside vs outside by projecting each
       midpoint onto the "inside direction" vector derived from flange_angle_deg:
           LEFT  inside_dir = ( sinθ,  0, -cosθ)   [points toward U interior]
           RIGHT inside_dir = (-sinθ,  0, -cosθ)   [points toward U interior]
       Larger projection → "inside"; smaller projection → "outside".

    Derivation of inside_dir
    ------------------------
    The thickness vector (from outer surface to inner surface of the flange)
    is perpendicular to the flange surface. For the LEFT flange at angle θ:
      - Flange surface direction (toward tip): (cosθ, 0, sinθ)
      - Thickness direction (inward, 90° CW in XZ plane): (sinθ, 0, -cosθ)
    Verified numerically with the user's 120° data:
      inside_dir = (sin120°, 0, -cos120°) = (0.866, 0, 0.5)
      dot(inner_tip=(-66.46,128.98), inside_dir) = -66.46*0.866 + 128.98*0.5 ≈  6.95
      dot(outer_tip=(-69.93,126.98), inside_dir) = -69.93*0.866 + 126.98*0.5 ≈  2.93
      → inner tip has larger dot ✓

    Parameters
    ----------
    obj              : Part::Feature whose Shape.Edges will be searched
    side             : "left" or "right"
    dim_y            : Extrusion length along Y (mm)
    dim_x            : Base width along X (mm)
    thickness        : Sheet thickness (mm)
    flange_angle_deg : Bend angle of the U-shape flange (degrees, default 90)
    wing_type        : "inside" (default) or "outside"
    tolerance        : Tolerance in mm for Δx, Δz and the Δy length check

    Returns
    -------
    "EdgeN" string (1-based) or None if not found.
    """
    import math
    angle_rad = math.radians(flange_angle_deg)
    sin_a = math.sin(angle_rad)
    cos_a = math.cos(angle_rad)

    # "Inside" direction: unit vector pointing from outer face toward inner face
    # (i.e., toward the U interior along the thickness of the flange)
    if side == "left":
        inside_dir_x = sin_a    # (sinθ, 0, -cosθ)
        inside_dir_z = -cos_a
    else:  # "right"
        inside_dir_x = -sin_a   # (-sinθ, 0, -cosθ)
        inside_dir_z = -cos_a

    # ── Step 1: collect all Y-parallel edges spanning full dim_y ───────────
    candidates = []  # (edge_index_1based, mid_x, mid_z)

    for i, edge in enumerate(obj.Shape.Edges):
        verts = edge.Vertexes
        if len(verts) < 2:
            continue
        a = verts[0].Point
        b = verts[1].Point

        # Must be Y-parallel: Δx and Δz both near zero
        if abs(a.x - b.x) > tolerance or abs(a.z - b.z) > tolerance:
            continue

        # Must span the full dim_y length
        if abs(abs(a.y - b.y) - dim_y) > tolerance:
            continue

        mid_x = (a.x + b.x) / 2.0
        mid_z = (a.z + b.z) / 2.0
        candidates.append((i + 1, mid_x, mid_z))

    if not candidates:
        return None

    # ── Step 2: split into left / right cluster ────────────────────────────
    half_x = dim_x / 2.0
    if side == "left":
        cluster = [c for c in candidates if c[1] < half_x]
    else:
        cluster = [c for c in candidates if c[1] > half_x]

    if not cluster:
        cluster = candidates  # last-resort fallback

    # ── Step 3: keep only top-2 edges by z (highest z = flange tip, not base)
    # At non-90° angles, base/fold edges can have larger mid_x than the tilted
    # flange tip, so we must isolate the tip pair BEFORE inside/outside selection.
    cluster_by_z = sorted(cluster, key=lambda c: c[2], reverse=True)
    tip_pair = cluster_by_z[:2]

    if len(tip_pair) < 2:
        print(f"[find_flange_tip_edge] WARNING: only {len(tip_pair)} tip "
              f"edge(s) found in {side} cluster; may be inaccurate.")

    # ── Step 4: pick inside / outside via dot product with inside_dir ──────
    def inside_proj(c):
        """Projection of edge midpoint onto the inside direction (XZ only)."""
        return c[1] * inside_dir_x + c[2] * inside_dir_z

    if wing_type == "inside":
        best = max(tip_pair, key=inside_proj)
    else:
        best = min(tip_pair, key=inside_proj)

    print(f"[find_flange_tip_edge] side={side}, wing_type={wing_type}, "
          f"angle={flange_angle_deg}°: "
          f"found Edge{best[0]} (mid_x={best[1]:.3f}, mid_z={best[2]:.3f}) "
          f"| inside_dir=({inside_dir_x:.3f},0,{inside_dir_z:.3f}) "
          f"proj={inside_proj(best):.3f} "
          f"| cluster={len(cluster)} total candidates={len(candidates)}")
    return f"Edge{best[0]}"


# ── Return-wing builders ─────────────────────────────────────────────────────
def AddUShapeLeftReturn(shape, thickness, dim_x, dim_y, flange_height,
                           wing_length, wing_angle_deg, bend_radius,
                           flange_angle_deg=90.0, tolerance=1.0, wing_type="inside"):
    """Bend a small return wing on top of the LEFT flange of a U-bracket.

    Uses find_flange_tip_edge() which is fully robust to any flange_angle_deg.

    Parameters:
    - shape:            Part::Feature to bend from (u_shape_obj or previous wing)
    - thickness:        Sheet thickness (mm)
    - dim_x:            Base width of the U-bracket along X (mm)
    - dim_y:            Length of the U-bracket along Y (mm)
    - flange_height:    Height of the left/right flanges (mm)
    - wing_length:      How far the return wing extends (mm)
    - wing_angle_deg:   Desired bend angle of the wing in degrees (typically 90)
    - bend_radius:      Inner bend radius (mm)
    - flange_angle_deg: Bend angle of the U-shape flange (degrees, default 90).
                        MUST match the bend_angle_deg used in Part.makeUShape().
                        Used to compute the inside-direction vector for correct
                        edge selection at non-90° angles.
    - tolerance:        Vertex-matching tolerance for edge search (mm)
    - wing_type:        "inside" (default) or "outside"

    Returns:
    - wing_obj (Part::FeaturePython) or None if the edge was not found
    """
    import FreeCAD as App
    
    is_raw_shape = not hasattr(shape, "Shape")
    if is_raw_shape:
        doc = App.ActiveDocument
        shape_obj = doc.addObject("Part::Feature", "TempLeftWingBase")
        shape_obj.Shape = shape
    else:
        shape_obj = shape
        doc = getattr(shape_obj, "Document", App.ActiveDocument)
        if not doc:
            doc = App.ActiveDocument

    edge_name = find_flange_tip_edge(
        shape_obj, "left",
        dim_y=dim_y, dim_x=dim_x, thickness=thickness,
        flange_angle_deg=flange_angle_deg,
        wing_type=wing_type, tolerance=tolerance,
    )
    if edge_name is None:
        print(f"[AddUShapeLeftReturn] WARNING: could not find tip edge "
              f"(side=left, wing_type={wing_type}), left wing skipped.")
        return shape if is_raw_shape else shape.Shape

    print(f"[AddUShapeLeftReturn] Found edge: {edge_name}")

    wing_obj = doc.addObject("Part::FeaturePython", "LeftWing")
    SheetMetalCmd.SMBendWall(wing_obj, shape_obj, [edge_name])
    wing_obj.length = wing_length
    wing_obj.angle  = 180.0 - wing_angle_deg   # SheetMetal convention
    wing_obj.radius = bend_radius
    doc.recompute()
    
    return wing_obj


def AddUShapeRightReturn(shape, thickness, dim_x, dim_y, flange_height,
                            wing_length, wing_angle_deg, bend_radius,
                            flange_angle_deg=90.0, tolerance=1.0, wing_type="inside"):
    """Bend a small return wing on top of the RIGHT flange of a U-bracket.

    Uses find_flange_tip_edge() which is fully robust to any flange_angle_deg.

    Parameters:
    - shape:            Part::Feature to bend from (u_shape_obj or previous wing)
    - thickness:        Sheet thickness (mm)
    - dim_x:            Base/width of the U-bracket along X (mm)
    - dim_y:            Length of the U-bracket along Y (mm)
    - flange_height:    Height of the left/right flanges (mm)
    - wing_length:      How far the return wing extends (mm)
    - wing_angle_deg:   Desired bend angle of the wing in degrees (typically 90)
    - bend_radius:      Inner bend radius (mm)
    - flange_angle_deg: Bend angle of the U-shape flange (degrees, default 90).
                        MUST match the bend_angle_deg used in Part.makeUShape().
                        Used to compute the inside-direction vector for correct
                        edge selection at non-90° angles.
    - tolerance:        Vertex-matching tolerance for edge search (mm)
    - wing_type:        "inside" (default) or "outside"

    Returns:
    - wing_obj (Part::FeaturePython) or None if the edge was not found
    """
    import FreeCAD as App
    
    is_raw_shape = not hasattr(shape, "Shape")
    if is_raw_shape:
        doc = App.ActiveDocument
        shape_obj = doc.addObject("Part::Feature", "TempRightWingBase")
        shape_obj.Shape = shape
    else:
        shape_obj = shape
        doc = getattr(shape_obj, "Document", App.ActiveDocument)
        if not doc:
            doc = App.ActiveDocument

    edge_name = find_flange_tip_edge(
        shape_obj, "right",
        dim_y=dim_y, dim_x=dim_x, thickness=thickness,
        flange_angle_deg=flange_angle_deg,
        wing_type=wing_type, tolerance=tolerance,
    )
    if edge_name is None:
        print(f"[AddUShapeRightReturn] WARNING: could not find tip edge "
              f"(side=right, wing_type={wing_type}), right wing skipped.")
        return shape if is_raw_shape else shape.Shape

    print(f"[AddUShapeRightReturn] Found edge: {edge_name}")

    wing_obj = doc.addObject("Part::FeaturePython", "RightWing")
    SheetMetalCmd.SMBendWall(wing_obj, shape_obj, [edge_name])
    wing_obj.length = wing_length
    wing_obj.angle  = 180.0 - wing_angle_deg   # SheetMetal convention
    wing_obj.radius = bend_radius
    doc.recompute()
    
    return wing_obj


def AddLShapeReturn(shape, thickness, dim_x, dim_y, flange_height,
                    wing_length, wing_angle_deg, bend_radius,
                    flange_angle_deg=90.0, tolerance=1.0, wing_type="inside"):
    """Bend a small return wing on top of the vertical flange of an L-bracket.
    
    Since the vertical flange of an L-bracket created by makeLShape is located
    at X=0, this is geometrically equivalent to the left flange of a U-bracket.
    
    Parameters:
    - shape:            Part::Feature to bend from (l_shape_obj)
    - thickness:        Sheet thickness (mm)
    - dim_x:            Base width of the L-bracket along X (mm)
    - dim_y:            Length of the L-bracket along Y (mm)
    - flange_height:    Height of the vertical flange (mm)
    - wing_length:      How far the return wing extends (mm)
    - wing_angle_deg:   Desired bend angle of the wing in degrees (typically 90)
    - bend_radius:      Inner bend radius (mm)
    - flange_angle_deg: Bend angle of the L-shape flange (degrees, default 90).
    - tolerance:        Vertex-matching tolerance for edge search (mm)
    - wing_type:        "inside" (default) or "outside"
    
    Returns:
    - wing_obj (Part::FeaturePython) or None if the edge was not found
    """
    print("[AddLShapeReturn] Aliasing to AddUShapeLeftReturn because L-bracket vertical flange is at X=0.")
    return AddUShapeLeftReturn(
        shape=shape, thickness=thickness, dim_x=dim_x, dim_y=dim_y, 
        flange_height=flange_height, wing_length=wing_length, 
        wing_angle_deg=wing_angle_deg, bend_radius=bend_radius,
        flange_angle_deg=flange_angle_deg, tolerance=tolerance, wing_type=wing_type
    )



def map_and_cut_leg2(bracket_shape, tool_shape, hole_center_y, hole_center_z, angle_deg, dim_y, thickness, flange_height, bend_radius):
    """
    Universal function to translate, rotate, and cut an arbitrary tool_shape onto the leg2 face.

    Assumptions for tool_shape:
    It should be created at the origin (0,0,0) in a standard 90-degree coordinate system:
      - Cut direction (into the wall) goes along +X (e.g., dir=App.Vector(1,0,0)).
      - The width/length dimension corresponding to `dim_y` aligns with global Y.
      - The height corresponding to `flange_height` aligns with global Z.

    ⚡ 90° FAST-PATH: When angle_deg == 90, the left flange is already a standard
    vertical wall aligned with global axes — the tool shape is already positioned
    correctly (pnt.x=0, dir=(1,0,0)). No rotation matrix is needed; cut directly.
    """
    import FreeCAD as App
    import math

    # ── 90° Fast-path: direct cut, no mapping needed ─────────────────────────
    if abs(angle_deg - 90.0) < 1e-6:
        try:
            result_shape = bracket_shape.cut(tool_shape)
            print(f"[map_and_cut_leg2] 90° fast-path: cut OK "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f})")
            return result_shape
        except Exception as exc:
            print(f"[map_and_cut_leg2] 90° fast-path: cut FAILED "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f}): {exc}")
            return bracket_shape
    # ─────────────────────────────────────────────────────────────────────────

    bend_deduction = bend_radius + thickness
    adjusted_hole_z = hole_center_z - bend_deduction
    
    leg2_face = None
    max_area = 0

    angle_rad = math.radians(angle_deg)
    expected_nx = -math.sin(angle_rad)
    expected_nz =  math.cos(angle_rad)
    expected_ny =  0.0
    if abs(expected_nx) < 1e-10: expected_nx = 0.0
    if abs(expected_ny) < 1e-10: expected_ny = 0.0
    if abs(expected_nz) < 1e-10: expected_nz = 0.0

    tolerance = 0.35
    candidates = []

    for face in bracket_shape.Faces:
        u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2
        v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2
        n = face.normalAt(u_mid, v_mid)

        if n.x > 0:
            continue

        score = abs(n.x - expected_nx) + abs(n.y - expected_ny) + abs(n.z - expected_nz)
        min_area = flange_height * dim_y * 0.5

        if score < tolerance and face.Area > min_area:
            center = face.valueAt(u_mid, v_mid)
            candidates.append((score, center.x, face))

    if candidates:
        candidates.sort(key=lambda c: (round(c[0], 8), abs(c[1])))
        leg2_face = candidates[0][2]

    if leg2_face is None:
        max_area = 0
        for face in bracket_shape.Faces:
            n = face.normalAt(0, 0)
            if (n.x < -0.1 and abs(n.z) < 0.9 and face.Area > max_area and face.Area > flange_height * dim_y * 0.3):
                max_area = face.Area
                leg2_face = face

    if leg2_face is None:
        print("Could not find suitable leg2 face for mapping tool")
        return bracket_shape

    u_min, u_max, v_min, v_max = leg2_face.ParameterRange
    corner_points = [
        leg2_face.valueAt(u_min, v_min),
        leg2_face.valueAt(u_max, v_min),
        leg2_face.valueAt(u_min, v_max),
        leg2_face.valueAt(u_max, v_max)
    ]

    y_variation_u = abs(corner_points[1].y - corner_points[0].y)
    y_variation_v = abs(corner_points[2].y - corner_points[0].y)
    effective_flange_height = flange_height - bend_deduction
    
    if y_variation_u > y_variation_v:
        u_normalized = hole_center_y / dim_y
        v_normalized = adjusted_hole_z / effective_flange_height
    else:
        u_normalized = adjusted_hole_z / effective_flange_height
        v_normalized = hole_center_y / dim_y

    u_param = u_min + u_normalized * (u_max - u_min)
    v_param = v_min + v_normalized * (v_max - v_min)

    hole_position = leg2_face.valueAt(u_param, v_param)
    
    # normal points OUTWARD from the face
    normal = leg2_face.normalAt(u_param, v_param)

    # In the 90-degree canonical frame for a vertical wall:
    # +X is the inward cut direction. We map this to `-normal`.
    t_x_target = -normal.normalize()

    # +Y is along the flange length (dim_y). We map this to global +Y.
    t_y_target = App.Vector(0, 1, 0)
    
    # +Z is the UP direction on the flange face.
    t_z_target = t_x_target.cross(t_y_target).normalize()

    # Re-normalize t_y_target to ensure perfect orthogonality
    t_y_target = t_z_target.cross(t_x_target).normalize()

    # The tool should start somewhat OUTSIDE the face to ensure complete cuts.
    # We move it backward along the cut direction (-t_x_target) which is outward normal.
    offset_dist = 2.0
    start_pos = hole_position - t_x_target * offset_dist

    mat = App.Matrix()
    mat.A11 = t_x_target.x; mat.A12 = t_y_target.x; mat.A13 = t_z_target.x; mat.A14 = start_pos.x
    mat.A21 = t_x_target.y; mat.A22 = t_y_target.y; mat.A23 = t_z_target.y; mat.A24 = start_pos.y
    mat.A31 = t_x_target.z; mat.A32 = t_y_target.z; mat.A33 = t_z_target.z; mat.A34 = start_pos.z

    placement = App.Placement(mat)
    
    tool_shape.Placement = placement

    try:
        result_shape = bracket_shape.cut(tool_shape)
        print("Successfully mapped and cut tool_shape on leg2")
        return result_shape
    except Exception as e:
        print(f"Error cutting mapped shape on leg2: {e}")
        return bracket_shape


def map_and_cut_leg2_batch(bracket_shape, tools, angle_deg, dim_y, thickness, flange_height, bend_radius):
    """
    Batch function: finds the leg2 / left-flange face ONCE, then maps and cuts
    all tool shapes in a single pass, automatically handling any bend angle.

    ── HOW TO CREATE TOOLS (90° natural-coordinate convention) ──────────────────
    Build every tool as if the flange were a standard VERTICAL 90° wall:
      • The cut direction is +X  (dir = App.Vector(1, 0, 0))
      • pnt.x = 0               (outer face of the 90° wall)
      • pnt.y = hole_center_y + size_y / 2   (Y runs in −Y from pnt with dir=(1,0,0))
      • pnt.z = hole_center_z − size_z / 2   (Z runs upward from pnt)

    Where hole_center_y and hole_center_z are the ACTUAL target coordinates
    measured from the same origin as makeLShape / makeUShape:
      • hole_center_y  ∈ [0, dim_y]          — along the extrusion length
      • hole_center_z  ∈ [0, flange_height]  — height measured from base z=0

    Examples:
      # Circle centered at (cy, cz):
      tool = Part.makeCylinder(r, thickness + 2.0,
                               App.Vector(0, cy, cz), App.Vector(1, 0, 0))

      # Box/Rectangle centered at (cy, cz), size sz×sy:
      tool = Part.makeBox(sz, sy, thickness + 2.0,
                          App.Vector(0, cy + sy/2, cz - sz/2), App.Vector(1, 0, 0))

      # Oblong (sz=Z-extent, sy=Y-extent):
      tool = Part.makeOblong(sz, sy, thickness + 2.0,
                             App.Vector(0, cy + sy/2, cz - sz/2), App.Vector(1, 0, 0))

    The function rotates these 90° tools onto the actual angled face (any angle_deg)
    and subtracts the bend zone automatically — you never need to worry about the
    bend_radius, angle, or face geometry.

    ⚡ 90° FAST-PATH: When angle_deg == 90, the left flange is already a standard
    vertical wall with normal (-1, 0, 0). Tools created with pnt.x=0 and
    dir=(1,0,0) are already correctly positioned — cut directly without any
    rotation matrix. This avoids floating-point issues at sin(90°)/cos(90°).

    ── IMPORTANT ───────────────────────────────────────────────────────────────
    • Each tool must be a SEPARATE instance (Placement is modified in-place).
    • hole_center_y / hole_center_z in the dict are used ONLY for logging;
      the tool's actual position is already encoded in its pnt.

    ── How it works (geometry) ────────────────────────────────────────────────
    Step 1 — Find the outer face using bidirectional normal matching
             (works for L-bracket AND U-bracket at ANY angle).
    Step 2 — Build a local 3-D frame [t_x, t_y, t_z] from the face's real edges:
               t_x = cut direction (−face_normal)
               t_y = global +Y (extrusion axis)
               t_z = "up the flange" (derived from t_x×t_y, corrected by actual edge Z)
             find origin_pt = vertex on the bend-line edge at Y=0.
    Step 3 — A single shared placement matrix maps all tool shapes:
               start_pos = origin_pt − t_x×OFFSET − t_z×bend_deduction
             After the matrix, a tool whose local pnt is at (0, cy, cz) arrives at:
               origin_pt + t_y×cy + t_z×(cz − bend_deduction) − t_x×OFFSET
             which is precisely the desired point on the angled face.   ✓

    Parameters
    ----------
    bracket_shape   : Part.Shape  — working solid
    tools           : list of {"tool_shape", "hole_center_y", "hole_center_z"}
    angle_deg       : float  — bend angle in degrees (e.g. 90, 110, 120)
    dim_y           : float  — extrusion length along Y
    thickness       : float  — sheet thickness
    flange_height   : float  — total flange height (from base z=0)
    bend_radius     : float  — inner bend radius

    Returns
    -------
    Part.Shape with all tools cut out.
    """
    import FreeCAD as App
    import math

    # ── 90° Fast-path: tools are already in world coords, cut directly ────────
    if abs(angle_deg - 90.0) < 1e-6:
        print("[map_and_cut_leg2_batch] 90° fast-path: cutting tools directly (no rotation).")
        for entry in tools:
            tool_shape    = entry["tool_shape"]
            hole_center_y = entry["hole_center_y"]
            hole_center_z = entry["hole_center_z"]
            try:
                bracket_shape = bracket_shape.cut(tool_shape)
                print(f"[map_and_cut_leg2_batch] 90° cut OK  "
                      f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f})")
            except Exception as exc:
                print(f"[map_and_cut_leg2_batch] 90° cut FAILED "
                      f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f}): {exc}")
        return bracket_shape
    # ─────────────────────────────────────────────────────────────────────────

    bend_deduction = bend_radius + thickness

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 1 — Find the outer face of the left flange / leg2
    # ═══════════════════════════════════════════════════════════════════════
    angle_rad = math.radians(angle_deg)
    sin_a = math.sin(angle_rad)
    cos_a = math.cos(angle_rad)

    # The outer (outward-pointing) normal of the left-flange plate.
    # L-bracket (θ ≤ 90°): flange goes upward-and-inward  → n_outer.x < 0
    # U-bracket  (θ > 90°): left flange goes outward-and-up → n_outer.x > 0
    # We encode both sign conventions explicitly and test BOTH at once:
    #   candidate_normals = [(-sinθ, 0, cosθ), (+sinθ, 0, cosθ)]
    # The one with the best face-normal match and largest qualifying area wins.
    def _snap0(v):
        return 0.0 if abs(v) < 1e-10 else v

    candidate_normals = [
        App.Vector(_snap0(-sin_a), 0.0, _snap0(cos_a)),   # classic L-bracket outer face
        App.Vector(_snap0( sin_a), 0.0, _snap0(cos_a)),   # U-bracket left-flange outer face
    ]

    SCORE_TOL = 0.35                          # Manhattan-distance tolerance on normal
    MIN_AREA  = flange_height * dim_y * 0.45  # at least 45 % of theoretical area

    best_score  = float("inf")
    best_area   = 0.0
    best_cx     = float("inf")   # tie-breaker: smallest center_x (leg2 = X=0 side)
    leg2_face   = None
    leg2_normal = None   # the outward normal of the chosen face (App.Vector)

    for face in bracket_shape.Faces:
        if face.Area < MIN_AREA:
            continue

        u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2
        v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2
        n     = face.normalAt(u_mid, v_mid)
        center = face.valueAt(u_mid, v_mid)

        for n_ref in candidate_normals:
            score = abs(n.x - n_ref.x) + abs(n.y - n_ref.y) + abs(n.z - n_ref.z)
            if score < SCORE_TOL:
                # Prefer smallest score; break ties with smallest center_x
                # (leg2 / left / top flange is always on the X=0 side)
                # This fixes Z-bracket where bottom-inner-face has the SAME normal
                # and area as top-outer-face, but center_x is much larger.
                if (score < best_score - 1e-6 or
                    (abs(score - best_score) < 1e-6 and center.x < best_cx - 1e-6)):
                    best_score  = score
                    best_area   = face.Area
                    best_cx     = center.x
                    leg2_face   = face
                    leg2_normal = n_ref   # store expected normal, not noisy measured one

    # ── Fallback: pick the largest planar face that looks like the left flange ─
    if leg2_face is None:
        print("[map_and_cut_leg2_batch] WARNING: normal-match failed; using area fallback.")
        max_area = 0.0
        for face in bracket_shape.Faces:
            if face.Area <= max_area:
                continue
            n = face.normalAt(
                (face.ParameterRange[0] + face.ParameterRange[1]) / 2,
                (face.ParameterRange[2] + face.ParameterRange[3]) / 2,
            )
            # Any face that is not horizontal (base) and not a side panel
            if abs(n.y) < 0.05 and abs(n.z) < 0.98 and face.Area > flange_height * dim_y * 0.3:
                max_area    = face.Area
                leg2_face   = face
                leg2_normal = n

    if leg2_face is None:
        print("[map_and_cut_leg2_batch] ERROR: Could not find leg2 face — no cuts applied.")
        return bracket_shape

    print(f"[map_and_cut_leg2_batch] Found face: area={leg2_face.Area:.1f}, "
          f"n_ref=({leg2_normal.x:.3f},0,{leg2_normal.z:.3f}), score={best_score:.4f}")

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 2 — Build a stable 3-D local frame from the face's actual geometry
    #
    # Convention (matches canonical tool frame):
    #   t_x  = inward-cut direction  = −leg2_normal   (into the plate)
    #   t_y  = along extrusion length = global +Y
    #   t_z  = "up the flange"         = t_x × t_y  (then re-orthogonalised)
    #
    # face_origin_3d: the 3-D point on the outer face that corresponds to the
    #   canonical origin (y=0, z=0), i.e. the intersection of the bend line
    #   and the front end (Y=0) of the flange.
    #   We derive it from the face's own edges rather than UV-parameter tricks.
    # ═══════════════════════════════════════════════════════════════════════

    # Local frame construction:
    #   t_x = inward cut direction = -leg2_normal
    #   t_y = along extrusion = always global +Y (since dim_y is the Y extrusion)
    #   t_z = "up the flange" = t_x × t_y, corrected to always point away from base
    t_x = App.Vector(-leg2_normal.x, -leg2_normal.y, -leg2_normal.z).normalize()
    t_y = App.Vector(0.0, 1.0, 0.0)    # extrusion axis: always global +Y

    t_z_raw = t_x.cross(t_y)
    if t_z_raw.Length < 1e-9:
        t_z = App.Vector(0.0, 0.0, 1.0)
    else:
        t_z = t_z_raw.normalize()

    # ── Locate bend-line edge and face origin via edge geometry ────────────
    # Collect all Y-parallel edges on the face (Δx≈0, Δz≈0, span ≈ dim_y).
    # Sort by world Z coordinate to find:
    #   bend-line edge  (smallest Z = closest to the base plate)
    #   tip edge        (largest Z  = free end of the flange)
    tol_edge = 1.0   # mm tolerance for parallelism and length checks

    y_parallel_edges = []   # list of (z_mid, edge)
    for edge in leg2_face.Edges:
        verts = edge.Vertexes
        if len(verts) < 2:
            continue
        a = verts[0].Point
        b = verts[1].Point
        # Y-parallel check: Δx and Δz must be small
        if abs(a.x - b.x) > tol_edge or abs(a.z - b.z) > tol_edge:
            continue
        # Span ≈ dim_y
        if abs(abs(a.y - b.y) - dim_y) > tol_edge:
            continue
        z_mid = (a.z + b.z) / 2.0
        y_parallel_edges.append((z_mid, edge))

    if y_parallel_edges:
        # Sort by world-Z: smallest = bend-line (closest to base), largest = tip
        y_parallel_edges.sort(key=lambda e: e[0])
        bend_z_mid, bend_edge = y_parallel_edges[0]
        tip_z_mid,  _tip_edge = y_parallel_edges[-1]

        # Ensure t_z points FROM bend edge TOWARD tip edge.
        # Since tip_z_mid > bend_z_mid (tip is higher in world Z),
        # t_z must have a positive Z component. Flip if needed.
        if t_z.z < 0:
            t_z = t_z * -1.0
        # t_y stays (0, 1, 0) regardless of t_z flip (extrusion is always along Y)

        # Face origin = vertex on bend-line edge with minimum Y (front face Y=0)
        verts = bend_edge.Vertexes
        origin_pt = min((v.Point for v in verts), key=lambda p: p.y)
    else:
        # Fallback: sample the face at (u_min, v_min) — rarely needed
        print("[map_and_cut_leg2_batch] WARNING: no Y-parallel edges found; using UV fallback.")
        u_min_p, u_max_p, v_min_p, v_max_p = leg2_face.ParameterRange
        origin_pt = leg2_face.valueAt(u_min_p, v_min_p)

    print(f"[map_and_cut_leg2_batch] face_origin={origin_pt}, "
          f"t_x=({t_x.x:.3f},{t_x.y:.3f},{t_x.z:.3f}), "
          f"t_y=({t_y.x:.3f},{t_y.y:.3f},{t_y.z:.3f}), "
          f"t_z=({t_z.x:.3f},{t_z.y:.3f},{t_z.z:.3f})")

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 3 — Place ALL tools with a single shared placement matrix
    #
    # ── Natural-coordinate convention ────────────────────────────────────
    # Tools are created with ACTUAL flange coordinates in pnt (no origin shift):
    #   For dir=(1,0,0):
    #     param1 = size_z  →  pnt.z = hole_center_z - size_z / 2
    #     param2 = size_y  →  pnt.y = hole_center_y + size_y / 2  (ADD — Y runs in −Y)
    #     param3 = cut_depth
    #     pnt.x = 0
    #   For cylinder:
    #     pnt = App.Vector(0, hole_center_y, hole_center_z)
    #
    # The AI simply uses the actual (y, z) coordinates — no mental gymnastics.
    # This function absorbs the bend_deduction automatically in start_pos.
    #
    # ── Why a single matrix works for ALL tools ───────────────────────────
    # After applying [t_x | t_y | t_z | start_pos], a tool whose local pnt
    # is at (0, cy, cz) maps to world:
    #   start_pos + t_y*cy + t_z*cz
    #   = (origin_pt − t_x*OFFSET − t_z*bend_deduction) + t_y*cy + t_z*cz
    #   = origin_pt + t_y*cy + t_z*(cz − bend_deduction) − t_x*OFFSET
    #   = [correct face position] − [offset outside face]   ✓
    # ═══════════════════════════════════════════════════════════════════════
    OFFSET = 0.0   # mm — canonical x=0 lands exactly at the outer face after placement.
                   # Tools are responsible for their own outside-face clearance:
                   # - Part.makeCylinder(r, thickness+2, pnt=(0,cy,cz)): starts AT outer face ✓
                   # - Part.makeCountersink(...): internal buffer shifts cyl_start to -buffer ✓


    # start_pos is CONSTANT for the whole batch (independent of cy/cz)
    start_pos = origin_pt - t_x * OFFSET - t_z * bend_deduction

    mat = App.Matrix()
    mat.A11 = t_x.x;  mat.A12 = t_y.x;  mat.A13 = t_z.x;  mat.A14 = start_pos.x
    mat.A21 = t_x.y;  mat.A22 = t_y.y;  mat.A23 = t_z.y;  mat.A24 = start_pos.y
    mat.A31 = t_x.z;  mat.A32 = t_y.z;  mat.A33 = t_z.z;  mat.A34 = start_pos.z
    mat.A44 = 1.0
    placement = App.Placement(mat)

    for entry in tools:
        tool_shape    = entry["tool_shape"]
        hole_center_y = entry["hole_center_y"]   # used for logging only
        hole_center_z = entry["hole_center_z"]   # used for logging only

        tool_shape.Placement = placement

        try:
            bracket_shape = bracket_shape.cut(tool_shape)
            print(f"[map_and_cut_leg2_batch] cut OK  "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f})")
        except Exception as exc:
            print(f"[map_and_cut_leg2_batch] cut FAILED "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f}): {exc}")

    return bracket_shape


def map_and_cut_right_flange_batch(bracket_shape, tools, angle_deg, dim_x, dim_y, thickness, flange_height, bend_radius):
    """
    Batch function: finds the RIGHT flange face of a U-bracket ONCE, then maps and
    cuts all tool shapes in a single pass, automatically handling any bend angle.

    This is the mirror of map_and_cut_leg2_batch for the RIGHT flange.

    ── HOW TO CREATE TOOLS (90° natural-coordinate convention) ──────────────────
    Build every tool as if the right flange were a standard VERTICAL 90° wall:
      • The cut direction is +X  (dir = App.Vector(1, 0, 0))
      • pnt.x = dim_x - thickness   (inner face of the 90° right wall)
      • pnt.y = hole_center_y + size_y / 2   (ADD — Y runs in −Y from pnt with dir=(1,0,0))
      • pnt.z = hole_center_z − size_z / 2   (SUBTRACT — Z runs upward from pnt)

    Where hole_center_y and hole_center_z use the same coordinate system as makeUShape:
      • hole_center_y  ∈ [0, dim_y]           — along the extrusion length
      • hole_center_z  ∈ [0, flange_height]   — height measured from base z=0

    Examples:
      x0 = dim_x - thickness   # right flange inner-face X position at 90°

      # Circle centered at (cy, cz):
      tool = Part.makeCylinder(r, thickness + 2.0,
                               App.Vector(x0, cy, cz), App.Vector(1, 0, 0))

      # Box/Rectangle (sz=Z-extent, sy=Y-extent):
      tool = Part.makeBox(sz, sy, thickness + 2.0,
                          App.Vector(x0, cy + sy/2, cz - sz/2), App.Vector(1, 0, 0))

      # Oblong (sz=Z-extent, sy=Y-extent):
      tool = Part.makeOblong(sz, sy, thickness + 2.0,
                             App.Vector(x0, cy + sy/2, cz - sz/2), App.Vector(1, 0, 0))

    ⚡ 90° FAST-PATH: When angle_deg == 90, the right flange is already a standard
    vertical wall at X = dim_x. Tools created with pnt.x = dim_x - thickness and
    dir=(1,0,0) are already correctly positioned — cut directly without any
    rotation matrix. This avoids floating-point issues at sin(90°)/cos(90°).

    ── IMPORTANT ───────────────────────────────────────────────────────────────
    • Each tool must be a SEPARATE instance (transformShape is applied in-place).
    • hole_center_y / hole_center_z in the dict are used ONLY for logging;
      the tool's actual position is already encoded in its pnt.

    ── How it works (geometry) ────────────────────────────────────────────────
    Step 1 — Find the outer right-flange face:  normal = (+sinθ, 0, +cosθ)
    Step 2 — Build local frame from the face's actual Y-parallel edges:
               t_x = −right_normal  (inward cut direction)
               t_y = global +Y
               t_z = t_x × t_y, flipped so t_z.z > 0 (points from bend toward tip)
             origin_pt = vertex on bend-line edge at Y=0.
    Step 3 — One shared matrix maps all tools:
               start_pos = origin_pt − t_x×(OFFSET + dim_x − thickness) − t_z×bend_deduction
             A tool at local (dim_x−thickness, cy, cz) arrives at:
               origin_pt + t_y×cy + t_z×(cz − bend_deduction) − t_x×OFFSET  ✓

    Parameters
    ----------
    bracket_shape   : Part.Shape  — working solid
    tools           : list of {"tool_shape", "hole_center_y", "hole_center_z"}
    angle_deg       : float  — bend angle in degrees (e.g. 90, 110, 120)
    dim_x           : float  — base width of U-bracket along X (mm)
    dim_y           : float  — extrusion length along Y
    thickness       : float  — sheet thickness
    flange_height   : float  — total right-flange height (from base z=0)
    bend_radius     : float  — inner bend radius

    Returns
    -------
    Part.Shape with all tools cut out.
    """
    import FreeCAD as App
    import math

    # ── 90° Fast-path: tools are already in world coords, cut directly ────────
    if abs(angle_deg - 90.0) < 1e-6:
        print("[map_and_cut_right_flange_batch] 90° fast-path: cutting tools directly (no rotation).")
        for entry in tools:
            tool_shape    = entry["tool_shape"]
            hole_center_y = entry["hole_center_y"]
            hole_center_z = entry["hole_center_z"]
            try:
                bracket_shape = bracket_shape.cut(tool_shape)
                print(f"[map_and_cut_right_flange_batch] 90° cut OK  "
                      f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f})")
            except Exception as exc:
                print(f"[map_and_cut_right_flange_batch] 90° cut FAILED "
                      f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f}): {exc}")
        return bracket_shape
    # ─────────────────────────────────────────────────────────────────────────

    bend_deduction = bend_radius + thickness

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 1 — Find the outer face of the right flange
    # ═══════════════════════════════════════════════════════════════════════
    angle_rad = math.radians(angle_deg)
    sin_a = math.sin(angle_rad)
    cos_a = math.cos(angle_rad)

    def _snap0(v):
        return 0.0 if abs(v) < 1e-10 else v

    # Right flange outer normal at angle θ: (+sinθ, 0, +cosθ)
    # At 90°: (1, 0, 0).  At 120°: (0.866, 0, -0.5).
    candidate_normals = [
        App.Vector(_snap0(sin_a),  0.0, _snap0(cos_a)),   # right outer face (primary)
        App.Vector(_snap0(-sin_a), 0.0, _snap0(cos_a)),   # symmetric fallback
    ]

    SCORE_TOL = 0.35
    MIN_AREA  = flange_height * dim_y * 0.45

    best_score   = float("inf")
    best_cx      = -float("inf")   # tie-breaker: pick rightmost (largest center_x)
    right_face   = None
    right_normal = None

    for face in bracket_shape.Faces:
        if face.Area < MIN_AREA:
            continue

        u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2
        v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2
        n     = face.normalAt(u_mid, v_mid)

        # Hard guard: only consider faces with positive X component (right flange side)
        if n.x < 0:
            continue

        center = face.valueAt(u_mid, v_mid)

        for n_ref in candidate_normals:
            score = abs(n.x - n_ref.x) + abs(n.y - n_ref.y) + abs(n.z - n_ref.z)
            if score < SCORE_TOL:
                if score < best_score - 1e-6 or (abs(score - best_score) < 1e-6 and center.x > best_cx):
                    best_score   = score
                    best_cx      = center.x
                    right_face   = face
                    right_normal = n_ref

    # ── Fallback ──────────────────────────────────────────────────────────────
    if right_face is None:
        print("[map_and_cut_right_flange_batch] WARNING: normal-match failed; using area fallback.")
        max_area = 0.0
        for face in bracket_shape.Faces:
            if face.Area <= max_area:
                continue
            n = face.normalAt(
                (face.ParameterRange[0] + face.ParameterRange[1]) / 2,
                (face.ParameterRange[2] + face.ParameterRange[3]) / 2,
            )
            if abs(n.y) < 0.05 and n.x > 0.05 and abs(n.z) < 0.98 and face.Area > flange_height * dim_y * 0.3:
                max_area     = face.Area
                right_face   = face
                right_normal = n

    if right_face is None:
        print("[map_and_cut_right_flange_batch] ERROR: Could not find right flange face — no cuts applied.")
        return bracket_shape

    print(f"[map_and_cut_right_flange_batch] Found face: area={right_face.Area:.1f}, "
          f"n_ref=({right_normal.x:.3f},0,{right_normal.z:.3f}), score={best_score:.4f}")

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 2 — Build a stable 3-D local frame from the face's actual geometry
    #
    #   t_x = inward-cut direction = −right_normal
    #   t_y = global +Y (extrusion axis)
    #   t_z = "up the flange" = t_x × t_y, flipped so t_z.z > 0
    # ═══════════════════════════════════════════════════════════════════════
    t_x = App.Vector(-right_normal.x, -right_normal.y, -right_normal.z).normalize()
    t_y = App.Vector(0.0, 1.0, 0.0)

    t_z_raw = t_x.cross(t_y)
    if t_z_raw.Length < 1e-9:
        t_z = App.Vector(0.0, 0.0, 1.0)
    else:
        t_z = t_z_raw.normalize()

    # ── Locate bend-line edge and face origin via edge geometry ────────────
    # Collect Y-parallel edges on the face (Δx≈0, Δz≈0, span≈dim_y).
    tol_edge = 1.0
    y_parallel_edges = []
    for edge in right_face.Edges:
        verts = edge.Vertexes
        if len(verts) < 2:
            continue
        a = verts[0].Point
        b = verts[1].Point
        if abs(a.x - b.x) > tol_edge or abs(a.z - b.z) > tol_edge:
            continue
        if abs(abs(a.y - b.y) - dim_y) > tol_edge:
            continue
        z_mid = (a.z + b.z) / 2.0
        y_parallel_edges.append((z_mid, edge))

    if y_parallel_edges:
        y_parallel_edges.sort(key=lambda e: e[0])
        _bend_z,  bend_edge = y_parallel_edges[0]   # lowest Z = bend-line
        _tip_z,   _tip_edge = y_parallel_edges[-1]  # highest Z = free tip

        # t_z must point FROM bend-line TOWARD tip (increasing world Z)
        if t_z.z < 0:
            t_z = t_z * -1.0

        # origin_pt = the bend-line vertex closest to Y=0 (front face)
        verts = bend_edge.Vertexes
        origin_pt = min((v.Point for v in verts), key=lambda p: p.y)
    else:
        print("[map_and_cut_right_flange_batch] WARNING: no Y-parallel edges found; using UV fallback.")
        u_min_p, u_max_p, v_min_p, v_max_p = right_face.ParameterRange
        origin_pt = right_face.valueAt(u_min_p, v_min_p)

    print(f"[map_and_cut_right_flange_batch] face_origin={origin_pt}, "
          f"t_x=({t_x.x:.3f},{t_x.y:.3f},{t_x.z:.3f}), "
          f"t_y=({t_y.x:.3f},{t_y.y:.3f},{t_y.z:.3f}), "
          f"t_z=({t_z.x:.3f},{t_z.y:.3f},{t_z.z:.3f})")

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 3 — Place ALL tools with a single shared placement matrix
    #
    # Tool convention: pnt.x = dim_x - thickness, dir=(1,0,0)
    # The matrix must compensate for the X offset (dim_x - thickness) so that
    # local (dim_x-thickness, cy, cz) → world = origin_pt - t_x*OFFSET + t_y*cy + t_z*(cz - bend_deduction)
    #
    # Derivation:  world = start_pos + t_x*v.x + t_y*v.y + t_z*v.z
    #   We want:   origin_pt - t_x*OFFSET + t_y*cy + t_z*(cz - bend_deduction)
    #   With v.x = dim_x - thickness:
    #     start_pos = origin_pt - t_x*OFFSET - t_z*bend_deduction - t_x*(dim_x - thickness)
    #               = origin_pt - t_x*(OFFSET + dim_x - thickness) - t_z*bend_deduction   ✓
    OFFSET    = 2.0
    x_offset  = dim_x - thickness
    start_pos = origin_pt - t_x * (OFFSET + x_offset) - t_z * bend_deduction

    mat = App.Matrix()
    mat.A11 = t_x.x;  mat.A12 = t_y.x;  mat.A13 = t_z.x;  mat.A14 = start_pos.x
    mat.A21 = t_x.y;  mat.A22 = t_y.y;  mat.A23 = t_z.y;  mat.A24 = start_pos.y
    mat.A31 = t_x.z;  mat.A32 = t_y.z;  mat.A33 = t_z.z;  mat.A34 = start_pos.z
    mat.A44 = 1.0
    # NOTE: We use transformShape(mat) instead of Placement = App.Placement(mat)
    # because Placement decomposes the 4x4 matrix into quaternion + translation.
    # When the rotation sub-matrix has a negative determinant (as happens for the
    # right-flange frame which is a reflection of the left-flange frame),
    # the quaternion decomposition produces INCORRECT results.
    # transformShape() directly applies the matrix to all vertices — always correct.

    for entry in tools:
        tool_shape    = entry["tool_shape"]
        hole_center_y = entry["hole_center_y"]
        hole_center_z = entry["hole_center_z"]

        tool_shape.transformShape(mat)

        try:
            bracket_shape = bracket_shape.cut(tool_shape)
            print(f"[map_and_cut_right_flange_batch] cut OK  "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f})")
        except Exception as exc:
            print(f"[map_and_cut_right_flange_batch] cut FAILED "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f}): {exc}")

    return bracket_shape


def add_hole_right_flange(bracket_shape, hole_radius, hole_center_y, hole_center_z, angle_deg, dim_y, thickness, flange_length, bend_radius):
    """
    Add hole on the RIGHT flange of a U-shaped bracket.
    Based on add_hole_leg2 but with reversed X normal direction to target the right flange (at X=dim_x side).

    For U-shape: add_hole_leg2 works for the LEFT flange (X=0 side, normal points toward -X).
    This function targets the RIGHT flange (X=dim_x side, normal points toward +X).

    Parameters:
    - bracket_shape: Current bracket shape
    - hole_radius: Hole radius
    - hole_center_y: Hole center position along Y axis on the flange plate (from 0 to dim_y).
                     This is a position on the flange plate used as reference origin, not an exact 3D coordinate.
    - hole_center_z: Hole center position along Z axis on the flange plate (from 0 to flange_length, including bend).
                     This is a position on the flange plate used as reference origin, not an exact 3D coordinate.
    - angle_deg: Bend angle (typically 90°)
    - dim_y: Extrude length (Y dimension)
    - thickness: Sheet thickness
    - flange_length: Total length of the right flange (including bend section)
    - bend_radius: Radius of the bend
    """

    # Calculate bend deduction (bend portion that should not be included in hole positioning)
    bend_deduction = bend_radius + thickness

    # Adjust hole_center_z to account for bend offset
    adjusted_hole_z = hole_center_z - bend_deduction

    print(f"[Right Flange] Original hole_center_z: {hole_center_z}, Bend deduction: {bend_deduction}, Adjusted: {adjusted_hole_z}")

    # Find right flange face
    right_flange_face = None
    max_area = 0

    # Calculate expected normal vector for RIGHT flange face
    # Opposite X direction compared to left flange / leg2 → POSITIVE X
    angle_rad = math.radians(angle_deg)
    expected_normal_x = math.sin(angle_rad)   # POSITIVE (points outward toward +X)
    expected_normal_z = math.cos(angle_rad)
    expected_normal_y = 0.0
    # FIX: math.cos/sin of "nice" angles carry floating-point error
    #      (e.g. cos(90°) ≈ 6.123e-17 instead of 0.0). Round < 1e-10 to 0.
    if abs(expected_normal_x) < 1e-10: expected_normal_x = 0.0
    if abs(expected_normal_y) < 1e-10: expected_normal_y = 0.0
    if abs(expected_normal_z) < 1e-10: expected_normal_z = 0.0

    print(f"Looking for right flange face with expected normal: ({expected_normal_x:.3f}, {expected_normal_y:.3f}, {expected_normal_z:.3f})")

    # ── Candidate collection (mirrors add_hole_leg2 pattern) ─────────────────
    # FIX: removed min(score, score_flip) — the flip allowed left flange (n.x<0)
    #      to score 0 against expected (+X), making it indistinguishable from
    #      the right flange outer face.
    # FIX: hard guard — skip any face pointing toward -X (left flange side).
    # FIX: collect ALL candidates then pick one with LARGEST center_x
    #      (rightmost = right outer face at x≈dim_x) so that the left inner face
    #      (n.x>0, center_x≈thickness) cannot win on a floating-point tie.
    tolerance = 0.35
    candidates = []  # list of (score, center_x, face)

    for face in bracket_shape.Faces:
        u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2
        v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2
        normal = face.normalAt(u_mid, v_mid)

        # Hard guard: skip any face pointing toward -X (belongs to left flange)
        if normal.x < 0:
            continue

        score = abs(normal.x - expected_normal_x) + abs(normal.y - expected_normal_y) + abs(normal.z - expected_normal_z)
        min_area = flange_length * dim_y * 0.5

        if score < tolerance and face.Area > min_area:
            center = face.valueAt(u_mid, v_mid)
            candidates.append((score, center.x, face))
            print(f"  [right] Candidate: area={face.Area:.1f}, n=({normal.x:.3f},{normal.y:.3f},{normal.z:.3f}), score={score:.4f}, center_x={center.x:.3f}")

    # Pick best score first, then LARGEST center_x (rightmost = right outer flange)
    # Using -center_x in sort key so largest center_x appears at index 0.
    if candidates:
        candidates.sort(key=lambda c: (round(c[0], 8), -c[1]))
        right_flange_face = candidates[0][2]
        print(f"  [right] Selected: score={candidates[0][0]:.4f}, center_x={candidates[0][1]:.3f}")

    # Fallback: find largest face with positive X normal component (right side)
    if right_flange_face is None:
        print("Could not find right flange face by normal vector, using fallback method")
        for face in bracket_shape.Faces:
            normal = face.normalAt(0, 0)
            # Look for faces pointing toward +X (right side), skip horizontal and purely vertical-Z faces
            if (abs(normal.z) < 0.9 and normal.x > 0.1 and
                face.Area > max_area and face.Area > flange_length * dim_y * 0.3):
                max_area = face.Area
                right_flange_face = face
                print(f"Fallback: selected face with area={face.Area:.1f}, normal=({normal.x:.3f}, {normal.y:.3f}, {normal.z:.3f})")

    if right_flange_face is None:
        print("Could not find suitable right flange face")
        return bracket_shape

    print(f"Found right flange face with area: {right_flange_face.Area}")

    # Find center point of face for reference
    u_mid = (right_flange_face.ParameterRange[0] + right_flange_face.ParameterRange[1]) / 2
    v_mid = (right_flange_face.ParameterRange[2] + right_flange_face.ParameterRange[3]) / 2
    face_center = right_flange_face.valueAt(u_mid, v_mid)
    normal = right_flange_face.normalAt(u_mid, v_mid)

    print(f"Center point of right flange: {face_center}")
    print(f"Normal vector of right flange: {normal}")

    # Get face bounds in parameter space
    u_min, u_max, v_min, v_max = right_flange_face.ParameterRange

    # Test corner points to understand face parameter mapping
    corner_points = [
        right_flange_face.valueAt(u_min, v_min),
        right_flange_face.valueAt(u_max, v_min),
        right_flange_face.valueAt(u_min, v_max),
        right_flange_face.valueAt(u_max, v_max)
    ]

    print(f"Face corner points:")
    print(f"  (u_min, v_min): {corner_points[0]}")
    print(f"  (u_max, v_min): {corner_points[1]}")
    print(f"  (u_min, v_max): {corner_points[2]}")
    print(f"  (u_max, v_max): {corner_points[3]}")

    # Determine which parameter direction corresponds to Y (length) and Z (height)
    y_variation_u = abs(corner_points[1].y - corner_points[0].y)
    y_variation_v = abs(corner_points[2].y - corner_points[0].y)

    z_variation_u = abs(corner_points[1].z - corner_points[0].z)
    z_variation_v = abs(corner_points[2].z - corner_points[0].z)

    print(f"Parameter variations: Y_u={y_variation_u:.3f}, Y_v={y_variation_v:.3f}, Z_u={z_variation_u:.3f}, Z_v={z_variation_v:.3f}")

    # Calculate the effective flange_length (excluding bend portion)
    effective_flange_length = flange_length - bend_deduction

    # Determine correct parameter mapping
    if y_variation_u > y_variation_v:
        u_normalized = hole_center_y / dim_y
        v_normalized = adjusted_hole_z / effective_flange_length
        print(f"Mapping: U->Y (length), V->Z (height), effective_flange_length={effective_flange_length}")
    else:
        u_normalized = adjusted_hole_z / effective_flange_length
        v_normalized = hole_center_y / dim_y
        print(f"Mapping: U->Z (height), V->Y (length), effective_flange_length={effective_flange_length}")

    # Map to face parameter range
    u_param = u_min + u_normalized * (u_max - u_min)
    v_param = v_min + v_normalized * (v_max - v_min)

    # Get the actual 3D position on the face surface
    hole_position = right_flange_face.valueAt(u_param, v_param)

    print(f"Face parameter range: U({u_min:.3f}, {u_max:.3f}), V({v_min:.3f}, {v_max:.3f})")
    print(f"Normalized position: U={u_normalized:.3f}, V={v_normalized:.3f}")
    print(f"Face parameters: U={u_param:.3f}, V={v_param:.3f}")
    print(f"Hole position on face: {hole_position}")

    # Get the normal at this position for cylinder direction
    normal = right_flange_face.normalAt(u_param, v_param)

    # FIX: cylinder_height = thickness + 2 guarantees the cut fully passes through
    #      the flange regardless of floating-point positioning.
    # FIX: start from OUTSIDE the face (subtract full depth along normal) so the
    #      cylinder enters from the outer surface and exits cleanly through the inner.
    cut_depth = thickness + 2.0
    cylinder_start = hole_position - normal * cut_depth

    hole_cylinder = Part.makeCylinder(
        hole_radius,
        cut_depth,
        cylinder_start,
        normal
    )

    # Create cylinder object for debugging (optional)
    if App.ActiveDocument:
        hole_obj = App.ActiveDocument.addObject("Part::Feature", "HoleRightFlangeDebug")
        hole_obj.Shape = hole_cylinder
        hole_obj.Label = "Hole Cylinder Right Flange"
        print(f"Created cylinder with cut_depth: {cut_depth}")
        print(f"Cylinder start position: {cylinder_start}")
        print(f"Cylinder direction (normal): {normal}")

    try:
        result_shape = bracket_shape.cut(hole_cylinder)
        print("Successfully created hole on right flange")
        return result_shape
    except Exception as e:
        print(f"Error creating hole on right flange: {e}")
        return bracket_shape


def add_hole_bottom_flange(bracket_shape, hole_radius, hole_center_y, hole_center_z, angle_deg, dim_y, thickness, flange_length, bend_radius):
    """
    Add hole on the BOTTOM flange of a Z-shaped bracket.
    Based on add_hole_leg2 but with reversed normal direction to target the bottom flange
    (which bends downward, opposite to the top flange).

    For Z-shape: add_hole_leg2 works for the TOP flange (bends upward, normal toward -X, +Z).
    This function targets the BOTTOM flange (bends downward/inverted, normal toward +X, -Z).

    Parameters:
    - bracket_shape: Current bracket shape
    - hole_radius: Hole radius
    - hole_center_y: Hole center position along Y axis on the flange plate (from 0 to dim_y).
                     This is a position on the flange plate used as reference origin, not an exact 3D coordinate.
    - hole_center_z: Hole center position along Z axis on the flange plate (from 0 to flange_length, including bend).
                     This is a position on the flange plate used as reference origin, not an exact 3D coordinate.
                     Note: Z is measured along the flange surface, not in absolute 3D coordinates.
    - angle_deg: Bend angle (typically 90°)
    - dim_y: Extrude length (Y dimension)
    - thickness: Sheet thickness
    - flange_length: Total length of the bottom flange (including bend section)
    - bend_radius: Radius of the bend
    """

    # Calculate bend deduction (bend portion that should not be included in hole positioning)
    bend_deduction = bend_radius + thickness

    # Adjust hole_center_z to account for bend offset
    adjusted_hole_z = hole_center_z - bend_deduction

    print(f"[Bottom Flange] Original hole_center_z: {hole_center_z}, Bend deduction: {bend_deduction}, Adjusted: {adjusted_hole_z}")

    # Find bottom flange face
    bottom_flange_face = None
    max_area = 0
    best_match_score = float('inf')

    # Calculate expected normal vector for BOTTOM flange face
    # Bottom flange bends downward (inverted), so normal direction is opposite to top flange:
    # Top flange:    normal_x = -sin(angle), normal_z = +cos(angle)
    # Bottom flange: normal_x = +sin(angle), normal_z = -cos(angle)
    angle_rad = math.radians(angle_deg)
    expected_normal_x = math.sin(angle_rad)    # POSITIVE (opposite of top flange)
    expected_normal_z = -math.cos(angle_rad)   # NEGATIVE (bends downward)
    expected_normal_y = 0.0

    print(f"Looking for bottom flange face with expected normal: ({expected_normal_x:.3f}, {expected_normal_y:.3f}, {expected_normal_z:.3f})")

    # ── Primary search: direct match, NO flip-normal ──────────────────────────
    # FIX: removed min(score, score_flip) — the flip allowed top flange (-X)
    #      to also score 0, making face selection non-deterministic.
    tolerance = 0.35

    for face in bracket_shape.Faces:
        u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2
        v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2
        n = face.normalAt(u_mid, v_mid)

        # FIX: hard guard — skip any face pointing toward -X (top flange / leg2 side)
        if n.x < 0:
            continue

        score = abs(n.x - expected_normal_x) + abs(n.y - expected_normal_y) + abs(n.z - expected_normal_z)
        min_area = flange_length * dim_y * 0.5

        if score < tolerance and face.Area > min_area and score < best_match_score:
            best_match_score = score
            bottom_flange_face = face
            print(f"  [bottom] Candidate: area={face.Area:.1f}, n=({n.x:.3f},{n.y:.3f},{n.z:.3f}), score={score:.4f}")

    # ── Fallback: largest face with strictly positive X normal ────────────────
    # FIX: require n.x > 0.1 (positive), to exclude top flange.
    if bottom_flange_face is None:
        print("[add_hole_bottom_flange] Primary search failed — fallback (positive-X faces only)")
        for face in bracket_shape.Faces:
            n = face.normalAt(0, 0)
            if (n.x > 0.1 and              # strictly right / +X direction (bottom flange)
                abs(n.z) < 0.9 and          # not a horizontal face
                face.Area > max_area and
                face.Area > flange_length * dim_y * 0.3):
                max_area = face.Area
                bottom_flange_face = face
                print(f"  [bottom] Fallback: area={face.Area:.1f}, n=({n.x:.3f},{n.y:.3f},{n.z:.3f})")

    if bottom_flange_face is None:
        print("Could not find suitable bottom flange face")
        return bracket_shape

    print(f"Found bottom flange face with area: {bottom_flange_face.Area}")

    # Find center point of face for reference
    u_mid = (bottom_flange_face.ParameterRange[0] + bottom_flange_face.ParameterRange[1]) / 2
    v_mid = (bottom_flange_face.ParameterRange[2] + bottom_flange_face.ParameterRange[3]) / 2
    face_center = bottom_flange_face.valueAt(u_mid, v_mid)
    normal = bottom_flange_face.normalAt(u_mid, v_mid)

    print(f"Center point of bottom flange: {face_center}")
    print(f"Normal vector of bottom flange: {normal}")

    # Get face bounds in parameter space
    u_min, u_max, v_min, v_max = bottom_flange_face.ParameterRange

    # Test corner points to understand face parameter mapping
    corner_points = [
        bottom_flange_face.valueAt(u_min, v_min),
        bottom_flange_face.valueAt(u_max, v_min),
        bottom_flange_face.valueAt(u_min, v_max),
        bottom_flange_face.valueAt(u_max, v_max)
    ]

    print(f"Face corner points:")
    print(f"  (u_min, v_min): {corner_points[0]}")
    print(f"  (u_max, v_min): {corner_points[1]}")
    print(f"  (u_min, v_max): {corner_points[2]}")
    print(f"  (u_max, v_max): {corner_points[3]}")

    # Determine which parameter direction corresponds to Y (length) and Z (height)
    y_variation_u = abs(corner_points[1].y - corner_points[0].y)
    y_variation_v = abs(corner_points[2].y - corner_points[0].y)

    z_variation_u = abs(corner_points[1].z - corner_points[0].z)
    z_variation_v = abs(corner_points[2].z - corner_points[0].z)

    print(f"Parameter variations: Y_u={y_variation_u:.3f}, Y_v={y_variation_v:.3f}, Z_u={z_variation_u:.3f}, Z_v={z_variation_v:.3f}")

    # Calculate the effective flange_length (excluding bend portion)
    effective_flange_length = flange_length - bend_deduction

    # Determine correct parameter mapping
    if y_variation_u > y_variation_v:
        u_normalized = hole_center_y / dim_y
        v_normalized = adjusted_hole_z / effective_flange_length
        print(f"Mapping: U->Y (length), V->Z (height), effective_flange_length={effective_flange_length}")
    else:
        u_normalized = adjusted_hole_z / effective_flange_length
        v_normalized = hole_center_y / dim_y
        print(f"Mapping: U->Z (height), V->Y (length), effective_flange_length={effective_flange_length}")

    # Map to face parameter range
    u_param = u_min + u_normalized * (u_max - u_min)
    v_param = v_min + v_normalized * (v_max - v_min)

    # Get the actual 3D position on the face surface
    hole_position = bottom_flange_face.valueAt(u_param, v_param)

    print(f"Face parameter range: U({u_min:.3f}, {u_max:.3f}), V({v_min:.3f}, {v_max:.3f})")
    print(f"Normalized position: U={u_normalized:.3f}, V={v_normalized:.3f}")
    print(f"Face parameters: U={u_param:.3f}, V={v_param:.3f}")
    print(f"Hole position on face: {hole_position}")

    # Get the normal at this position for cylinder direction
    normal = bottom_flange_face.normalAt(u_param, v_param)

    # FIX: cylinder_height = thickness + 2 guarantees the cut fully passes through
    #      the flange regardless of floating-point positioning.
    # FIX: start from OUTSIDE the face (subtract full depth along normal) so the
    #      cylinder enters from the outer surface and exits cleanly through the inner.
    cut_depth = thickness + 2.0
    cylinder_start = hole_position - normal * cut_depth

    hole_cylinder = Part.makeCylinder(
        hole_radius,
        cut_depth,
        cylinder_start,
        normal
    )

    # Create cylinder object for debugging (optional)
    if App.ActiveDocument:
        hole_obj = App.ActiveDocument.addObject("Part::Feature", "HoleBottomFlangeDebug")
        hole_obj.Shape = hole_cylinder
        hole_obj.Label = "Hole Cylinder Bottom Flange"
        print(f"Created cylinder with cut_depth: {cut_depth}")
        print(f"Cylinder start position: {cylinder_start}")
        print(f"Cylinder direction (normal): {normal}")

    try:
        result_shape = bracket_shape.cut(hole_cylinder)
        print("Successfully created hole on bottom flange")
        return result_shape
    except Exception as e:
        print(f"Error creating hole on bottom flange: {e}")
        return bracket_shape


def map_and_cut_bottom_flange_batch(bracket_shape, tools, angle_deg, dim_x, dim_y, thickness, flange_height, bend_radius):
    """
    Batch function: finds the BOTTOM flange face of a Z-bracket ONCE, then maps and
    cuts all tool shapes in a single pass, automatically handling any bend angle.

    This is the Z-bracket bottom-flange counterpart of map_and_cut_right_flange_batch.

    ── HOW TO CREATE TOOLS (90° natural-coordinate convention) ──────────────────
    Build every tool as if the bottom flange were a standard VERTICAL 90° wall at
    X = dim_x (inner face = dim_x − thickness):
      • The cut direction is +X  (dir = App.Vector(1, 0, 0))
      • pnt.x = dim_x - thickness   (inner face of the 90° bottom wall)
      • pnt.y = hole_center_y + size_y / 2   (ADD — Y runs in −Y from pnt with dir=(1,0,0))
      • pnt.z = hole_center_z − size_z / 2   (SUBTRACT — Z runs in the flange direction from pnt)

    Where hole_center_y and hole_center_z use the bottom-flange coordinate system:
      • hole_center_y  ∈ [0, dim_y]           — along the extrusion length
      • hole_center_z  ∈ [0, flange_height]   — distance from bend line along flange surface

    Examples:
      x0 = dim_x - thickness   # bottom flange inner-face X position at 90°

      # Circle centered at (cy, cz):
      tool = Part.makeCylinder(r, thickness + 2.0,
                               App.Vector(x0, cy, cz), App.Vector(1, 0, 0))

      # Box/Rectangle (sz=Z-extent, sy=Y-extent):
      tool = Part.makeBox(sz, sy, thickness + 2.0,
                          App.Vector(x0, cy + sy/2, cz - sz/2), App.Vector(1, 0, 0))

      # Oblong (sz=Z-extent, sy=Y-extent):
      tool = Part.makeOblong(sz, sy, thickness + 2.0,
                             App.Vector(x0, cy + sy/2, cz - sz/2), App.Vector(1, 0, 0))

    ⚡ NO 90° FAST-PATH for bottom flange (unlike top/right flange).
    At 90° the bottom flange's free tip is at world Z < 0 (downward), so tools
    built with natural pnt.z ∈ [0, flange_height] are NOT in world coordinates.
    The matrix transform below is ALWAYS applied, even at exactly 90°.

    ── IMPORTANT ───────────────────────────────────────────────────────────────
    • Each tool must be a SEPARATE instance (transformShape modifies in-place).
    • hole_center_y / hole_center_z in the dict are used ONLY for logging;
      the tool's actual position is already encoded in its pnt.

    ── How it works (geometry) ────────────────────────────────────────────────
    Step 1 — Find the outer bottom-flange face:
             Z-bracket bottom flange outer normal = (+sinθ, 0, −cosθ)
             At 90°:  (1, 0, 0).  At 120°: (0.866, 0, 0.5)
    Step 2 — Build local frame from the face's actual Y-parallel edges:
               t_x = −bottom_normal  (inward cut direction)
               t_y = global +Y
               t_z = t_x × t_y, flipped so t_z.z < 0 (points from bend toward tip = downward)
             origin_pt = vertex on bend-line edge at Y=0.
    Step 3 — One shared matrix maps all tools:
               start_pos = origin_pt − t_x×(OFFSET + dim_x − thickness) − t_z×bend_deduction
             A tool at local (dim_x−thickness, cy, cz) arrives at:
               origin_pt + t_y×cy + t_z×(cz − bend_deduction) − t_x×OFFSET  ✓

    Parameters
    ----------
    bracket_shape   : Part.Shape  — working solid
    tools           : list of {"tool_shape", "hole_center_y", "hole_center_z"}
    angle_deg       : float  — bend angle in degrees (e.g. 90, 110, 120)
    dim_x           : float  — web height of Z-bracket along X (mm)
    dim_y           : float  — extrusion length along Y
    thickness       : float  — sheet thickness
    flange_height   : float  — total bottom-flange height (from base z=0)
    bend_radius     : float  — inner bend radius

    Returns
    -------
    Part.Shape with all tools cut out.
    """
    import FreeCAD as App
    import math

    # NOTE: NO 90° fast-path for bottom flange!
    # Unlike top/right flanges (Z axis goes UP = positive), the bottom flange Z axis
    # goes DOWNWARD (negative in world space) even at 90°.
    # Tools built with natural coords (pnt.z ∈ [0, flange_height], positive) are NOT
    # in world coordinates for the bottom flange — they would land inside the web.
    # The matrix transform in STEP 2–3 below handles the flip correctly for all angles.
    # ─────────────────────────────────────────────────────────────────────────

    bend_deduction = bend_radius + thickness

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 1 — Find the outer face of the bottom flange
    # ═══════════════════════════════════════════════════════════════════════
    angle_rad = math.radians(angle_deg)
    sin_a = math.sin(angle_rad)
    cos_a = math.cos(angle_rad)

    def _snap0(v):
        return 0.0 if abs(v) < 1e-10 else v

    # Bottom flange outer normal at angle θ: (+sinθ, 0, −cosθ)
    # At 90°: (1, 0, 0).  At 120°: (0.866, 0, 0.5).
    # Note: −cosθ because the bottom flange bends DOWNWARD (opposite to top flange).
    candidate_normals = [
        App.Vector(_snap0(sin_a),  0.0, _snap0(-cos_a)),   # bottom outer face (primary)
        App.Vector(_snap0(-sin_a), 0.0, _snap0(-cos_a)),   # symmetric fallback
    ]

    SCORE_TOL = 0.35
    MIN_AREA  = flange_height * dim_y * 0.45

    best_score    = float("inf")
    best_cx       = -float("inf")   # tie-breaker: pick rightmost (largest center_x = dim_x side)
    bottom_face   = None
    bottom_normal = None

    for face in bracket_shape.Faces:
        if face.Area < MIN_AREA:
            continue

        u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2
        v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2
        n     = face.normalAt(u_mid, v_mid)

        # Hard guard: bottom flange outer normal has n.x > 0 (bends from X=dim_x side)
        # Skip faces with n.x < 0 (those belong to top flange / leg2)
        if n.x < 0:
            continue

        center = face.valueAt(u_mid, v_mid)

        for n_ref in candidate_normals:
            score = abs(n.x - n_ref.x) + abs(n.y - n_ref.y) + abs(n.z - n_ref.z)
            if score < SCORE_TOL:
                if score < best_score - 1e-6 or (abs(score - best_score) < 1e-6 and center.x > best_cx):
                    best_score    = score
                    best_cx       = center.x
                    bottom_face   = face
                    bottom_normal = n_ref

    # ── Fallback ──────────────────────────────────────────────────────────────
    if bottom_face is None:
        print("[map_and_cut_bottom_flange_batch] WARNING: normal-match failed; using area fallback.")
        max_area = 0.0
        for face in bracket_shape.Faces:
            if face.Area <= max_area:
                continue
            n = face.normalAt(
                (face.ParameterRange[0] + face.ParameterRange[1]) / 2,
                (face.ParameterRange[2] + face.ParameterRange[3]) / 2,
            )
            # Look for large face with n.x > 0 and n.z < 0 (bottom flange bending down)
            if abs(n.y) < 0.05 and n.x > 0.05 and face.Area > flange_height * dim_y * 0.3:
                max_area      = face.Area
                bottom_face   = face
                bottom_normal = n

    if bottom_face is None:
        print("[map_and_cut_bottom_flange_batch] ERROR: Could not find bottom flange face — no cuts applied.")
        return bracket_shape

    print(f"[map_and_cut_bottom_flange_batch] Found face: area={bottom_face.Area:.1f}, "
          f"n_ref=({bottom_normal.x:.3f},{bottom_normal.y:.3f},{bottom_normal.z:.3f}), score={best_score:.4f}")

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 2 — Build a stable 3-D local frame from the face's actual geometry
    #
    #   t_x = inward-cut direction = −bottom_normal
    #   t_y = global +Y (extrusion axis)
    #   t_z = "down the flange" = t_x × t_y, flipped so t_z.z < 0
    #         (points from bend-line TOWARD the tip, which is downward for bottom flange)
    # ═══════════════════════════════════════════════════════════════════════
    t_x = App.Vector(-bottom_normal.x, -bottom_normal.y, -bottom_normal.z).normalize()
    t_y = App.Vector(0.0, 1.0, 0.0)

    t_z_raw = t_x.cross(t_y)
    if t_z_raw.Length < 1e-9:
        t_z = App.Vector(0.0, 0.0, -1.0)  # default downward for bottom flange
    else:
        t_z = t_z_raw.normalize()

    # ── Locate bend-line edge and face origin via edge geometry ────────────
    # Collect Y-parallel edges on the face (Δx≈0, Δz≈0, span≈dim_y).
    tol_edge = 1.0
    y_parallel_edges = []
    for edge in bottom_face.Edges:
        verts = edge.Vertexes
        if len(verts) < 2:
            continue
        a = verts[0].Point
        b = verts[1].Point
        if abs(a.x - b.x) > tol_edge or abs(a.z - b.z) > tol_edge:
            continue
        if abs(abs(a.y - b.y) - dim_y) > tol_edge:
            continue
        z_mid = (a.z + b.z) / 2.0
        y_parallel_edges.append((z_mid, edge))

    if y_parallel_edges:
        y_parallel_edges.sort(key=lambda e: e[0])
        # For bottom flange: highest Z = bend-line (closest to web), lowest Z = free tip
        _bend_z,  _bend_edge = y_parallel_edges[-1]   # highest Z = bend-line
        _tip_z,   _tip_edge  = y_parallel_edges[0]    # lowest Z = free tip

        bend_edge = _bend_edge

        # t_z must point FROM bend-line TOWARD tip (decreasing world Z for bottom flange)
        if t_z.z > 0:
            t_z = t_z * -1.0

        # origin_pt = the bend-line vertex closest to Y=0 (front face)
        verts = bend_edge.Vertexes
        origin_pt = min((v.Point for v in verts), key=lambda p: p.y)
    else:
        print("[map_and_cut_bottom_flange_batch] WARNING: no Y-parallel edges found; using UV fallback.")
        u_min_p, u_max_p, v_min_p, v_max_p = bottom_face.ParameterRange
        origin_pt = bottom_face.valueAt(u_min_p, v_max_p)  # highest Z approx

    print(f"[map_and_cut_bottom_flange_batch] face_origin={origin_pt}, "
          f"t_x=({t_x.x:.3f},{t_x.y:.3f},{t_x.z:.3f}), "
          f"t_y=({t_y.x:.3f},{t_y.y:.3f},{t_y.z:.3f}), "
          f"t_z=({t_z.x:.3f},{t_z.y:.3f},{t_z.z:.3f})")

    # ═══════════════════════════════════════════════════════════════════════
    # STEP 3 — Place ALL tools with a single shared transformation matrix
    #
    # Tool convention: pnt.x = dim_x - thickness, dir=(1,0,0)
    # Same derivation as map_and_cut_right_flange_batch:
    #   start_pos = origin_pt - t_x*(OFFSET + dim_x - thickness) - t_z*bend_deduction
    # so that a tool at local (dim_x-thickness, cy, cz) maps to:
    #   origin_pt - t_x*OFFSET + t_y*cy + t_z*(cz - bend_deduction)   ✓
    #
    # NOTE: We use transformShape(mat) instead of Placement = App.Placement(mat)
    # because Placement decomposes the 4x4 matrix into quaternion + translation.
    # When the rotation sub-matrix has a negative determinant (as happens for the
    # bottom-flange frame which is a reflection), the quaternion decomposition
    # produces INCORRECT results. transformShape() directly applies the matrix
    # to all vertices — always correct.
    # ═══════════════════════════════════════════════════════════════════════
    OFFSET    = 2.0
    x_offset  = dim_x - thickness
    start_pos = origin_pt - t_x * (OFFSET + x_offset) - t_z * bend_deduction

    mat = App.Matrix()
    mat.A11 = t_x.x;  mat.A12 = t_y.x;  mat.A13 = t_z.x;  mat.A14 = start_pos.x
    mat.A21 = t_x.y;  mat.A22 = t_y.y;  mat.A23 = t_z.y;  mat.A24 = start_pos.y
    mat.A31 = t_x.z;  mat.A32 = t_y.z;  mat.A33 = t_z.z;  mat.A34 = start_pos.z
    mat.A44 = 1.0

    for entry in tools:
        tool_shape    = entry["tool_shape"]
        hole_center_y = entry["hole_center_y"]
        hole_center_z = entry["hole_center_z"]

        tool_shape.transformShape(mat)

        try:
            bracket_shape = bracket_shape.cut(tool_shape)
            print(f"[map_and_cut_bottom_flange_batch] cut OK  "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f})")
        except Exception as exc:
            print(f"[map_and_cut_bottom_flange_batch] cut FAILED "
                  f"(cy={hole_center_y:.1f}, cz={hole_center_z:.1f}): {exc}")

    return bracket_shape
