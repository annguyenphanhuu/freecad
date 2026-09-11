# TubeFunction.py - Functions related to tube/cylinder operations
# -*- coding: utf-8 -*-
#
# COORDINATE SYSTEM CONVENTION (after refactor):
#   - Y-axis : length (longitudinal direction, tube extends from Y=0 to Y=length)
#   - X-axis : width  (cross-section horizontal dimension)
#   - Z-axis : height (cross-section vertical dimension)
#
import FreeCAD as App
import Part
import math
import sys
import os

# Add sheetmetal directory to path (it's at project root level)
sheetmetal_path = os.path.join(os.path.dirname(__file__), '..', 'sheetmetal')
if sheetmetal_path not in sys.path:
    sys.path.insert(0, sheetmetal_path)

import SheetMetalTools

def taskRestoreDefaults(obj, default_vars):
    pass
SheetMetalTools.taskRestoreDefaults = taskRestoreDefaults

def makeRectangularTube(length, outer_width, outer_height, thickness,
                       outer_fillet_radius=0.0, inner_fillet_radius=0.0):
    """
    Create a basic rectangular tube with optional fillets.

    Coordinate system:
        - Y-axis: length (longitudinal direction, tube extends from Y=0 to Y=length)
        - X-axis: outer_width  (cross-section width, horizontal)
        - Z-axis: outer_height (cross-section height, vertical)
        - Origin at (0,0,0) — front-bottom-left corner of the tube

    Face nomenclature:
        - TOP FACE    (Z-max): dimensions are outer_width × length
        - BOTTOM FACE (Z-min): dimensions are outer_width × length
        - FRONT FACE  (X-min): dimensions are length × outer_height
        - BACK FACE   (X-max): dimensions are length × outer_height

    Args:
        length (float): Length of the tube along Y-axis
        outer_width (float): Outer width of the tube (X dimension)
        outer_height (float): Outer height of the tube (Z dimension)
        thickness (float): Wall thickness of the tube
        outer_fillet_radius (float): Fillet radius for outer edges (default: 0.0)
        inner_fillet_radius (float): Fillet radius for inner edges (default: 0.0)

    Returns:
        Part.Shape: The basic rectangular tube shape
    """
    # Calculate inner dimensions
    inner_width = outer_width - 2 * thickness
    inner_height = outer_height - 2 * thickness

    # Validate dimensions
    if inner_width <= 0 or inner_height <= 0:
        raise ValueError("Wall thickness is too large for the given outer dimensions")

    # Create outer box: X=width, Y=length, Z=height
    outer_box = Part.makeBox(outer_width, length, outer_height, App.Vector(0, 0, 0))

    # Apply fillet to outer box edges if specified
    if outer_fillet_radius > 0:
        try:
            outer_edges = []
            for edge in outer_box.Edges:
                # Find edges parallel to the length (Y-axis)
                if abs(edge.Length - length) < 0.001:
                    outer_edges.append(edge)

            if outer_edges:
                outer_box_filleted = outer_box.makeFillet(outer_fillet_radius, outer_edges)
            else:
                outer_box_filleted = outer_box
                print("Warning: No suitable edges found for outer fillet")
        except Exception as e:
            print(f"Warning: Could not apply outer fillet: {e}")
            outer_box_filleted = outer_box
    else:
        outer_box_filleted = outer_box

    # Create inner box positioned centrally to achieve uniform wall thickness
    # Offset: X by thickness, Y=0 (full length), Z by thickness
    inner_box = Part.makeBox(inner_width, length, inner_height,
                             App.Vector(thickness, 0, thickness))

    # Apply fillet to inner box edges if specified
    if inner_fillet_radius > 0:
        try:
            inner_edges = []
            for edge in inner_box.Edges:
                # Find edges parallel to the length (Y-axis)
                if abs(edge.Length - length) < 0.001:
                    inner_edges.append(edge)

            if inner_edges:
                inner_box_filleted = inner_box.makeFillet(inner_fillet_radius, inner_edges)
            else:
                inner_box_filleted = inner_box
                print("Warning: No suitable edges found for inner fillet")
        except Exception as e:
            print(f"Warning: Could not apply inner fillet: {e}")
            inner_box_filleted = inner_box
    else:
        inner_box_filleted = inner_box

    # Boolean cut to form the tube
    tube_shape = outer_box_filleted.cut(inner_box_filleted)

    return tube_shape


def makeCircularTube(length, outer_diameter, thickness):
    """
    Create a basic circular tube (hollow cylinder).

    Coordinate system:
        - Y-axis: length (longitudinal direction, tube extends from Y=0 to Y=length)
        - X-Z plane: circular cross-section (centered at origin)
        - Origin at (0,0,0) — center of the circular cross-section at tube start (Y=0)

    Construction method:
        1. Create outer cylinder: makeCylinder(outer_radius, length) along Y-axis at (0,0,0)
        2. Create inner cylinder: makeCylinder(inner_radius, length) along Y-axis at (0,0,0)
           where inner_diameter = outer_diameter - 2*thickness, inner_radius = inner_diameter/2
        3. Boolean cut: outer - inner → hollow tube

    Resulting shape:
        - Hollow cylinder extending from Y=0 to Y=length
        - Circular cross-section in X-Z plane with outer_diameter and wall thickness

    Args:
        length (float): Length of the tube along Y-axis
        outer_diameter (float): Outer diameter of the tube
        thickness (float): Wall thickness of the tube

    Returns:
        Part.Shape: The basic circular tube shape
    """
    # Calculate inner diameter
    inner_diameter = outer_diameter - 2 * thickness

    # Validate dimensions
    if inner_diameter <= 0:
        raise ValueError("Wall thickness is too large for the given outer diameter")

    # Calculate radii
    outer_radius = outer_diameter / 2
    inner_radius = inner_diameter / 2

    # Create outer cylinder along Y-axis
    outer_cylinder = Part.makeCylinder(outer_radius, length, App.Vector(0, 0, 0), App.Vector(0, 1, 0))

    # Create inner cylinder (hollow part) along Y-axis
    inner_cylinder = Part.makeCylinder(inner_radius, length, App.Vector(0, 0, 0), App.Vector(0, 1, 0))

    # Boolean cut to form the tube
    tube_shape = outer_cylinder.cut(inner_cylinder)

    return tube_shape



def create_square_tube_angled_cuts(tube_shape, length, outer_width, outer_height, left_cut_angle=45.0, right_cut_angle=60.0):
    """
    Apply angled cuts to both ends of a rectangular tube oriented along the Y-axis.

    Convention:
        - "left" end  = START of tube at Y=0
        - "right" end = END   of tube at Y=length
        - Angle is measured from the tube axis (Y) in the Y-Z plane
        - 0.0  = no cut (straight, 90° to tube axis) — skipped
        - 45.0 = 45° bevel cut
        - 90.0 = parallel to tube axis — invalid, skipped

    The cut plane tilts from bottom (Z=0) at one Y position to top (Z=outer_height)
    at another Y position, creating a diagonal bevel on the tube end.

    Args:
        tube_shape (Part.Shape): The tube shape to cut
        length (float): Length of the tube along Y-axis
        outer_width (float): Outer width of the tube (X dimension)
        outer_height (float): Outer height of the tube (Z dimension)
        left_cut_angle (float): Bevel angle at Y=0 (start) in degrees (default: 45.0)
        right_cut_angle (float): Bevel angle at Y=length (end) in degrees (default: 60.0)

    Returns:
        Part.Shape: The tube shape with angled cuts applied
    """
    # Convert angles to radians
    left_cut_angle_rad  = math.radians(left_cut_angle)
    right_cut_angle_rad = math.radians(right_cut_angle)

    # Skip 0-degree and 90-degree cuts
    if abs(left_cut_angle) < 0.001:
        print(f"Skipping left cut: angle {left_cut_angle}° is too close to 0°")
        left_cut_enabled = False
    elif abs(left_cut_angle - 90.0) < 0.001:
        print(f"Skipping left cut: angle {left_cut_angle}° is too close to 90°")
        left_cut_enabled = False
    else:
        left_cut_length_offset = outer_height / math.tan(left_cut_angle_rad)
        left_cut_enabled = True

    if abs(right_cut_angle) < 0.001:
        print(f"Skipping right cut: angle {right_cut_angle}° is too close to 0°")
        right_cut_enabled = False
    elif abs(right_cut_angle - 90.0) < 0.001:
        print(f"Skipping right cut: angle {right_cut_angle}° is too close to 90°")
        right_cut_enabled = False
    else:
        right_cut_length_offset = outer_height / math.tan(right_cut_angle_rad)
        right_cut_enabled = True

    # Create cutting plane at the left end (Y=0) if enabled
    # Tube is X=0..outer_width, Z=0..outer_height, Y=0..length
    if left_cut_enabled:
        # Left end: bevel goes from bottom (Z=0) at Y=0 to top (Z=outer_height) at Y=offset
        left_cut_points = [
            App.Vector(0,           0,                      0),             # Bottom-front
            App.Vector(outer_width, 0,                      0),             # Bottom-back
            App.Vector(outer_width, left_cut_length_offset, outer_height),  # Top-back
            App.Vector(0,           left_cut_length_offset, outer_height),  # Top-front
        ]

    # Create cutting plane at the right end (Y=length) if enabled
    if right_cut_enabled:
        # Right end: bevel goes from top (Z=outer_height) at Y=length-offset to bottom (Z=0) at Y=length
        right_cut_points = [
            App.Vector(0,           length - right_cut_length_offset, outer_height),  # Top-front
            App.Vector(outer_width, length - right_cut_length_offset, outer_height),  # Top-back
            App.Vector(outer_width, length,                           0),              # Bottom-back
            App.Vector(0,           length,                           0),              # Bottom-front
        ]

    # Create cutting solids using extrusion along Y-axis
    if left_cut_enabled:
        # Left cutting solid — extrude in -Y direction to remove the unwanted portion
        left_cut_face  = Part.Face(Part.makePolygon(left_cut_points + [left_cut_points[0]]))
        left_cut_solid = left_cut_face.extrude(App.Vector(0, -(left_cut_length_offset + 5), 0))

    if right_cut_enabled:
        # Right cutting solid — extrude in +Y direction to remove the unwanted portion
        right_cut_face  = Part.Face(Part.makePolygon(right_cut_points + [right_cut_points[0]]))
        right_cut_solid = right_cut_face.extrude(App.Vector(0, right_cut_length_offset + 5, 0))

    # Apply the angled cuts to the tube
    try:
        if left_cut_enabled:
            tube_shape = tube_shape.cut(left_cut_solid)
        if right_cut_enabled:
            tube_shape = tube_shape.cut(right_cut_solid)
    except Exception as e:
        print(f"Warning: Could not apply angled cuts: {e}")

    return tube_shape

