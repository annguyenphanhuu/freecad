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

    `distance_from_corner` selects between the two shapes this produces:
    - `0` (default) — an OPEN notch: the cut reaches the corner and severs it.
    - `>= cut_width / 2` — a CLOSED rectangular hole sitting on the diagonal,
      clear of both plate edges.
    Values in between give a partially open notch.

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
