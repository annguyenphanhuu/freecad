# TubeFunction.py - Functions related to tube/cylinder operations
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

def makeTub(thickness, bend_radius, dim_x, dim_y, height, bend_angle=90, target_walls=None):
    """
    Create Capot by bending a flat base plate (Plat) on its 4 perimeter
    edges simultaneously with SMBendWall. Bending all requested edges in a
    single SMBendWall call lets FreeCAD auto-merge/relieve the corners when
    bend_angle < 90 (e.g. 60 deg) would otherwise make adjacent flanges
    overlap.

    Parameters:
    - thickness: Sheet thickness (mm)
    - bend_radius: Inner bend radius (mm)
    - dim_x: Dimension X (Length) (mm)
    - dim_y: Dimension Y (Width) (mm)
    - height: Flange length measured from the bend line (mm)
    - bend_angle: Physical bend angle in degrees (e.g. 60, 90, 120). Defaults to 90.
    - target_walls: List of walls to BUILD/bend. Options are "front",
                    "back" (or "rear"), "left", "right". Defaults to all 4
                    walls ["front", "back", "left", "right"] if None.

    Returns:
    - Capot object with the requested walls bent at bend_angle
    """
    if target_walls is None:
        target_walls = ["front", "back", "left", "right"]
    else:
        target_walls = [w.lower() for w in target_walls]

    kept_walls = set(target_walls)
    if "rear" in kept_walls or "back" in kept_walls:
        kept_walls.update({"back", "rear"})

    base_shape = SheetMetalBaseShapeCmd.smCreateBaseShape(
        type="Plat",
        thickness=thickness,
        radius=bend_radius,
        width=dim_y,
        length=dim_x,
        height=0,
        flangeWidth=0,
        fillGaps=True,
        origin="0,0"
    )

    base_obj = App.ActiveDocument.addObject("Part::Feature", "Capot_Base")
    base_obj.Shape = base_shape
    base_obj.Label = "Capot_Base"
    App.ActiveDocument.recompute()

    # Perimeter edges of the flat plate, all at Z = thickness (top surface)
    plate_z = thickness
    tolerance = 0.1
    wall_edges = {
        "back":  ((-dim_x / 2.0, dim_y / 2.0, plate_z), (dim_x / 2.0, dim_y / 2.0, plate_z)),
        "front": ((-dim_x / 2.0, -dim_y / 2.0, plate_z), (dim_x / 2.0, -dim_y / 2.0, plate_z)),
        "left":  ((-dim_x / 2.0, -dim_y / 2.0, plate_z), (-dim_x / 2.0, dim_y / 2.0, plate_z)),
        "right": ((dim_x / 2.0, -dim_y / 2.0, plate_z), (dim_x / 2.0, dim_y / 2.0, plate_z)),
    }

    edge_candidates = []
    for wall, (t1, t2) in wall_edges.items():
        if wall not in kept_walls:
            continue
        edge_name, _ = find_edge_by_coordinates(base_obj.Shape, t1, t2, tolerance=tolerance)
        if edge_name:
            edge_candidates.append(edge_name)
        else:
            print(f"Warning: Could not find edge for wall '{wall}'.")

    if not edge_candidates:
        print("Warning: No walls to bend (target_walls empty). Returning flat base plate.")
        tub_obj = App.ActiveDocument.addObject("Part::Feature", "Capot")
        tub_obj.Shape = base_obj.Shape
        tub_obj.Label = "Capot"
        App.ActiveDocument.removeObject(base_obj.Name)
        App.ActiveDocument.recompute()
        return tub_obj

    # Bend all target edges at once so FreeCAD resolves overlapping corners
    bend_obj = App.ActiveDocument.addObject("Part::FeaturePython", "SMBendWall_Capot_All")
    SheetMetalCmd.SMBendWall(bend_obj, base_obj, edge_candidates)

    bend_obj.length = height - bend_radius
    bend_obj.angle = 180.0 - bend_angle
    bend_obj.radius = bend_radius
    App.ActiveDocument.recompute()

    tub_obj = App.ActiveDocument.addObject("Part::Feature", "Capot")
    tub_obj.Shape = bend_obj.Shape
    tub_obj.Label = "Capot"
    App.ActiveDocument.recompute()

    return tub_obj

Part.makeTub = makeTub

def AddOutwardBend(shape, bend_length, bend_angle, bend_radius, target_walls=None, fillet=0.0, thickness=1.5):
    """
    Add outward bends to specific edges of a shape (bending away from the shape)

    Args:
        shape: The base shape object to add bends to
        bend_length: Length of the bend
        bend_angle: Angle of the bend in degrees
        bend_radius: Radius of the bend
        target_walls: List of walls to bend. Options are "front", "back" (or "rear"), "left", "right". 
                      Defaults to ["left", "right"].
        fillet: Fillet radius to apply to edges parallel to Z-axis with length equal to thickness
        thickness: Sheet thickness to identify edges for filleting

    Returns:
        The shape with outward bends applied, or original shape if bending fails
    """
    if target_walls is None:
        target_walls = ["left", "right"]
    elif isinstance(target_walls, str):
        target_walls = [target_walls]

    bbox = shape.Shape.BoundBox
    wall_height = bbox.ZMax

    # Calculate dim_x and dim_y based on the base of the shape (Z < thickness + 0.1)
    # This prevents outward bends at the top from artificially inflating dim_x and dim_y
    min_x, max_x = float('inf'), float('-inf')
    min_y, max_y = float('inf'), float('-inf')
    
    for v in shape.Shape.Vertexes:
        if v.Z < thickness + 0.1:
            min_x = min(min_x, v.X)
            max_x = max(max_x, v.X)
            min_y = min(min_y, v.Y)
            max_y = max(max_y, v.Y)
            
    if max_x > min_x and max_y > min_y:
        # Use the maximum absolute extent to ensure symmetry assumption holds
        dim_x = max(abs(max_x), abs(min_x)) * 2.0
        dim_y = max(abs(max_y), abs(min_y)) * 2.0
    else:
        # Fallback to full bbox
        dim_x = max(abs(bbox.XMax), abs(bbox.XMin)) * 2.0
        dim_y = max(abs(bbox.YMax), abs(bbox.YMin)) * 2.0

    edge_candidates = []
    
    for wall in target_walls:
        wall_lower = wall.lower()
        if wall_lower in ["back", "rear"]:
            t1 = (-dim_x/2 + thickness + 0.1, dim_y/2, wall_height)
            t2 = (dim_x/2 - thickness - 0.1, dim_y/2, wall_height)
        elif wall_lower == "front":
            t1 = (-dim_x/2 + thickness + 0.1, -dim_y/2, wall_height)
            t2 = (dim_x/2 - thickness - 0.1, -dim_y/2, wall_height)
        elif wall_lower == "left":
            t1 = (-dim_x/2, dim_y/2-0.1, wall_height)
            t2 = (-dim_x/2, -dim_y/2+0.1, wall_height)
        elif wall_lower == "right":
            t1 = (dim_x/2, dim_y/2-0.1, wall_height)
            t2 = (dim_x/2, -dim_y/2+0.1, wall_height)
        else:
            print(f"Warning: Unknown wall '{wall}'. Skipping.")
            continue
            
        edge_name, _ = find_edge_by_coordinates(shape.Shape, t1, t2, tolerance=10.0)
        if edge_name:
            edge_candidates.append(edge_name)
        else:
            print(f"Warning: Could not dynamically find edge for wall '{wall}'.")

    if not edge_candidates:
        print("Warning: Could not find any edges to bend. Skipping bend.")
        return shape

    try:
        # Create a single wall object with all edges at once
        walls_str = "_".join(target_walls)
        wall_obj = App.ActiveDocument.addObject("Part::FeaturePython", f"SMBendWall_Outward_{walls_str}")
        SheetMetalCmd.SMBendWall(wall_obj, shape, edge_candidates)

        wall_obj.length = bend_length - bend_radius
        wall_obj.angle = (180 - bend_angle)
        wall_obj.radius = bend_radius
        App.ActiveDocument.recompute()

        fused_shape = wall_obj.Shape

        # Apply fillet if specified
        if fillet > 0.0:
            try:
                # Find edges that are parallel to Z-axis and have length equal to thickness
                edges_to_fillet = []
                for i, edge in enumerate(fused_shape.Edges):
                    try:
                        # Check if edge is parallel to Z-axis (direction vector close to (0,0,1) or (0,0,-1))
                        edge_vector = edge.lastVertex().Point.sub(edge.firstVertex().Point)
                        if edge_vector.Length < 0.001:  # Skip degenerate edges
                            continue

                        edge_vector_normalized = edge_vector.normalize()

                        # Check if edge is parallel to Z-axis (dot product with Z-axis close to ±1)
                        z_axis = App.Vector(0, 0, 1)
                        dot_product = abs(edge_vector_normalized.dot(z_axis))

                        # Check if edge length is approximately equal to thickness
                        edge_length = edge.Length

                        if dot_product > 0.99 and abs(edge_length - thickness) < 0.1:  # Relaxed tolerance
                            # Only select the outer corners of the flange, avoiding inner relief cuts
                            midpoint = edge.CenterOfMass
                            bbox = fused_shape.BoundBox
                            is_outer = False
                            
                            tolerance = 5.0 # mm
                            
                            # Check if edge is near any of the bounding box extremities
                            if (abs(midpoint.x - bbox.XMin) < tolerance or 
                                abs(midpoint.x - bbox.XMax) < tolerance or
                                abs(midpoint.y - bbox.YMin) < tolerance or 
                                abs(midpoint.y - bbox.YMax) < tolerance):
                                is_outer = True
                                    
                            if is_outer:
                                edges_to_fillet.append(edge)
                    except Exception:
                        continue  # Skip problematic edges

                if edges_to_fillet:
                    print(f"Found {len(edges_to_fillet)} outer flange edges parallel to Z-axis with thickness {thickness}")

                    # Try to apply fillet with the requested radius first, then progressively smaller radii
                    fillet_applied = False

                    # Start with the exact requested fillet radius
                    fillet_radius = fillet

                    # Try applying fillet to all edges first with requested radius
                    try:
                        fused_shape = fused_shape.makeFillet(fillet_radius, edges_to_fillet)
                        print(f"Successfully applied fillet radius {fillet_radius} to {len(edges_to_fillet)} edges")
                        fillet_applied = True
                    except Exception as e:
                        print(f"Failed to apply fillet radius {fillet_radius} to all edges: {e}")

                        # Try with half the requested radius
                        fillet_radius = fillet * 0.5
                        try:
                            fused_shape = fused_shape.makeFillet(fillet_radius, edges_to_fillet)
                            print(f"Applied reduced fillet radius {fillet_radius} to {len(edges_to_fillet)} edges")
                            fillet_applied = True
                        except Exception as e2:
                            print(f"Failed with half radius {fillet_radius}: {e2}")

                            # Try applying fillet to edges one by one with progressively smaller radii
                            successful_fillets = 0

                            # Try different fillet radii for individual edges, starting with requested radius
                            radii_to_try = [
                                fillet,  # Start with requested radius
                                fillet * 0.75,  # 75% of requested
                                fillet * 0.5,   # 50% of requested
                                fillet * 0.25,  # 25% of requested
                                min(thickness * 0.3, 1.0),  # Conservative fallback
                                0.1  # Very small fallback radius
                            ]

                            radius_usage = {}  # Track which radii were successfully used

                            for edge in edges_to_fillet:
                                edge_filleted = False
                                for fillet_radius in radii_to_try:
                                    try:
                                        fused_shape = fused_shape.makeFillet(fillet_radius, [edge])
                                        successful_fillets += 1
                                        edge_filleted = True

                                        # Track radius usage
                                        if fillet_radius not in radius_usage:
                                            radius_usage[fillet_radius] = 0
                                        radius_usage[fillet_radius] += 1

                                        break  # Success, move to next edge
                                    except Exception:
                                        continue  # Try next smaller radius

                                if not edge_filleted:
                                    continue  # Skip this edge completely

                            if successful_fillets > 0:
                                # Report which radii were actually used
                                radius_report = ", ".join([f"{r}mm({c} edges)" for r, c in radius_usage.items()])
                                print(f"Applied individual fillets to {successful_fillets}/{len(edges_to_fillet)} edges with radii: {radius_report}")
                                fillet_applied = True
                            else:
                                print("Could not apply fillet to any edges individually")

                    if not fillet_applied:
                        print("Fillet operation failed completely - continuing without fillet")
                else:
                    print(f"No edges found parallel to Z-axis with length ~{thickness} for filleting")

            except Exception as e:
                print(f"Warning: Could not apply fillet: {e}")

        fused_obj = App.ActiveDocument.addObject("Part::Feature", f"Tub_With_Outward_Bends_{walls_str}")
        fused_obj.Shape = fused_shape
        fused_obj.Label = f"Tub_With_Outward_Bends_{walls_str}"
        App.ActiveDocument.recompute()

        return fused_obj
    except Exception as e:
        print(f"Warning: Could not apply outward bends on walls {target_walls}: {e}")
        return shape

def AddHoleOutwardBend(tub_obj, length, width, height, bend_radius, additional_bend_length, edge_distance=10, hole_radius=5.0, hole_height=300.0, target_walls=None):
    """
    Create 4 cylindrical holes on flange at calculated positions

    Parameters:
    - width: Capot width (mm)
    - length: Capot length (mm)
    - bend_radius: Bend radius (mm)
    - additional_bend_length: Additional bend length (mm)
    - edge_distance: Distance from edge (mm) - default 10
    - hole_radius: Hole radius (mm) - default 5.0
    - hole_height: Hole height (mm) - default 300.0
    - tub_obj: Capot object to cut holes in
    - target_walls: List of walls that have flanges. Options are "front", "back", "left", "right".

    Returns:
    - FreeCAD object (capot with holes cut on flange)
    """

    if tub_obj is None:
        raise ValueError("tub_obj cannot be empty")

    if target_walls is None:
        target_walls = ["left", "right"]
    elif isinstance(target_walls, str):
        target_walls = [target_walls]

    z_coord = bend_radius + height 
    hole_positions = []

    # X direction bends (left/right walls)
    x_extended = length/2 + bend_radius + additional_bend_length - edge_distance
    y_normal = width/2 - edge_distance

    # Y direction bends (front/back walls)
    y_extended = width/2 + bend_radius + additional_bend_length - edge_distance
    x_normal = length/2 - edge_distance

    for wall in target_walls:
        wall_lower = wall.lower()
        if wall_lower == "front":
            hole_positions.extend([(x_normal, -y_extended, z_coord), (-x_normal, -y_extended, z_coord)])
        elif wall_lower in ["back", "rear"]:
            hole_positions.extend([(x_normal, y_extended, z_coord), (-x_normal, y_extended, z_coord)])
        elif wall_lower == "left":
            hole_positions.extend([(-x_extended, y_normal, z_coord), (-x_extended, -y_normal, z_coord)])
        elif wall_lower == "right":
            hole_positions.extend([(x_extended, y_normal, z_coord), (x_extended, -y_normal, z_coord)])
        else:
            print(f"Warning: Unknown wall '{wall}' for hole placement.")

    cutting_cylinders = []

    for i, (x, y, z) in enumerate(hole_positions):
        # Create cylinder to cut hole
        cylinder = Part.makeCylinder(hole_radius, hole_height)
        # Move cylinder to position
        cylinder = cylinder.translate(App.Vector(x, y, z))
        cutting_cylinders.append(cylinder)

    # Cut holes in tub
    modified_tub_shape = tub_obj.Shape

    # Cut each hole sequentially
    for i, cutting_cylinder in enumerate(cutting_cylinders):
        try:
            modified_tub_shape = modified_tub_shape.cut(cutting_cylinder)
            print(f"Created hole {i+1} at position ({hole_positions[i][0]:.1f}, {hole_positions[i][1]:.1f})")
        except Exception as e:
            print(f"Error cutting hole {i+1}: {e}")

    # Update tub shape
    tub_obj.Shape = modified_tub_shape
    App.ActiveDocument.recompute()

    return tub_obj

def AddInwardBend(shape, bend_length, bend_angle, bend_radius, target_walls=None, thickness=1.5):
    """
    Add inward bends to specific edges of a shape (bending toward the shape)

    Args:
        shape: The base shape object to add bends to
        bend_length: Length of the bend
        bend_angle: Angle of the bend in degrees
        bend_radius: Radius of the bend
        target_walls: List of walls to bend. Options are "front", "back" (or "rear"), "left", "right".
        thickness: Sheet thickness (used to locate the inner edges)

    Returns:
        The shape with inward bends applied, or original shape if bending fails
    """
    if target_walls is None:
        target_walls = ["left", "right"]
    elif isinstance(target_walls, str):
        target_walls = [target_walls]

    # Calculate dim_x and dim_y based on the base of the shape (Z < thickness + 0.1)
    min_x, max_x = float('inf'), float('-inf')
    min_y, max_y = float('inf'), float('-inf')
    
    for v in shape.Shape.Vertexes:
        if v.Z < thickness + 0.1:
            min_x = min(min_x, v.X)
            max_x = max(max_x, v.X)
            min_y = min(min_y, v.Y)
            max_y = max(max_y, v.Y)
            
    bbox = shape.Shape.BoundBox
    wall_height = bbox.ZMax

    if max_x > min_x and max_y > min_y:
        dim_x = max(abs(max_x), abs(min_x)) * 2.0
        dim_y = max(abs(max_y), abs(min_y)) * 2.0
    else:
        dim_x = max(abs(bbox.XMax), abs(bbox.XMin)) * 2.0
        dim_y = max(abs(bbox.YMax), abs(bbox.YMin)) * 2.0

    edge_candidates = []
    
    for wall in target_walls:
        wall_lower = wall.lower()
        if wall_lower in ["back", "rear"]:
            t1 = (-dim_x/2 + thickness + 0.1, dim_y/2 - thickness, wall_height)
            t2 = (dim_x/2 - thickness - 0.1, dim_y/2 - thickness, wall_height)
        elif wall_lower == "front":
            t1 = (-dim_x/2 + thickness + 0.1, -dim_y/2 + thickness, wall_height)
            t2 = (dim_x/2 - thickness - 0.1, -dim_y/2 + thickness, wall_height)
        elif wall_lower == "left":
            t1 = (-dim_x/2 + thickness, dim_y/2 - 0.1, wall_height)
            t2 = (-dim_x/2 + thickness, -dim_y/2 + 0.1, wall_height)
        elif wall_lower == "right":
            t1 = (dim_x/2 - thickness, dim_y/2 - 0.1, wall_height)
            t2 = (dim_x/2 - thickness, -dim_y/2 + 0.1, wall_height)
        else:
            print(f"Warning: Unknown wall '{wall}'. Skipping.")
            continue
            
        edge_name, _ = find_edge_by_coordinates(shape.Shape, t1, t2, tolerance=10.0)
        if edge_name:
            edge_candidates.append(edge_name)
        else:
            print(f"Warning: Could not dynamically find inner edge for wall '{wall}'.")

    if not edge_candidates:
        print("Warning: Could not find any inner edges to bend. Skipping bend.")
        return shape

    try:
        walls_str = "_".join(target_walls)
        wall_obj = App.ActiveDocument.addObject("Part::FeaturePython", f"SMBendWall_Inward_{walls_str}")
        SheetMetalCmd.SMBendWall(wall_obj, shape, edge_candidates)

        wall_obj.length = bend_length - bend_radius
        # For inward bending, we use the bend_angle directly instead of (180 - bend_angle)
        wall_obj.angle = bend_angle
        wall_obj.radius = bend_radius
        App.ActiveDocument.recompute()

        fused_obj = App.ActiveDocument.addObject("Part::Feature", f"Tub_With_Inward_Bends_{walls_str}")
        fused_obj.Shape = wall_obj.Shape
        fused_obj.Label = f"Tub_With_Inward_Bends_{walls_str}"
        App.ActiveDocument.recompute()

        return fused_obj
    except Exception as e:
        print(f"Warning: Could not apply inward bends on walls {target_walls}: {e}")
        return shape


def AddInwardBendExtended(shape, bend_length, bend_angle, bend_radius):
    """
    Add inward bends to extended set of edges of a shape (bending toward the shape)

    Args:
        shape: The base shape object to add bends to
        bend_length: Length of the bend
        bend_angle: Angle of the bend in degrees
        bend_radius: Radius of the bend

    Returns:
        The shape with inward bends applied, or original shape if bending fails
    """
    edge_candidates = ["Edge66", "Edge88", "Edge77", "Edge99"]

    try:
        # Create a single wall object with all edges at once
        wall_obj = App.ActiveDocument.addObject("Part::FeaturePython", "SMBendWall_InwardExt_All")
        SheetMetalCmd.SMBendWall(wall_obj, shape, edge_candidates)

        wall_obj.length = bend_length - bend_radius
        # For inward bending, we use the bend_angle directly instead of (180 - bend_angle)
        wall_obj.angle = bend_angle
        wall_obj.radius = bend_radius
        App.ActiveDocument.recompute()

        # Create the final fused object
        fused_obj = App.ActiveDocument.addObject("Part::Feature", "Tub_With_Extended_Inward_Bends")
        fused_obj.Shape = wall_obj.Shape
        fused_obj.Label = "Tub_With_Extended_Inward_Bends"
        App.ActiveDocument.recompute()

        return fused_obj
    except Exception as e:
        print(f"Warning: Could not apply extended inward bends: {e}")
        return shape


def find_edge_by_coordinates(shape, target_point1, target_point2, tolerance=0.1):
    """
    Find an edge that has vertices matching the target coordinates within tolerance

    Args:
        shape: The shape to analyze
        target_point1: First target point [x, y, z]
        target_point2: Second target point [x, y, z]
        tolerance: Coordinate matching tolerance

    Returns:
        Tuple of (edge_name, edge_object) or (None, None) if not found
    """
    target1 = App.Vector(target_point1[0], target_point1[1], target_point1[2])
    target2 = App.Vector(target_point2[0], target_point2[1], target_point2[2])

    for i, edge in enumerate(shape.Edges):
        # Get the two vertices of the edge
        vertices = edge.Vertexes
        if len(vertices) == 2:
            v1 = vertices[0].Point
            v2 = vertices[1].Point

            # Check if vertices match target points (in either order)
            match1 = (v1.distanceToPoint(target1) < tolerance and v2.distanceToPoint(target2) < tolerance)
            match2 = (v1.distanceToPoint(target2) < tolerance and v2.distanceToPoint(target1) < tolerance)

            if match1 or match2:
                edge_name = f"Edge{i+1}"
                print(f"Found matching edge: {edge_name}")
                print(f"  Vertex 1: X={v1.x:.2f}, Y={v1.y:.2f}, Z={v1.z:.2f}")
                print(f"  Vertex 2: X={v2.x:.2f}, Y={v2.y:.2f}, Z={v2.z:.2f}")
                return edge_name, edge

    return None, None


def get_capot_wall_frame(wall, bend_angle, dim_x, dim_y, thickness):
    """
    Local 3D frame for placing holes/cuts on a CAPOT wall bent at an
    ARBITRARY bend_angle (not just 90 deg). Use this instead of the fixed
    90-deg dir/pnt table whenever bend_angle != 90.

    - u_dir: along the wall's length (X for front/back, Y for left/right).
             Unaffected by bend_angle - same as the existing X/Y coordinate
             already used for that wall at 90 deg.
    - v_dir: "up" the wall face from the bend line. Replaces the old fixed
             Z-height assumption (world Z = thickness + h) - use
             `origin + u*u_dir + h*v_dir` instead, where h is the same
             distance-from-bend-line value used before.
    - n_dir: cutting tool axis through the wall thickness. Replaces the old
             fixed dir vector, e.g. (0,1,0) for front wall.

    Works directly as `dir`/`pnt` for rotationally symmetric tools
    (makeCylinder, makeHexagon, makeThreaded, makeCountersink). For
    asymmetric tools (makeBox, makeOblong, makeKeyhole), build the tool at
    local origin (0,0,0) with axes X=u, Y=v, Z=n, then pass it through
    `place_on_capot_wall()` below.

    Returns: (origin, u_dir, v_dir, n_dir) as App.Vector.
    """
    wall = wall.lower()
    D = math.radians(180.0 - bend_angle)  # SMBendWall deformation angle used by makeTub

    if wall == "front":
        origin = App.Vector(0.0, -dim_y / 2.0, thickness)
        u_dir = App.Vector(1, 0, 0)
        v_dir = App.Vector(0.0, -math.cos(D), math.sin(D))
        n_dir = App.Vector(0.0, math.sin(D), math.cos(D))
    elif wall in ("back", "rear"):
        origin = App.Vector(0.0, dim_y / 2.0, thickness)
        u_dir = App.Vector(1, 0, 0)
        v_dir = App.Vector(0.0, math.cos(D), math.sin(D))
        n_dir = App.Vector(0.0, -math.sin(D), math.cos(D))
    elif wall == "left":
        origin = App.Vector(-dim_x / 2.0, 0.0, thickness)
        u_dir = App.Vector(0, 1, 0)
        v_dir = App.Vector(-math.cos(D), 0.0, math.sin(D))
        n_dir = App.Vector(math.sin(D), 0.0, math.cos(D))
    elif wall == "right":
        origin = App.Vector(dim_x / 2.0, 0.0, thickness)
        u_dir = App.Vector(0, 1, 0)
        v_dir = App.Vector(math.cos(D), 0.0, math.sin(D))
        n_dir = App.Vector(-math.sin(D), 0.0, math.cos(D))
    else:
        raise ValueError(f"Unknown wall '{wall}'")

    return origin, u_dir, v_dir, n_dir


def resolve_capot_wall_hole_position(shape, wall, bend_angle, dim_x, dim_y, thickness,
                                      u, v, cut_depth, bend_radius, margin=1.0, min_face_area=1000.0):
    """
    Resolve a hole's real cutting start-point on a CAPOT wall bent at an
    ARBITRARY bend_angle, by snapping the analytically-computed (u, v)
    position from get_capot_wall_frame() onto the REAL bent wall face on
    `shape` (via Face.distToShape), instead of trusting the flat-frame
    formula alone.

    Why this is needed: get_capot_wall_frame()'s directions (u_dir/v_dir/
    n_dir) are always geometrically exact (validated against real FreeCAD
    face normals at 40/90/125 deg - always an exact match). But at extreme
    bend_angle values, SMBendWall's corner relief/merge (see makeTub
    docstring) shifts the real wall surface's plane offset by an amount
    that a fixed `bend_radius` correction does not reliably capture (a few
    mm error was enough to make cuts silently miss entirely at 40 deg,
    while working fine at 90/125 deg with the same fixed offset). Snapping
    onto the actual geometry sidesteps needing an exact analytical model of
    SheetMetal's relief geometry.

    Args:
        shape: the real bent CAPOT shape (e.g. tub_obj.Shape) to snap against.
        wall, bend_angle, dim_x, dim_y, thickness: same as get_capot_wall_frame().
        u, v: nominal position along the wall's u_dir/v_dir (same convention
              as get_capot_wall_frame's docstring: origin + u*u_dir + v*v_dir).
        cut_depth: desired through-cut depth (e.g. thickness + 2.0).
        bend_radius: bend radius used for makeTub (also used as base pull-back).
        margin: extra safety margin (mm) added on top of bend_radius so the
                tool reliably starts outside the material and clears fully
                through it. Default 1.0mm.
        min_face_area: ignore small side/relief faces smaller than this (mm^2)
                       when searching for the wall's main outer face.

    Returns: (start_point, n_dir, depth) - start_point already pulled back
    outside the material along -n_dir, depth padded by 2*margin. Use these
    directly to build the cutting tool (makeBox/makeOblong/makeCylinder/
    makeHexagon), then place_on_capot_wall() for non-symmetric tools.

    Raises ValueError if no matching wall face is found on `shape`.
    """
    origin, u_dir, v_dir, n_dir = get_capot_wall_frame(wall, bend_angle, dim_x, dim_y, thickness)
    nominal_point = origin + u_dir * u + v_dir * v

    face = None
    best_area = -1.0
    for f in shape.Faces:
        if not isinstance(f.Surface, Part.Plane):
            continue
        if f.Area < min_face_area:
            continue
        try:
            normal = f.normalAt(0.5, 0.5)
        except Exception:
            continue
        if normal.dot(n_dir) > 0.999 and f.Area > best_area:
            face = f
            best_area = f.Area

    if face is None:
        raise ValueError(f"No matching outer face found for wall '{wall}' at bend_angle={bend_angle}")

    _, pts, _ = face.distToShape(Part.Vertex(nominal_point))
    snapped_point = pts[0][0]

    start = snapped_point - n_dir * (bend_radius + margin)
    depth = cut_depth + 2.0 * margin
    return start, n_dir, depth


def place_on_capot_wall(shape, origin, u_dir, v_dir, n_dir):
    """
    Transform a cutting tool built in LOCAL coordinates (X=u_dir, Y=v_dir,
    Z=n_dir, at local origin 0,0,0) onto its real position/orientation on an
    angled CAPOT wall. Pair with get_capot_wall_frame().

    Example (rectangular hole on a wall bent at a custom angle):
        origin, u_dir, v_dir, n_dir = get_capot_wall_frame("front", bend_angle, dim_x, dim_y, thickness)
        tool = Part.makeBox(size_u, size_v, depth, App.Vector(-size_u/2.0, -size_v/2.0 + h, 0.0))
        tool = place_on_capot_wall(tool, origin, u_dir, v_dir, n_dir)
        final_shape = final_shape.cut(tool)
    """
    m = App.Matrix(
        u_dir.x, v_dir.x, n_dir.x, origin.x,
        u_dir.y, v_dir.y, n_dir.y, origin.y,
        u_dir.z, v_dir.z, n_dir.z, origin.z,
        0.0, 0.0, 0.0, 1.0
    )
    return shape.transformGeometry(m)
