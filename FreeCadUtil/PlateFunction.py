# PlateFunction.py - Functions related to plate operations
# -*- coding: utf-8 -*-
import FreeCAD as App
import Part
import math
import sys
import os

# Conditional sheetmetal import - only import when needed
# This allows makeOblong, makeHalfCylinder, makeHexagon, makeThreaded to work without sheetmetal
def _ensure_sheetmetal():
    """Import sheetmetal only when needed"""
    global SheetMetalCmd, SheetMetalBaseShapeCmd, SheetMetalTools
    try:
        # Add sheetmetal directory to path (it's at project root level)
        sheetmetal_path = os.path.join(os.path.dirname(__file__), '..', 'sheetmetal')
        if sheetmetal_path not in sys.path:
            sys.path.insert(0, sheetmetal_path)
        
        from sheetmetal import SheetMetalCmd, SheetMetalBaseShapeCmd
        import SheetMetalTools
        
        def taskRestoreDefaults(obj, default_vars):
            pass
        SheetMetalTools.taskRestoreDefaults = taskRestoreDefaults
        return True
    except ImportError as e:
        print(f"Warning: SheetMetal module not available: {e}")
        return False


def makeUShape(
    dim_x=60.0,
    dim_y=120.0,
    thickness=2.0,
    flange_height_left=20.0,
    flange_height_right=20.0,
    bend_angle_deg_left=90.0,
    bend_angle_deg_right=90.0,
    bend_radius=2.0,
):
    """
    Create a U-shaped plate with flanges along Y-axis (on X=0 and X=dim_x edges).

    Parameters:
    - dim_x: Dimension of the plate along X-axis
    - dim_y: Dimension of the plate along Y-axis
    - thickness: Plate thickness (Z dimension)
    - flange_height_left: Height of the left bent flange (at X=0)
    - flange_height_right: Height of the right bent flange (at X=dim_x)
    - bend_angle_deg: (Deprecated) Common bend angle for both flanges.
                      If provided, overrides bend_angle_deg_left and bend_angle_deg_right.
    - bend_radius: Bend radius
    - bend_angle_deg_left: Bend angle in degrees for the LEFT flange (at X=0). Default 90°.
    - bend_angle_deg_right: Bend angle in degrees for the RIGHT flange (at X=dim_x). Default 90°.

    Returns:
    - final_obj: The final FreeCAD object with the U-shaped plate
    """
    # Ensure sheetmetal is available
    if not _ensure_sheetmetal():
        raise ImportError("SheetMetal module is required for makeUShape function")

    # Create document with auto-generated name
    doc_name = "U_Shaped_Plate_Y_Axis"
    doc = App.newDocument(doc_name)

    # Subtract bend zone (bend_radius + thickness) from both sides of the base box,
    # matching the same logic as makeLShape.
    excess = bend_radius + thickness
    base_shape = Part.makeBox(dim_x - 2 * excess, dim_y, thickness, App.Vector(excess, 0, 0))
    base_obj = doc.addObject("Part::Feature", "BasePlate")
    base_obj.Shape = base_shape
    doc.recompute()

    # Bend along Y-axis (flanges on X=0 and X=dim_x edges)
    edge1 = "Edge2"   # Left edge (X=0)
    edge2 = "Edge6"   # Right edge (X=dim_x)

    print("Creating U-shaped plate with flanges along Y-axis")

    # Create first flange (left) — uses bend_angle_deg_left
    flange1_obj = doc.addObject("Part::FeaturePython", "SMBendWall1")
    SheetMetalCmd.SMBendWall(flange1_obj, base_obj, [edge1])
    flange1_obj.length = flange_height_left - excess
    flange1_obj.angle = 180 - bend_angle_deg_left  # SheetMetal uses supplementary angle
    flange1_obj.radius = bend_radius

    # Create second flange (right) — uses bend_angle_deg_right
    flange2_obj = doc.addObject("Part::FeaturePython", "SMBendWall2")
    SheetMetalCmd.SMBendWall(flange2_obj, base_obj, [edge2])
    flange2_obj.length = flange_height_right - excess
    flange2_obj.angle = 180 - bend_angle_deg_right
    flange2_obj.radius = bend_radius

    doc.recompute()

    # Fuse plate + flanges
    try:
        fused_shape = base_obj.Shape.fuse(flange1_obj.Shape).fuse(flange2_obj.Shape)
        final_obj = doc.addObject("Part::Feature", "U_Shaped_Plate")
        final_obj.Shape = fused_shape
        doc.recompute()
    except Exception as e:
        print(f"Warning: fusing failed - using compound. Reason: {e}")
        compound_shape = Part.makeCompound([base_obj.Shape,
                                            flange1_obj.Shape,
                                            flange2_obj.Shape])
        final_obj = doc.addObject("Part::Feature", "U_Shaped_Plate")
        final_obj.Shape = compound_shape
        doc.recompute()

    return final_obj.Shape

Part.makeUShape = makeUShape

def makeLShape(dim_x, dim_y, thickness, flange_height, bend_angle_deg, bend_radius):
    """
    Creates an L-shaped plate using SheetMetal workbench.

    Args:
        dim_x: X dimension - Length of the base plate
        dim_y: Y dimension - Width of the plate
        thickness: Sheet thickness
        flange_height: Flange / wing height
        bend_angle_deg: 90deg bend angle in degrees
        bend_radius: Radius of the bend

    Returns:
        Part.Shape - The L-shaped plate geometry
    """
    # Ensure sheetmetal is available
    if not _ensure_sheetmetal():
        raise ImportError("SheetMetal module is required for makeLShape function")
    
    # Create document
    doc = App.newDocument("L_Shape_Bracket")

    # Create base box (base plate)
    base_box = Part.makeBox(dim_x - (bend_radius + thickness), dim_y, thickness, App.Vector(bend_radius + thickness, 0, 0))
    base_obj = doc.addObject("Part::Feature", "BaseBox")
    base_obj.Shape = base_box
    base_obj.Label = "Base Box"
    doc.recompute()

    # Create SMBendWall (flange)
    wall_obj = doc.addObject("Part::FeaturePython", "SMBendWall")
    edge = "Edge2"
    wall_feature = SheetMetalCmd.SMBendWall(wall_obj, base_obj, [edge])

    # Set properties for the bend
    wall_obj.length = flange_height - (bend_radius + thickness)
    wall_obj.angle = (180 - bend_angle_deg)
    wall_obj.radius = bend_radius
    doc.recompute()

    # Create final L-bracket by fusing base and wall
    try:
        fused_shape = base_obj.Shape.fuse(wall_obj.Shape)
        final_bracket_obj = doc.addObject("Part::Feature", "L_Bracket")
        final_bracket_obj.Shape = fused_shape
        final_bracket_obj.Label = "L-Bracket"
        doc.recompute()
        print("Successfully created L-shaped bracket")
        return final_bracket_obj.Shape
    except Exception as e:
        print(f"Warning: Could not fuse shapes: {e}")
        # Fallback to compound shape
        bracket_shape = Part.makeCompound([base_obj.Shape, wall_obj.Shape])
        final_bracket_obj = doc.addObject("Part::Feature", "L_Bracket")
        final_bracket_obj.Shape = bracket_shape
        final_bracket_obj.Label = "L-Bracket"
        doc.recompute()
        print("Created L-shaped bracket as compound shape")
        return final_bracket_obj.Shape
Part.makeLShape = makeLShape


def resolve_lbracket_cross_bend_holes(
    base_length, flange_height, bend_radius, thickness,
    reference_edge, edge_distance, spacing, min_edge_distance,
    row_positions, hole_count=None, stop_position=None, cross_bend=True,
    hole_radius=0.0,
):
    """
    Resolve hole center positions for a continuous linear pattern on an
    L-bracket base that may need to continue across the bend onto the
    vertical wall.

    Uses the existing hardcoded bend convention: base bend is at x=0,
    opposite/free edge is at x=dim_x (base_length); vertical wall bend is
    at z=0 (same line), free/top edge is at z=flange_height.

    Args:
        base_length: X extent of the base plate (dim_x).
        flange_height: height of the vertical wall (Z extent).
        bend_radius, thickness: sheet metal parameters. `bend_radius` (the inner
            radius) is the developed arc length the pattern travels while
            wrapping around the fold, so spacing stays continuous across the
            bend. `bend_radius + thickness` is the bend clearance: the flat
            portion of each leg starts this far from its projected bend line
            (matches makeLShape), so no hole may sit closer than that to the
            bend on either leg.
        reference_edge: "far_edge" (measure edge_distance from x=base_length,
            pattern runs toward the bend) or "bend_edge" (measure from x=0,
            pattern runs toward the free edge).
        edge_distance: D, distance from reference_edge to the first hole.
        spacing: E, center-to-center distance between holes.
        min_edge_distance: DFM minimum clearance from the HOLE EDGE (not its
            center) to a FREE edge — LC_02: `center - hole_radius >=
            min_edge_distance`, i.e. pass `thickness`. At a BEND edge the
            required clearance is larger and is derived, not passed:
            `bend_clearance + min_edge_distance` (= `bend_radius + 2*thickness`,
            matching B_05_COMMON "Minimum hole EDGE distance from TC = r + 2t").
            Do NOT pass a bend-specific value here.
        hole_radius: radius of the feature being placed (diameter / 2), so every
            clearance test above is applied to the hole's EDGE rather than its
            center. Defaults to 0.0 (center-only clamping, the pre-fix
            behaviour) — always pass the real radius, otherwise a hole whose
            center clears the limit can still overhang the material boundary.
        row_positions: list of Y coordinates, one per parallel row.
        hole_count: total holes wanted (across base + any vertical
            continuation), or None to run until a boundary is hit.
        stop_position: explicit stop coordinate in the same frame as
            reference_edge (e.g. base_length/2 for "stop in the middle" of
            the base), or None. When given, disables cross-bend entirely.
        cross_bend: whether the pattern may continue across the fold onto the
            vertical wall. Pass False whenever the request restricts the holes
            to the base ("only on the base" / "uniquement sur la base"); then
            `vertical_positions` is ALWAYS empty, so the caller cannot
            accidentally drill the wall even if it still runs its
            map_and_cut_leg2_batch block. True (default) lets a `far_edge`
            pattern with no stop-condition wrap onto the wall.

    Returns:
        {"rows": [{"y": Y, "base_positions": [...], "vertical_positions": [...],
                   "crossing_gap": float | None}, ...]}
    """
    if reference_edge not in ("far_edge", "bend_edge"):
        raise ValueError(f"reference_edge must be 'far_edge' or 'bend_edge', got {reference_edge!r}")

    # Coerce numeric arguments — generated code sometimes passes them as strings
    # (e.g. spacing="60", edge_distance="100"), which crashes the numeric
    # comparisons below ("<= not supported between instances of str and int").
    def _num(v):
        return v if v is None else float(v)

    base_length = _num(base_length)
    flange_height = _num(flange_height)
    bend_radius = _num(bend_radius)
    thickness = _num(thickness)
    edge_distance = _num(edge_distance)
    spacing = _num(spacing)
    min_edge_distance = _num(min_edge_distance)
    hole_radius = _num(hole_radius) or 0.0
    stop_position = _num(stop_position)
    hole_count = None if hole_count is None else int(hole_count)
    row_positions = [float(y) for y in row_positions]

    # Same defensive coercion for cross_bend: generated code may pass the STRING
    # "False"/"None", and bool("False") is True, which would wrongly drill the
    # vertical wall on a base-only request.
    if isinstance(cross_bend, str):
        cross_bend = cross_bend.strip().lower() not in ("false", "none", "0", "no", "")

    if spacing <= 0:
        raise ValueError(f"spacing must be positive, got {spacing!r}")

    # Normalize a degenerate/falsy stop_position (e.g. 0.0) to None. A stop at
    # coordinate 0 is the bend line itself and is never a meaningful stop; some
    # callers emit 0.0 (or a negative value) to mean "no stop". Left as-is, that
    # would silently disable the cross-bend continuation, because the cross_bend
    # guard tests `stop_position is None` (0.0 is not None). This is the source
    # of run-to-run instability where identical requests sometimes drop the
    # vertical-wall holes.
    if stop_position is not None and stop_position <= 0:
        stop_position = None

    rows = []
    for y in row_positions:
        row = _resolve_lbracket_cross_bend_row(
            base_length, flange_height, bend_radius, thickness, reference_edge,
            edge_distance, spacing, min_edge_distance, hole_count, stop_position,
            bool(cross_bend), hole_radius,
        )
        row["y"] = y
        rows.append(row)
    return {"rows": rows}


def _resolve_lbracket_cross_bend_row(
    base_length, flange_height, bend_radius, thickness, reference_edge,
    edge_distance, spacing, min_edge_distance, hole_count, stop_position,
    cross_bend=True, hole_radius=0.0,
):
    # Bend clearance: the flat portion of each leg starts this far from its
    # projected bend line (makeLShape offsets each flat by bend_radius+thickness).
    # No hole may sit closer than this to the bend on either leg.
    bend_clearance = bend_radius + thickness

    # The two clearance limits, both measured to the HOLE EDGE (center -/+ radius):
    #   free edge -> LC_02          : hole_edge >= min_edge_distance
    #   bend edge -> B_05_L/COMMON  : hole_edge >= bend_clearance + min_edge_distance
    # Keeping them as two named values stops the two branches below from drifting
    # apart again (the base-only branch used to clamp the BEND with the free-edge
    # limit, while the cross-bend branch used bend_clearance for the same edge).
    free_edge_limit = min_edge_distance
    bend_edge_limit = bend_clearance + min_edge_distance

    base_positions = []
    vertical_positions = []
    crossing_gap = None

    # ── Base-only cases: crossing explicitly disabled, pattern measured from ──
    # the bend, or a stop-condition given. None of these cross the bend.
    # `cross_bend=False` is authoritative: the wall stays untouched regardless of
    # what the caller does with the (always empty) vertical_positions list.
    if not cross_bend or reference_edge == "bend_edge" or stop_position is not None:
        direction = -1 if reference_edge == "far_edge" else 1
        x = (base_length - edge_distance) if reference_edge == "far_edge" else edge_distance
        while True:
            if hole_count is not None and len(base_positions) >= hole_count:
                break
            if stop_position is not None and (
                (direction == -1 and x < stop_position) or (direction == 1 and x > stop_position)
            ):
                break
            # direction -1 runs from the free edge toward the BEND at x=0;
            # direction +1 runs from the bend toward the FREE edge at x=base_length.
            if direction == -1 and x - hole_radius < bend_edge_limit:
                break
            if direction == 1 and x + hole_radius > base_length - free_edge_limit:
                break
            base_positions.append(x)
            x += direction * spacing
        return {
            "base_positions": base_positions,
            "vertical_positions": vertical_positions,
            "crossing_gap": crossing_gap,
        }

    # ── far_edge, no stop: continuous arc-length pattern that may cross ──────
    # Continuous developed coordinate `sigma`, measured from the base free edge
    # (x = base_length). Hole k sits at sigma_k = edge_distance + k*spacing.
    #   base zone : sigma <= base_length                 -> x = base_length - sigma
    #   bend arc  : base_length < sigma < base_length+bend_radius -> on the fold,
    #               undrillable -> hole skipped
    #   wall zone : sigma >= base_length + bend_radius    -> z = sigma - base_length - bend_radius
    # The +bend_radius term is the inner-arc developed length the pattern travels
    # while wrapping around the fold, keeping the spacing continuous across it.
    k = 0
    while True:
        if hole_count is not None and (len(base_positions) + len(vertical_positions)) >= hole_count:
            break
        sigma = edge_distance + k * spacing
        k += 1
        if sigma <= base_length:
            x = base_length - sigma
            # too close to the free edge (only possible for the very first hole)
            if x + hole_radius > base_length - free_edge_limit:
                continue
            # inside the base-side bend zone -> skip; larger sigma will reach the wall
            if x - hole_radius < bend_edge_limit:
                continue
            base_positions.append(x)
            continue
        # past the base bend line: map onto the vertical wall
        z = sigma - base_length - bend_radius
        if z - hole_radius < bend_edge_limit:
            # inside the bend arc / clearance zone -> undrillable, skip this hole
            continue
        if z + hole_radius > flange_height - free_edge_limit:
            # the hole would overhang the wall's free (top) edge -> stop the row
            break
        vertical_positions.append(z)

    if base_positions and vertical_positions:
        # Developed gap across the bend between the last base hole and the first
        # wall hole. Equals `spacing` when no hole was skipped in the arc zone.
        crossing_gap = base_positions[-1] + bend_radius + vertical_positions[0]

    return {
        "base_positions": base_positions,
        "vertical_positions": vertical_positions,
        "crossing_gap": crossing_gap,
    }


Part.resolveLBracketCrossBendHoles = resolve_lbracket_cross_bend_holes


def makeZShape(
    dim_x,
    dim_y,
    thickness,
    top_flange_height,
    bottom_flange_height,
    top_bend_angle_deg,
    bottom_bend_angle_deg,
    bend_radius,
):
    """
    Create a Z-shaped plate with flanges of potentially different heights.

    Parameters:
    - dim_x: Dimension of the plate along X-axis
    - dim_y: Dimension of the plate along Y-axis
    - thickness: Plate thickness (Z dimension)
    - top_flange_height: Height of the top flange (bent upwards)
    - bottom_flange_height: Height of the bottom flange (bent downwards)
    - top_bend_angle_deg: Bend angle in degrees for the top flange
    - bottom_bend_angle_deg: Bend angle in degrees for the bottom flange
    - bend_radius: Bend radius

    Returns:
    - final_obj: The final FreeCAD object with the Z-shaped plate
    """
    # Ensure sheetmetal is available
    if not _ensure_sheetmetal():
        raise ImportError("SheetMetal module is required for makeZShape function")

    # Create document with auto-generated name
    doc_name = "Z_Shaped_Plate_Y_Axis"
    doc = App.newDocument(doc_name)

    # Create base plate (subtract bend_radius + thickness from both ends to account for bend zones)
    excess = bend_radius + thickness
    base_shape = Part.makeBox(dim_x - 2 * excess, dim_y, thickness, App.Vector(excess, 0, 0))
    base_obj = doc.addObject("Part::Feature", "BasePlate")
    base_obj.Shape = base_shape
    doc.recompute()

    # Bend along Y-axis (flanges on X=0 and X=dim_x edges)
    edge1 = "Edge2"   # Left edge (X=0) - top flange
    edge2 = "Edge6"   # Right edge (X=dim_x) - bottom flange

    print("Creating Z-shaped plate with flanges along Y-axis")

    # Create top flange (bends up)
    flange1_obj = doc.addObject("Part::FeaturePython", "SMBendWall1")
    SheetMetalCmd.SMBendWall(flange1_obj, base_obj, [edge1])
    flange1_obj.length = top_flange_height - excess
    flange1_obj.angle = 180 - top_bend_angle_deg  # SheetMetal uses supplementary angle
    flange1_obj.radius = bend_radius
    flange1_obj.invert = False

    # Create bottom flange (bends down)
    flange2_obj = doc.addObject("Part::FeaturePython", "SMBendWall2")
    SheetMetalCmd.SMBendWall(flange2_obj, base_obj, [edge2])
    flange2_obj.length = bottom_flange_height - excess
    flange2_obj.angle = 180 - bottom_bend_angle_deg
    flange2_obj.radius = bend_radius
    flange2_obj.invert = True # Invert the direction of the bend

    doc.recompute()

    # Fuse plate + flanges
    try:
        fused_shape = base_obj.Shape.fuse(flange1_obj.Shape).fuse(flange2_obj.Shape)
        final_obj = doc.addObject("Part::Feature", "Z_Shaped_Plate")
        final_obj.Shape = fused_shape
        doc.recompute()
    except Exception as e:
        print(f"Warning: fusing failed - using compound. Reason: {e}")
        compound_shape = Part.makeCompound([base_obj.Shape,
                                            flange1_obj.Shape,
                                            flange2_obj.Shape])
        final_obj = doc.addObject("Part::Feature", "Z_Shaped_Plate")
        final_obj.Shape = compound_shape
        doc.recompute()

    return final_obj.Shape

Part.makeZShape = makeZShape


def find_flat_face(obj, normal_vector=App.Vector(0, 0, 1), tolerance=0.95):
    """
    Find the flat plane on the object with normal close to normal_vector
    and the maximum area to attach the next sketch fold.
    """
    flat_face_name = None
    max_area = -1.0
    for i, face in enumerate(obj.Shape.Faces):
        if face.Surface.__class__.__name__ in ("GeomPlane", "Plane"):
            u_mid = (face.ParameterRange[0] + face.ParameterRange[1]) / 2.0
            v_mid = (face.ParameterRange[2] + face.ParameterRange[3]) / 2.0
            n = face.normalAt(u_mid, v_mid)
            if abs(n.dot(normal_vector)) > tolerance:
                if face.Area > max_area:
                    max_area = face.Area
                    flat_face_name = f"Face{i+1}"
    return flat_face_name

def makeCircularFoldedPlate(
    diameter=200.0,
    thickness=2.0,
    bend_radius=2.0,
    folds=None,
    arc_angle=360.0
):
    """
    Creates a circular plate folded using SMFold (Sketch-based folding).
    """
    if folds is None:
        folds = []

    if not _ensure_sheetmetal():
        raise ImportError("SheetMetal module is required for makeCircularFoldedPlate")

    doc = App.newDocument("Circular_Folded_Plate")
    
    # Base circular plate
    radius = diameter / 2.0
    base_shape = Part.makeCylinder(radius, thickness, App.Vector(0, 0, 0), App.Vector(0, 0, 1), arc_angle)
    current_obj = doc.addObject("Part::Feature", "BasePlate")
    current_obj.Shape = base_shape
    doc.recompute()
    
    # We extend the fold lines enough to fully cut across the plate
    bend_line_half_len = radius + 20.0
    
    for idx, fold in enumerate(folds):
        offset_x = fold.get("offset_x", 0.0)
        angle = fold.get("angle", 90.0)
        invert = fold.get("invert", False)
        
        # Find flat top face dynamically
        face_name = find_flat_face(current_obj, App.Vector(0, 0, 1))
        if not face_name:
            print(f"Warning: No suitable flat face found for fold {idx+1}. Skipping.")
            continue
            
        sketch = doc.addObject("Sketcher::SketchObject", f"FoldSketch_{idx+1}")
        sketch.AttachmentSupport = [(current_obj, face_name)]
        sketch.MapMode = "FlatFace"
        doc.recompute()
        
        # Draw bend line along Y axis at offset_x
        p1 = App.Vector(offset_x, -bend_line_half_len, 0.0)
        p2 = App.Vector(offset_x, bend_line_half_len, 0.0)
        sketch.addGeometry(Part.LineSegment(p1, p2), False)
        doc.recompute()
        
        fold_obj = doc.addObject("Part::FeaturePython", f"SMFold_{idx+1}")
        from sheetmetal.SheetMetalFoldCmd import SMFoldWall
        SMFoldWall(fold_obj, current_obj, [face_name], sketch)
        
        if App.GuiUp:
            from sheetmetal.SheetMetalFoldCmd import SMFoldViewProvider
            SMFoldViewProvider(fold_obj.ViewObject)
            
        fold_obj.radius = bend_radius
        fold_obj.angle = angle
        fold_obj.Position = "middle"
        fold_obj.invertbend = fold.get("invertbend", False)
        fold_obj.invert = invert
        doc.recompute()
        
        current_obj = fold_obj
        
    return current_obj.Shape

def makeCircularLShape(diameter, thickness, offset_x, bend_angle_deg, bend_radius, arc_angle=360.0):
    """
    Creates a circular plate with a single bend (L-shape).
    - diameter: Diameter of the circular plate
    - thickness: Thickness of the sheet metal
    - offset_x: Distance from the center of the plate to the bend line
    - bend_angle_deg: Angle of the bend in degrees
    - bend_radius: Inside bend radius
    """
    return makeCircularFoldedPlate(
        diameter=diameter,
        thickness=thickness,
        bend_radius=bend_radius,
        folds=[{"offset_x": offset_x, "angle": bend_angle_deg, "invert": False}],
        arc_angle=arc_angle
    )

def makeCircularUShape(diameter, thickness, offset_x_left, offset_x_right, bend_angle_left, bend_angle_right, bend_radius, arc_angle=360.0):
    """
    Creates a circular plate with two parallel bends in the same direction (U-shape).
    """
    return makeCircularFoldedPlate(
        diameter=diameter,
        thickness=thickness,
        bend_radius=bend_radius,
        folds=[
            {"offset_x": offset_x_left, "angle": bend_angle_left, "invert": False},
            {"offset_x": offset_x_right, "angle": bend_angle_right, "invert": False}
        ],
        arc_angle=arc_angle
    )

def makeCircularZShape(diameter, thickness, offset_x_left, offset_x_right, bend_angle_left, bend_angle_right, bend_radius, arc_angle=360.0):
    """
    Creates a circular plate with two parallel bends in opposite directions (Z-shape).
    """
    return makeCircularFoldedPlate(
        diameter=diameter,
        thickness=thickness,
        bend_radius=bend_radius,
        folds=[
            {"offset_x": offset_x_left, "angle": bend_angle_left, "invert": False},
            {"offset_x": offset_x_right, "angle": bend_angle_right, "invert": True}
        ],
        arc_angle=arc_angle
    )

Part.makeCircularLShape = makeCircularLShape
Part.makeCircularUShape = makeCircularUShape
Part.makeCircularZShape = makeCircularZShape
Part.makeCircularFoldedPlate = makeCircularFoldedPlate



def makeOblong(length, width, height, pnt=None, dir=None):
    """
    Create an oblong (rectangular) box shape with rounded ends (pill shape).
    The rounded ends are created by filleting the short edges at the ends.

    Parameters:
    - length: Dimension along the main axis (long dimension)
    - width: Dimension perpendicular to length (short dimension, determines fillet radius)
    - height: Dimension along the thickness direction
    - pnt: Position vector (optional, default Vector(0,0,0))
    - dir: Direction vector for height (optional, default Vector(0,0,1))

    Returns:
    - Part.Shape - The oblong box shape with rounded ends
    """
    if pnt is None:
        pnt = App.Vector(0, 0, 0)
    if dir is None:
        dir = App.Vector(0, 0, 1)
    
    box = Part.makeBox(
        length,
        width,
        height,
        pnt,
        dir
    )
    
    # For an oblong shape, we need to fillet the edges at the ends
    # These are the edges that are perpendicular to the length direction
    # and have length equal to width
    
    # Calculate fillet radius (equal to width)
    fillet_radius = min(length, width) / 2.0 - 1e-6
    
    # Normalize the extrusion direction
    dir_norm = dir.normalize()
    
    # Find edges with length equal to height that are parallel to the extrusion direction
    edges_to_fillet = []
    for edge in box.Edges:
        if abs(edge.Length - height) < 1e-6:
            # Check if edge is parallel to the extrusion direction
            v0 = edge.Vertexes[0].Point
            v1 = edge.Vertexes[1].Point
            edge_vec = (v1 - v0).normalize()
            
            # Dot product of parallel unit vectors is 1 or -1
            if abs(abs(edge_vec.dot(dir_norm)) - 1.0) < 1e-6:
                edges_to_fillet.append(edge)
    
    # Only apply fillet if we found exactly 4 edges (to be safe)
    if len(edges_to_fillet) == 4:
        # Try to create the fillet
        try:
            filleted_box = box.makeFillet(fillet_radius, edges_to_fillet)
            # Validate the result
            if filleted_box.isValid():
                return filleted_box
            else:
                # If fillet result is invalid, return the original box
                return box
        except Exception:
            # If fillet fails, return the original box
            return box
    
    # If no edges found or fillet not applicable, return the original box
    return box

    

# Add makeOblong to Part module for convenience
Part.makeOblong = makeOblong

def makeKeyhole(length, width_large, width_small, height, pnt=None, dir=None):
    """
    Create a keyhole shape (a large cylinder, a small cylinder, and a connecting box).
    
    Parameters:
    - length: Total length of the bounding box.
    - width_large: Diameter of the large circle.
    - width_small: Diameter of the small circle.
    - height: Extrusion height.
    - pnt: Position vector (optional, default Vector(0,0,0)). This is the lower-left corner
           of the bounding box of the large circle end.
    - dir: Direction vector for height (optional, default Vector(0,0,1))
    
    Returns:
    - Part.Shape - The keyhole shape
    """
    if pnt is None:
        pnt = App.Vector(0, 0, 0)
    if dir is None:
        dir = App.Vector(0, 0, 1)
        
    r1 = width_large / 2.0
    r2 = width_small / 2.0
    
    d = length - r1 - r2
    
    if d <= 0:
        return Part.makeCylinder(r1, height, pnt + App.Vector(r1, r1, 0), dir)
        
    if abs(r1 - r2) < 1e-6:
        return makeOblong(length, width_large, height, pnt, dir)
        
    # 1. Large cylinder
    cyl1_center = App.Vector(r1, r1, 0)
    cyl1 = Part.makeCylinder(r1, height, cyl1_center, App.Vector(0, 0, 1))
    
    # 2. Small cylinder
    cyl2_center = App.Vector(length - r2, r1, 0)
    cyl2 = Part.makeCylinder(r2, height, cyl2_center, App.Vector(0, 0, 1))
    
    # 3. Connecting box (width = small circle diameter, length = distance between centers)
    box_pnt = App.Vector(r1, r1 - r2, 0)
    box = Part.makeBox(d, width_small, height, box_pnt, App.Vector(0, 0, 1))
    
    # Fuse the 3 blocks together
    keyhole = cyl1.fuse(cyl2).fuse(box).removeSplitter()
    
    # Rotate if needed
    dir_n = dir.normalize()
    z_axis = App.Vector(0, 0, 1)
    if dir_n.cross(z_axis).Length > 1e-6 or dir_n.dot(z_axis) < 0:
        import math
        rot = App.Rotation(z_axis, dir_n)
        # Shape.rotate takes angle in degrees
        keyhole.rotate(App.Vector(0, 0, 0), rot.Axis, math.degrees(rot.Angle))
        
    # Translate
    keyhole.translate(pnt)
    
    return keyhole

Part.makeKeyhole = makeKeyhole

def makeHalfCylinder(radius, height, pnt=None, dir=None):
    """
    Create a half-cylinder shape.
    The flat face is oriented along the XY plane by default if dir is along Z.

    Parameters:
    - radius: The radius of the half-cylinder.
    - height: The height of the half-cylinder.
    - pnt: Position vector (optional, default Vector(0,0,0)).
    - dir: Direction vector for the axis of the half-cylinder (optional, default Vector(0,0,1)).

    Returns: Part.Shape - the created half-cylinder shape.
    """
    if pnt is None:
        pnt = App.Vector(0, 0, 0)
    if dir is None:
        dir = App.Vector(0, 0, 1) # Default to Z-axis for height

    # Create a full cylinder
    full_cylinder = Part.makeCylinder(radius, height, pnt, dir)

    # Create a cutting box to make it a half-cylinder
    # The box should be large enough to cut the cylinder in half
    # We'll place the box to cut along the cylinder's length

    # Calculate the rotation from the default Z-axis to the target direction
    z_axis = App.Vector(0, 0, 1)
    rotation = App.Rotation(z_axis, dir)

    # Define the cutting box in a standard orientation (e.g., cutting the positive Y part)
    box_size = 2 * radius
    box_pnt = App.Vector(-radius, 0, 0) + pnt # Start from the center plane
    cutter = Part.makeBox(box_size, radius, height)
    cutter.translate(App.Vector(-radius, 0, 0)) # Center the box for the cut

    # Apply the same placement as the cylinder
    cutter.rotate(pnt, rotation.Axis, rotation.Angle)
    cutter.translate(pnt)

    # Perform the cut
    half_cylinder = full_cylinder.cut(cutter)

    return half_cylinder

# Add makeHalfCylinder to Part module for convenience
Part.makeHalfCylinder = makeHalfCylinder

def makeHexagon(radius, height, pnt=App.Vector(0, 0, 0), dir=App.Vector(0, 0, 1)):
    """
    Create a regular hexagon (hexagonal prism)
    
    Args:
        radius: Radius from center to vertex
        height: Height along the dir direction
        pnt: Position of the center of the base
        dir: Direction of the axis (height direction)
    
    Returns:
        Part.Solid: Regular hexagon
    """
    # Normalize the direction vector
    direction = dir.normalize()
    
    # Create local coordinate system for the hexagonal plane
    # Find 2 vectors perpendicular to direction to create a plane
    if abs(direction.dot(App.Vector(0, 0, 1))) < 0.99:
        # If direction is not parallel to Z-axis
        u = direction.cross(App.Vector(0, 0, 1)).normalize()
    else:
        # If direction is parallel to Z-axis, use X-axis
        u = direction.cross(App.Vector(1, 0, 0)).normalize()
    
    v = direction.cross(u).normalize()
    
    # Create 6 points of the regular hexagon on the plane perpendicular to direction
    points = []
    for i in range(6):
        angle = math.radians(60 * i)
        # Calculate coordinates in local coordinate system (u, v)
        local_x = radius * math.cos(angle)
        local_y = radius * math.sin(angle)
        # Convert to global coordinates
        point = pnt + u * local_x + v * local_y
        points.append(point)
    
    # Create edges
    edges = []
    for i in range(6):
        p1 = points[i]
        p2 = points[(i + 1) % 6]
        edges.append(Part.LineSegment(p1, p2).toShape())
    
    # Create wire and face
    wire = Part.Wire(edges)
    face = Part.Face(wire)
    
    # Extrude along the direction with length height
    extrude_vector = direction * height
    hexagon = face.extrude(extrude_vector)
    
    return hexagon

# Add makeHexagon to Part module for convenience
Part.makeHexagon = makeHexagon

def makeThreaded(radius, height, pnt=None, dir=None):
    """
    Create a threaded hole (semantically) using cylindrical geometry.
    
    NOTE: This function creates the SAME geometry as Part.makeCylinder, but serves as a 
    semantic marker in the code to distinguish threaded holes from regular cylindrical holes.
    
    Usage Context:
    - When Threaded = False in PartDesign::Hole (for visualization purposes), the geometry
      becomes indistinguishable from regular cylindrical holes.
    - This function allows the DETECTION FLOW to identify which holes are threaded vs regular,
      by searching for "makeThreaded" calls in the code, rather than relying on geometry alone.
    
    Parameters:
    - radius: Radius of the threaded hole (controls the actual hole size)
    - height: Depth/height of the threaded hole
    - pnt: Position vector (optional, default Vector(0,0,0))
    - dir: Direction vector for the hole axis (optional, default Vector(0,0,1))
    
    Returns:
    - Part.Shape - A cylindrical shape representing the threaded hole
    
    Example:
        # Instead of:
        # hole = Part.makeCylinder(2.0, 10.0, App.Vector(5, 5, 0), App.Vector(0, 0, 1))
        
        # Use:
        # threaded_hole = Part.makeThreaded(2.0, 10.0, App.Vector(5, 5, 0), App.Vector(0, 0, 1))
        # This allows detection scripts to identify it as a threaded hole via code analysis
    """
    # Set default values
    if pnt is None:
        pnt = App.Vector(0, 0, 0)
    if dir is None:
        dir = App.Vector(0, 0, 1)
    
    # Create cylinder using FreeCAD's native function
    # The geometry is identical to makeCylinder - only the semantic meaning differs
    cylinder = Part.makeCylinder(radius, height, pnt, dir)
    
    return cylinder

# Add makeThreaded to Part module for convenience
Part.makeThreaded = makeThreaded


def makeCountersink(hole_radius, cs_radius, cs_angle_deg, thickness, pnt=None, dir=None,
                    cs_side="end", buffer=1.0):
    """Create a countersink tool shape (fused cylinder + cone) for boolean .cut() operations.

    Works correctly for both direct .cut() and map_and_cut_leg2_batch at ANY angle,
    because map_and_cut_leg2_batch uses OFFSET=0: canonical x=0 maps exactly to the
    outer face after the placement transform. The internal buffer shifts the cylinder
    start to x=-buffer (outside the face), providing the clearance needed for a
    clean boolean cut.

    Parameters
    ----------
    hole_radius  : float - through-hole radius, e.g. 1.6 for Ø3.2 mm
    cs_radius    : float - countersink mouth radius, e.g. 3.0 for Ø6 mm
    cs_angle_deg : float - full apex angle in degrees (e.g. 90)
    thickness    : float - material thickness (mm)
    pnt          : App.Vector - hole centre on the OUTER (entry) face
    dir          : App.Vector - unit vector FROM outer face INTO material
    cs_side      : 'start' = CS opens at outer/entry face
                   'end'   = CS opens at inner/exit face
    buffer       : float - clearance mm added outside each face (default 1.0)

    Usage
    -----
    # Direct .cut() on a flat plate:
    tool = Part.makeCountersink(
        hole_radius=1.6, cs_radius=3.0, cs_angle_deg=90,
        thickness=1.5, pnt=App.Vector(15, 30, 0),
        dir=App.Vector(0, 0, 1), cs_side='end',
    )
    bracket = bracket.cut(tool)

    # Via map_and_cut_leg2_batch (90 deg or any other angle) - NO special params:
    tool = Part.makeCountersink(
        hole_radius=1.6, cs_radius=3.0, cs_angle_deg=90,
        thickness=1.5, pnt=App.Vector(0, cy, cz),
        dir=App.Vector(1, 0, 0), cs_side='end',
    )
    bracket = map_and_cut_leg2_batch(bracket, [{'tool_shape': tool, ...}], ...)
    """
    import FreeCAD as App
    import Part as _Part
    import math

    if pnt is None:
        pnt = App.Vector(0.0, 0.0, 0.0)
    if dir is None:
        dir = App.Vector(0.0, 0.0, 1.0)

    dir_n = App.Vector(dir.x, dir.y, dir.z).normalize()

    # -- Geometry -----------------------------------------------------------
    half_angle_rad = math.radians(cs_angle_deg / 2.0)
    cs_depth = (cs_radius - hole_radius) / math.tan(half_angle_rad)

    # Extend cone by buffer so it exits cleanly (no paper-thin boolean remnant)
    extended_depth = cs_depth + buffer
    extended_cs_r  = hole_radius + extended_depth * math.tan(half_angle_rad)

    # -- Through-hole cylinder ----------------------------------------------
    # Starts buffer mm before pnt (outside the material) for a clean cut.
    # buffer also extends past the inner face, so the cylinder is symmetric.
    cyl_start  = pnt - dir_n * buffer
    cyl_height = thickness + 2.0 * buffer
    cylinder   = _Part.makeCylinder(hole_radius, cyl_height, cyl_start, dir_n)

    # -- Countersink cone ---------------------------------------------------
    if cs_side == "start":
        # Cone opens WIDE at pnt (outer/entry face) and NARROWS inward.
        # r(buffer) = extended_cs_r - buffer*tan(half) = cs_radius exactly at pnt.
        cone_start = pnt - dir_n * buffer
        cone = _Part.makeCone(extended_cs_r, hole_radius, extended_depth, cone_start, dir_n)
    else:  # "end"
        # Cone opens at exit face (pnt + dir_n * thickness), pointing outward (-dir_n).
        exit_pt    = pnt + dir_n * thickness
        cone_start = exit_pt + dir_n * buffer
        cone = _Part.makeCone(extended_cs_r, hole_radius, extended_depth, cone_start, -dir_n)

    # -- Fuse and return ----------------------------------------------------
    tool = cylinder.fuse(cone)
    return tool


# Add makeCountersink to Part module for convenience
Part.makeCountersink = makeCountersink


def makeTShape(
    dim_x=200.0,
    dim_y=400.0,
    thickness=3.0,
    height=80.0,
    bend_radius=0.0,
):
    """
    Create a T-shaped sheet-metal profile using direct solid construction.
    (two Part.makeBox solids fused — NO SheetMetal bending required)

    Cross-section (looking along Y / extrusion axis):

         ←────────── dim_x ──────────→
         __________________________________
        |           FLANGE               |  ← thickness
                  |       |
                  |  WEB  |  ← height
                  |       |
                  ← thick →

    The web is centred on the flange:
      web_left  = (dim_x - thickness) / 2
      web_right = (dim_x + thickness) / 2

    Coordinate system (origin = bottom-left corner of flange):
      X : 0 → dim_x          (flange full width)
      Y : 0 → dim_y          (extrusion / length)
      Z : 0 → thickness      (flange)
              thickness → thickness + height  (web, above flange)

    Key face positions:
      Flange bottom face  : Z = 0
      Flange top face     : Z = thickness
      Web left outer face : X = (dim_x - thickness) / 2
      Web right outer face: X = (dim_x + thickness) / 2
      Web tip (top face)  : Z = thickness + height
      Bend arcs           : 2 concave edges at Z = thickness (web meets flange)

    Parameters
    ----------
    dim_x       : float — total flange width (X, mm)
    dim_y       : float — extrusion / length (Y, mm)
    thickness   : float — sheet thickness (mm); same for flange and web
    height      : float — height of the vertical web above the flange top face (mm)
    bend_radius : float — inner fillet radius at the 2 junction edges (0 = sharp, mm)
                          Typical value = thickness (standard press-brake inner radius)

    Returns
    -------
    Part.Shape — The fused T-shaped solid
    """
    import FreeCAD as App
    import Part

    web_x = (dim_x - thickness) / 2.0   # X position of web left face

    # ── 1. FLANGE ─────────────────────────────────────────────────────────────
    # X: [0 … dim_x],  Y: [0 … dim_y],  Z: [0 … thickness]
    flange = Part.makeBox(dim_x, dim_y, thickness, App.Vector(0, 0, 0))

    # ── 2. WEB ────────────────────────────────────────────────────────────────
    # X: [web_x … web_x+thickness],  Z: [thickness … thickness+height]
    web = Part.makeBox(thickness, dim_y, height, App.Vector(web_x, 0, thickness))

    # ── 3. Fuse ───────────────────────────────────────────────────────────────
    try:
        t_solid = flange.fuse(web).removeSplitter()
        print("[makeTShape] Fuse OK — T profile created successfully.")
    except Exception as exc:
        print(f"[makeTShape] WARNING: fuse failed ({exc}); using compound fallback.")
        t_solid = Part.makeCompound([flange, web])

    # ── 4. Bend fillet at junction (Z = thickness) ────────────────────────────
    # Find 2 Y-parallel edges of length ≈ dim_y whose midpoint is at Z ≈ thickness
    # and strictly inside X ∈ (0, dim_x).
    if bend_radius > 0:
        try:
            tol = max(1e-3, thickness * 0.1)
            junction_edges = []
            for edge in t_solid.Edges:
                if abs(edge.Length - dim_y) > tol:
                    continue
                verts = edge.Vertexes
                if len(verts) < 2:
                    continue
                mid = (verts[0].Point + verts[1].Point) * 0.5
                if abs(mid.z - thickness) < tol and tol < mid.x < dim_x - tol:
                    junction_edges.append(edge)

            print(f"[makeTShape] Found {len(junction_edges)} junction edge(s).")
            if len(junction_edges) >= 2:
                t_solid = t_solid.makeFillet(bend_radius, junction_edges)
                print(f"[makeTShape] Bend fillet r={bend_radius}mm applied.")
            else:
                print("[makeTShape] WARNING: too few junction edges — skipping fillet.")
        except Exception as exc:
            print(f"[makeTShape] WARNING: bend fillet failed ({exc}).")

    return t_solid


# Register makeTShape on Part module for convenience
Part.makeTShape = makeTShape


def makeIShape(
    dim_x=200.0,
    dim_y=400.0,
    thickness=3.0,
    height=150.0,
    bend_radius=0.0,
):
    """
    Create an I-shaped (H-beam) sheet-metal profile using direct solid construction.
    (three Part.makeBox solids fused — NO SheetMetal bending required)

    Cross-section (looking along Y / extrusion axis):

         ←────────── dim_x ──────────→
         __________________________________
        |           TOP FLANGE           |  ← thickness
                  |       |
                  |  WEB  |  ← height  (between the two flanges)
                  |       |
        |_______BOTTOM FLANGE____________|  ← thickness
         ←────────── dim_x ──────────→

    Both flanges have the same width dim_x.
    The web is centred and shared by both flanges.

    Coordinate system (origin = bottom-left corner of bottom flange):
      X : 0 → dim_x                     (flange full width)
      Y : 0 → dim_y                     (extrusion / length)
      Z : 0                             → bottom face of bottom flange
          thickness                     → top of bottom flange / BOTTOM JUNCTION
          thickness + height            → bottom of top flange / TOP JUNCTION
          2×thickness + height          → top face of top flange

    Key face positions:
      Bottom flange bottom face : Z = 0
      Bottom flange top face    : Z = thickness           ← bottom bend junction
      Web left outer face       : X = (dim_x - thickness) / 2
      Web right outer face      : X = (dim_x + thickness) / 2
      Top flange bottom face    : Z = thickness + height  ← top bend junction
      Top flange top face       : Z = 2×thickness + height
      Bend arcs                 : 4 concave edges (2 at each junction)

    Parameters
    ----------
    dim_x       : float — total flange width (X, mm); same for top and bottom
    dim_y       : float — extrusion / length (Y, mm)
    thickness   : float — sheet thickness (mm); same for all three plates
    height      : float — height of the web BETWEEN the two flanges (mm)
    bend_radius : float — inner fillet radius at ALL 4 junction edges (0 = sharp, mm)
                          Typical value = thickness (standard press-brake inner radius)

    Returns
    -------
    Part.Shape — The fused I-shaped solid (with optional bend fillets at both junctions)
    """
    import FreeCAD as App
    import Part

    web_x = (dim_x - thickness) / 2.0   # X position of web left face

    # ── 1. BOTTOM FLANGE ──────────────────────────────────────────────────────
    # X: [0 … dim_x],  Z: [0 … thickness]
    bottom_flange = Part.makeBox(dim_x, dim_y, thickness, App.Vector(0, 0, 0))

    # ── 2. WEB ────────────────────────────────────────────────────────────────
    # X: [web_x … web_x+thickness],  Z: [thickness … thickness+height]
    web = Part.makeBox(thickness, dim_y, height, App.Vector(web_x, 0, thickness))

    # ── 3. TOP FLANGE ─────────────────────────────────────────────────────────
    # X: [0 … dim_x],  Z: [thickness+height … 2×thickness+height]
    top_flange = Part.makeBox(dim_x, dim_y, thickness, App.Vector(0, 0, thickness + height))

    # ── 4. Fuse ───────────────────────────────────────────────────────────────
    try:
        i_solid = bottom_flange.fuse(web).fuse(top_flange).removeSplitter()
        print("[makeIShape] Fuse OK — I profile created successfully.")
    except Exception as exc:
        print(f"[makeIShape] WARNING: fuse failed ({exc}); using compound fallback.")
        i_solid = Part.makeCompound([bottom_flange, web, top_flange])

    # ── 5. Bend fillets at BOTH junctions ─────────────────────────────────────
    # Bottom junction: Z = thickness
    # Top    junction: Z = thickness + height
    # Find all Y-parallel edges (length ≈ dim_y) at those Z heights,
    # strictly inside X ∈ (0, dim_x).
    if bend_radius > 0:
        try:
            tol = max(1e-3, thickness * 0.1)
            z_bot = thickness
            z_top = thickness + height

            junction_edges = []
            for edge in i_solid.Edges:
                if abs(edge.Length - dim_y) > tol:
                    continue
                verts = edge.Vertexes
                if len(verts) < 2:
                    continue
                mid = (verts[0].Point + verts[1].Point) * 0.5
                at_junc = (abs(mid.z - z_bot) < tol or abs(mid.z - z_top) < tol)
                if at_junc and tol < mid.x < dim_x - tol:
                    junction_edges.append(edge)

            print(f"[makeIShape] Found {len(junction_edges)} junction edge(s).")
            if len(junction_edges) >= 2:
                i_solid = i_solid.makeFillet(bend_radius, junction_edges)
                print(f"[makeIShape] Bend fillet r={bend_radius}mm applied — "
                      f"{len(junction_edges)} edge(s).")
            else:
                print("[makeIShape] WARNING: too few junction edges — skipping fillet.")
        except Exception as exc:
            print(f"[makeIShape] WARNING: bend fillet failed ({exc}).")

    return i_solid


# Register makeIShape on Part module for convenience
Part.makeIShape = makeIShape


def _validate_positive_length(name, value):
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0.")


def _validate_triangle_inequality(side_1, side_2, side_3):
    if side_1 + side_2 <= side_3 or side_1 + side_3 <= side_2 or side_2 + side_3 <= side_1:
        raise ValueError("Triangle side lengths do not satisfy the triangle inequality.")


def get_equilateral_triangle_points(side_length):
    """
    Return points for an equilateral triangle.

    Coordinate convention:
    - A = points[0] = (0, 0, 0)
    - B = points[1] = (side_length, 0, 0), so A-B is the base edge
    - C = points[2] points toward +Y
    """
    _validate_positive_length("side_length", side_length)
    triangle_height = math.sqrt(3.0) * side_length / 2.0
    return [
        App.Vector(0.0, 0.0, 0.0),
        App.Vector(side_length, 0.0, 0.0),
        App.Vector(side_length / 2.0, triangle_height, 0.0),
    ]


def get_isosceles_triangle_points(base_length, equal_side_length):
    """
    Return points for an isosceles triangle.

    A-B is the base edge, and C is centered above the base.
    """
    _validate_positive_length("base_length", base_length)
    _validate_positive_length("equal_side_length", equal_side_length)
    _validate_triangle_inequality(base_length, equal_side_length, equal_side_length)
    half_base = base_length / 2.0
    triangle_height = math.sqrt(equal_side_length * equal_side_length - half_base * half_base)
    return [
        App.Vector(0.0, 0.0, 0.0),
        App.Vector(base_length, 0.0, 0.0),
        App.Vector(half_base, triangle_height, 0.0),
    ]


def get_right_triangle_points_from_legs(x_leg_length, y_leg_length):
    """
    Return points for a right triangle from the two perpendicular legs.

    A = points[0] is the right-angle vertex. A-B is the X leg, A-C is the Y
    leg, and B-C is the hypotenuse. Therefore "grand cote" / longest side /
    hypotenuse must select points[1] -> points[2] for bends.
    """
    _validate_positive_length("x_leg_length", x_leg_length)
    _validate_positive_length("y_leg_length", y_leg_length)
    return [
        App.Vector(0.0, 0.0, 0.0),
        App.Vector(x_leg_length, 0.0, 0.0),
        App.Vector(0.0, y_leg_length, 0.0),
    ]


def get_right_isosceles_triangle_points_from_leg(leg_length):
    """Return points for a right-isosceles triangle from one perpendicular leg."""
    _validate_positive_length("leg_length", leg_length)
    return get_right_triangle_points_from_legs(leg_length, leg_length)


def get_right_isosceles_triangle_points_from_hypotenuse(hypotenuse_length):
    """
    Return points for a right-isosceles triangle from the hypotenuse.

    Use this when the user says "triangle rectangle isocele" with only
    "grand cote" / hypotenuse length.
    """
    _validate_positive_length("hypotenuse_length", hypotenuse_length)
    leg_length = hypotenuse_length / math.sqrt(2.0)
    return get_right_triangle_points_from_legs(leg_length, leg_length)


def get_right_triangle_points_from_hypotenuse_and_leg(hypotenuse_length, leg_length, leg_axis="x"):
    """
    Return points for a right triangle from hypotenuse and one leg.

    leg_axis="x" places the known leg on A-B; leg_axis="y" places it on A-C.
    """
    _validate_positive_length("hypotenuse_length", hypotenuse_length)
    _validate_positive_length("leg_length", leg_length)
    if leg_length >= hypotenuse_length:
        raise ValueError("leg_length must be smaller than hypotenuse_length.")
    other_leg_length = math.sqrt(hypotenuse_length * hypotenuse_length - leg_length * leg_length)
    if leg_axis == "x":
        return get_right_triangle_points_from_legs(leg_length, other_leg_length)
    if leg_axis == "y":
        return get_right_triangle_points_from_legs(other_leg_length, leg_length)
    raise ValueError("leg_axis must be 'x' or 'y'.")


def get_scalene_triangle_points_from_sides(base_length, side_to_origin, side_to_base_end):
    """
    Return points for a scalene triangle from three named sides.

    A-B is the base edge. side_to_origin is A-C, and side_to_base_end is B-C.
    """
    _validate_positive_length("base_length", base_length)
    _validate_positive_length("side_to_origin", side_to_origin)
    _validate_positive_length("side_to_base_end", side_to_base_end)
    _validate_triangle_inequality(base_length, side_to_origin, side_to_base_end)
    x_coord = (
        side_to_origin * side_to_origin
        - side_to_base_end * side_to_base_end
        + base_length * base_length
    ) / (2.0 * base_length)
    y_squared = side_to_origin * side_to_origin - x_coord * x_coord
    if y_squared < 0 and abs(y_squared) < 1e-9:
        y_squared = 0.0
    if y_squared <= 0:
        raise ValueError("Triangle side lengths create a degenerate triangle.")
    return [
        App.Vector(0.0, 0.0, 0.0),
        App.Vector(base_length, 0.0, 0.0),
        App.Vector(x_coord, math.sqrt(y_squared), 0.0),
    ]


def make_triangle_plate(points, thickness):
    """Create a triangular plate shape by extruding a 3-point triangle along +Z."""
    if len(points) != 3:
        raise ValueError("make_triangle_plate expects exactly 3 points.")
    _validate_positive_length("thickness", thickness)
    wire_points = [App.Vector(point.x, point.y, 0.0) for point in points]
    wire_points.append(App.Vector(points[0].x, points[0].y, 0.0))
    face = Part.Face(Part.makePolygon(wire_points))
    return face.extrude(App.Vector(0.0, 0.0, thickness))


def find_edge_by_points(shape, start_point, end_point, tolerance=0.05):
    """Return the FreeCAD edge name matching two points, independent of edge direction."""
    for index, edge in enumerate(shape.Edges):
        if len(edge.Vertexes) != 2:
            continue
        edge_start = edge.Vertexes[0].Point
        edge_end = edge.Vertexes[1].Point
        forward = (
            edge_start.distanceToPoint(start_point) <= tolerance
            and edge_end.distanceToPoint(end_point) <= tolerance
        )
        backward = (
            edge_start.distanceToPoint(end_point) <= tolerance
            and edge_end.distanceToPoint(start_point) <= tolerance
        )
        if forward or backward:
            return f"Edge{index + 1}"
    return None


Part.makeTrianglePlate = make_triangle_plate


_SQRT_HALF = math.sqrt(0.5)


def diagonal_corner_cut_points(
    plate_length, plate_width, corner, cut_length, cut_width,
    distance_from_corner=0.0, direction=None,
):
    """
    Compute the 4 XY corner points of a diagonal corner cut/hole tool.

    Split out from `makeDiagonalCornerCut` so the geometry can be unit-tested
    without FreeCAD. See that function for the full parameter contract.

    Returns:
    - (p1, p2, p3, p4) as (x, y) tuples, ordered near-left, near-right,
      far-right, far-left relative to the cut axis.
    """
    corners = {
        "front_left": (0.0, 0.0),
        "front_right": (plate_length, 0.0),
        "back_left": (0.0, plate_width),
        "back_right": (plate_length, plate_width),
    }
    if corner not in corners:
        raise ValueError(f"corner must be one of {list(corners)}, got {corner!r}")
    if cut_length <= 0.0 or cut_width <= 0.0:
        raise ValueError(
            f"cut_length and cut_width must be > 0, got {cut_length!r} / {cut_width!r}"
        )
    if distance_from_corner < 0.0:
        raise ValueError(
            f"distance_from_corner must be >= 0, got {distance_from_corner!r}"
        )

    corner_x, corner_y = corners[corner]
    # Unit vectors pointing from this corner into the plate along each edge.
    inward_x = 1.0 if corner_x == 0.0 else -1.0
    inward_y = 1.0 if corner_y == 0.0 else -1.0

    if direction is None:
        # Default: the corner's 45 deg angle bisector, NOT corner -> plate center.
        # The bisector keeps the cut symmetric about its own axis for ANY plate
        # aspect ratio; a corner -> center axis is only 45 deg on a square plate
        # and produces a visibly lopsided slot on a rectangular one.
        dir_x, dir_y = inward_x * _SQRT_HALF, inward_y * _SQRT_HALF
    else:
        dir_x, dir_y = float(direction.x), float(direction.y)
        norm = math.hypot(dir_x, dir_y)
        if norm < 1e-9:
            raise ValueError("direction must be a non-zero vector in the XY plane")
        dir_x, dir_y = dir_x / norm, dir_y / norm

    perp_x, perp_y = -dir_y, dir_x
    half_width = cut_width / 2.0

    # How much of each long side the plate's own corner eats away. The near end
    # of the rectangle straddles the plate boundary whenever it sits closer to
    # the corner than half the cut width (projected onto each edge), so the
    # visible side would come out SHORTER than cut_length. Extend the tool past
    # the far end by that amount so the machined side measures cut_length.
    # Both terms collapse to (half_width - distance_from_corner) on the default
    # 45 deg axis, which is the only case where both sides can be exact.
    lost_x = abs(half_width * perp_x) - distance_from_corner * abs(dir_x)
    lost_y = abs(half_width * perp_y) - distance_from_corner * abs(dir_y)
    overshoot = max(0.0, lost_x / abs(dir_x) if abs(dir_x) > 1e-12 else 0.0,
                    lost_y / abs(dir_y) if abs(dir_y) > 1e-12 else 0.0)

    near_x = corner_x + dir_x * distance_from_corner
    near_y = corner_y + dir_y * distance_from_corner
    far_reach = cut_length + overshoot

    p1 = (near_x + perp_x * half_width, near_y + perp_y * half_width)
    p2 = (near_x - perp_x * half_width, near_y - perp_y * half_width)
    p3 = (p2[0] + dir_x * far_reach, p2[1] + dir_y * far_reach)
    p4 = (p1[0] + dir_x * far_reach, p1[1] + dir_y * far_reach)
    return p1, p2, p3, p4


def makeDiagonalCornerCut(
    plate_length, plate_width, thickness, corner, cut_length, cut_width,
    distance_from_corner=0.0, direction=None, buffer=2.0,
):
    """
    Create a diagonal corner cut/hole tool (straight rectangular slot, NOT a
    triangle) for a flat rectangular plate, to be removed with `.cut()`.

    The tool is a `cut_length` x `cut_width` rectangle whose long axis runs
    along `direction` and which is CENTRED on that axis — never anchored by one
    of its own corners. Do NOT hand-build this with `Part.makeBox(...).rotate()`:
    `makeBox` is anchored at a box corner, so rotating it puts the cut axis on
    the rectangle's long edge instead of through its centre. Do NOT confuse it
    with make_triangle_plate/get_right_triangle_points_from_legs either — those
    build a whole triangular PLATE, not a corner notch.

    `distance_from_corner` selects between the shapes this produces:
    - `0` (default) — an OPEN notch: the cut reaches the corner and severs it.
    - `< cut_width / 2` — a partially open notch; the corner is still severed.
    - `== cut_width / 2` — DEGENERATE: the cut touches each plate edge at exactly
      one point, so the corner falls off as a separate loose triangle and the
      part comes out in two pieces. Legal geometry, but almost never intended.
    - `> cut_width / 2` — a CLOSED rectangular hole sitting on the diagonal,
      clear of both plate edges. This is the only value range that keeps the
      plate in one piece AND leaves material between the hole and the corner.

    `cut_length` always measures the cut's long side AS MACHINED ON THE PLATE.
    When the near end straddles the plate boundary the tool is automatically
    extended past its far end to compensate, so measuring the finished part
    returns `cut_length` rather than a shorter clipped value.

    Parameters:
    - plate_length, plate_width: plate X/Y dimensions
    - thickness: plate thickness (Z)
    - corner: "front_left" (0,0), "front_right" (plate_length,0),
              "back_left" (0,plate_width), "back_right" (plate_length,plate_width)
    - cut_length: length of the cut's long side, measured on the finished plate
    - cut_width: width of the cut, perpendicular to `direction`
    - distance_from_corner: distance from the corner point to the cut's near
                            short edge, measured ALONG the cut axis (not as
                            separate X/Y edge distances)
    - direction: optional App.Vector (XY plane) overriding the default axis,
                 which is the corner's 45 deg angle bisector
    - buffer: extra extrusion depth on each side of the plate for a clean through-cut

    Returns:
    - Part.Shape: solid cutting tool
    """
    pts = diagonal_corner_cut_points(
        plate_length, plate_width, corner, cut_length, cut_width,
        distance_from_corner=distance_from_corner, direction=direction,
    )
    vecs = [App.Vector(x, y, 0.0) for x, y in pts]

    face = Part.Face(Part.makePolygon(vecs + [vecs[0]]))

    cut_height = thickness + 2.0 * buffer
    tool = face.extrude(App.Vector(0.0, 0.0, cut_height))
    tool.translate(App.Vector(0.0, 0.0, -buffer))
    return tool


Part.makeDiagonalCornerCut = makeDiagonalCornerCut
