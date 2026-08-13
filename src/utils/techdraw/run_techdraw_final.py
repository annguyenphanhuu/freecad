import FreeCAD
import Part
import os
import sys
import math
from xml.etree import ElementTree as ET

# --- Configuration ---
script_dir = os.path.dirname(os.path.abspath(__file__))
step_file_name = "sheet.step"
output_svg_name = "output.svg"
template_name = "A4_TOLERY.svg"

STEP_FILE_PATH = os.path.join(script_dir, "input", step_file_name)
OUTPUT_SVG_PATH = os.path.join(script_dir, "output", output_svg_name)
TEMPLATE_PATH = os.path.join(script_dir, "templates", template_name)

# --- Hole Detection Configuration ---
MIN_HOLE_RADIUS = 0.5
MAX_HOLE_RADIUS = 50
SHOW_CENTER_LINES = True
SHOW_RADIUS_DIMENSIONS = True

# Check if files exist
if not os.path.exists(STEP_FILE_PATH):
    print(f"Error: STEP file not found at '{STEP_FILE_PATH}'")
    sys.exit(1)
if not os.path.exists(TEMPLATE_PATH):
    print(f"Error: Template file not found at '{TEMPLATE_PATH}'")
    sys.exit(1)

def project_point(point, direction):
    if direction == "front": return (point.x, point.z)
    elif direction == "top": return (point.x, point.y)
    elif direction == "right": return (-point.y, point.z) # Looking from the right
    return (point.x, point.y)

def create_svg_path_from_edges(edges, direction, scale, offset_x, offset_y):
    path_data_list = []
    for edge in edges:
        points = edge.discretize(20)
        path_data = "M " + " L ".join(f"{project_point(p, direction)[0] * scale + offset_x:.3f},{-(project_point(p, direction)[1] * scale) + offset_y:.3f}" for p in points)
        path_data_list.append(path_data)
    return path_data_list



def add_dimension(svg_group, p1, p2, text, position='top', offset=10, text_offset=3):
    """Adds a standard ISO dimension to the SVG group at a specified position."""
    (x1, y1), (x2, y2) = p1, p2
    style = {'stroke': 'black', 'stroke-width': '0.25', 'fill': 'none'}
    dim_group = ET.SubElement(svg_group, 'g', {'class': 'dimension'})

    if position in ['left', 'right']: # Vertical Dimension
        is_right = position == 'right'
        line_x = (max(x1, x2) + offset) if is_right else (min(x1, x2) - offset)
        ext_x1_start, ext_x1_end = (x1, x1 + 2) if is_right else (x1 - 2, x1)
        ext_x2_start, ext_x2_end = (x2, x2 + 2) if is_right else (x2 - 2, x2)

        ET.SubElement(dim_group, 'path', {**style, 'd': f'M {ext_x1_start} {y1} L {ext_x1_end} {y1}'})
        ET.SubElement(dim_group, 'path', {**style, 'd': f'M {ext_x2_start} {y2} L {ext_x2_end} {y2}'})
        ET.SubElement(dim_group, 'path', {**style, 'd': f'M {line_x} {y1} L {line_x} {y2}', 'marker-start': 'url(#arrowhead)', 'marker-end': 'url(#arrowhead)'})

        text_anchor = 'start' if is_right else 'end'
        text_x = line_x + text_offset if is_right else line_x - text_offset
        text_elem = ET.Element('text', {'x': str(text_x), 'y': str((y1 + y2) / 2), 'text-anchor': text_anchor, 'dominant-baseline': 'middle', 'font-size': '3.5'})
        text_elem.text = text
        dim_group.append(text_elem)

    elif position in ['top', 'bottom']: # Horizontal Dimension
        is_bottom = position == 'bottom'
        line_y = (max(y1, y2) + offset) if is_bottom else (min(y1, y2) - offset)
        ext_y1_start, ext_y1_end = (y1, y1 + 2) if is_bottom else (y1 - 2, y1)
        ext_y2_start, ext_y2_end = (y2, y2 + 2) if is_bottom else (y2 - 2, y2)

        ET.SubElement(dim_group, 'path', {**style, 'd': f'M {x1} {ext_y1_start} L {x1} {ext_y1_end}'})
        ET.SubElement(dim_group, 'path', {**style, 'd': f'M {x2} {ext_y2_start} L {x2} {ext_y2_end}'})
        ET.SubElement(dim_group, 'path', {**style, 'd': f'M {x1} {line_y} L {x2} {line_y}', 'marker-start': 'url(#arrowhead)', 'marker-end': 'url(#arrowhead)'})

        text_y = line_y + text_offset if is_bottom else line_y - text_offset
        dominant_baseline = 'hanging' if is_bottom else 'auto'
        text_elem = ET.Element('text', {'x': str((x1 + x2) / 2), 'y': str(text_y), 'text-anchor': 'middle', 'dominant-baseline': dominant_baseline, 'font-size': '3.5'})
        text_elem.text = text
        dim_group.append(text_elem)


def add_hole_center_lines(svg_group, hole_info, view_info, scale, offset_x, offset_y):
    """Adds center lines for a circular hole to the SVG group."""
    center_3d = hole_info['center']
    radius = hole_info['radius']
    normal = hole_info['normal']
    view_dir_str = view_info['dir']

    # Project the center point onto the view plane
    center_2d_proj = project_point(center_3d, view_dir_str)
    center_x = center_2d_proj[0] * scale + offset_x
    center_y = -center_2d_proj[1] * scale + offset_y

    # Determine if the hole is visible in this view
    view_vectors = {
        "front": FreeCAD.Vector(0, 1, 0), # Y-axis
        "top":   FreeCAD.Vector(0, 0, 1), # Z-axis
        "right": FreeCAD.Vector(1, 0, 0)  # X-axis
    }
    view_vector = view_vectors[view_dir_str]

    # Check if the hole's normal is parallel to the view vector (face-on view)
    if abs(normal.dot(view_vector)) > 0.99:
        style = {'stroke': 'black', 'stroke-width': '0.25', 'stroke-dasharray': '4 2'}
        center_group = ET.SubElement(svg_group, 'g', {'class': 'center-lines'})

        # Add center lines
        line_length = radius * scale * 1.5
        ET.SubElement(center_group, 'path', {**style, 'd': f'M {center_x - line_length} {center_y} L {center_x + line_length} {center_y}'})
        ET.SubElement(center_group, 'path', {**style, 'd': f'M {center_x} {center_y - line_length} L {center_x} {center_y + line_length}'})

def detect_circular_holes(shape, min_radius=0.5, max_radius=50):
    """Detects circular holes in the shape and returns their properties."""
    holes = []

    # Analyze all faces to find cylindrical holes
    for face in shape.Faces:
        if hasattr(face.Surface, 'Radius'):  # Cylindrical surface
            radius = face.Surface.Radius

            # Filter by radius range
            if min_radius <= radius <= max_radius:
                # Get the axis and center of the cylinder
                axis = face.Surface.Axis
                center = face.Surface.Center

                # Check if this is actually a hole (not an external cylinder)
                # We do this by checking if the face is internal (surrounded by material)

                # A more robust way to check for a hole is to check the face's orientation.
                # An 'inward' or 'Reversed' orientation typically signifies a hole.
                if face.Orientation == 'Reversed':
                    hole_info = {
                        'center': center,
                        'radius': radius,
                        'axis': axis,
                        'normal': axis.normalize(),
                        'face': face
                    }
                    holes.append(hole_info)

    # Remove duplicates (same center and radius)
    unique_holes = []
    for hole in holes:
        is_duplicate = False
        for existing in unique_holes:
            center_dist = hole['center'].distanceToPoint(existing['center'])
            radius_diff = abs(hole['radius'] - existing['radius'])
            if center_dist < 0.1 and radius_diff < 0.01:
                is_duplicate = True
                break
        if not is_duplicate:
            unique_holes.append(hole)

    return unique_holes

def add_radius_dimension(svg_group, hole_info, view_info, scale, offset_x, offset_y):
    """Adds radius dimension for a circular hole."""
    center_3d = hole_info['center']
    radius = hole_info['radius']
    normal = hole_info['normal']
    view_dir_str = view_info['dir']

    # Project the center point onto the view plane
    center_2d_proj = project_point(center_3d, view_dir_str)
    center_x = center_2d_proj[0] * scale + offset_x
    center_y = -center_2d_proj[1] * scale + offset_y

    # Determine if the hole is visible in this view (face-on)
    view_vectors = {
        "front": FreeCAD.Vector(0, 1, 0),
        "top":   FreeCAD.Vector(0, 0, 1),
        "right": FreeCAD.Vector(1, 0, 0)
    }
    view_vector = view_vectors[view_dir_str]

    # Only add radius dimension if hole is face-on in this view
    if abs(normal.dot(view_vector)) > 0.99:
        radius_scaled = radius * scale

        # Position the radius dimension line at 45 degrees
        angle = math.pi / 4  # 45 degrees
        end_x = center_x + radius_scaled * math.cos(angle)
        end_y = center_y + radius_scaled * math.sin(angle)

        # Leader line from center to radius point
        style = {'stroke': 'black', 'stroke-width': '0.25', 'fill': 'none'}
        dim_group = ET.SubElement(svg_group, 'g', {'class': 'radius-dimension'})

        # Radius line
        ET.SubElement(dim_group, 'path', {
            **style,
            'd': f'M {center_x} {center_y} L {end_x} {end_y}',
            'marker-end': 'url(#arrowhead)'
        })

        # Radius text
        text_x = end_x + 5
        text_y = end_y - 2
        text_elem = ET.Element('text', {
            'x': str(text_x),
            'y': str(text_y),
            'text-anchor': 'start',
            'dominant-baseline': 'middle',
            'font-size': '3.5'
        })
        text_elem.text = f"R{radius:.1f}"
        dim_group.append(text_elem)
# --- Main Script ---
doc = FreeCAD.newDocument("TechDrawFinal")
shape = Part.Shape()
# Prefer a sibling .brep next to the .step if one exists (written by
# worker.py's fast-wrapper right when the generating script exports STEP).
# .brep import is ~120-430x faster than parsing .step (measured on
# step_converter.py's own import path) since it's a direct dump of OCC's
# in-memory structures instead of text that has to be re-resolved. Falls
# back to the normal .step read if no sibling exists or the brep read fails
# for any reason.
_brep_path = os.path.splitext(STEP_FILE_PATH)[0] + ".brep"
if os.path.exists(_brep_path):
    try:
        shape.importBrep(_brep_path)
        print(f"Imported via sibling .brep (fast path): {_brep_path}")
    except Exception as _e:
        print(f"Sibling .brep import failed ({_e}), falling back to STEP")
        shape = Part.Shape()
        shape.read(STEP_FILE_PATH)
else:
    shape.read(STEP_FILE_PATH)
part_object = doc.addObject("Part::Feature", "Imported_STEP")
part_object.Shape = shape
doc.recompute()
print("STEP file imported successfully.")

# Detect circular holes
print("Detecting circular holes...")
holes = detect_circular_holes(shape, min_radius=MIN_HOLE_RADIUS, max_radius=MAX_HOLE_RADIUS)
print(f"Found {len(holes)} circular holes")

# Load SVG template
print(f"Loading template: {TEMPLATE_PATH}")
ET.register_namespace('', "http://www.w3.org/2000/svg")
tree = ET.parse(TEMPLATE_PATH)
root = tree.getroot()

# Find or create defs section and add arrowhead marker
ns = {'svg': 'http://www.w3.org/2000/svg'}
defs = root.find('svg:defs', ns)
if defs is None:
    defs = ET.SubElement(root, 'defs')
arrow_marker = ET.Element('marker', {'id': 'arrowhead', 'viewBox': '0 0 10 10', 'refX': '5', 'refY': '5', 'markerWidth': '6', 'markerHeight': '6', 'orient': 'auto-start-reverse'})
ET.SubElement(arrow_marker, 'path', {'d': 'M 0 0 L 10 5 L 0 10 z', 'fill': 'black'})
defs.append(arrow_marker)

# Create a group for our drawings
drawing_group = ET.SubElement(root, 'g', id='TechDrawViews')

# --- Auto-scaling and Layout ---
bbox = shape.BoundBox
length, width, height = bbox.XLength, bbox.YLength, bbox.ZLength

# Define available drawing area (A4 landscape minus borders and title block)
# Working space from template: 10 10 287 200. Title block starts at y=153
drawing_area_x_start = 10
drawing_area_y_start = 10
drawing_area_width = 277
drawing_area_height = 143 # Height above title block
view_padding = 20 # Padding between views
dimension_space = 25 # Extra space reserved for dimensions below the views

# Total size needed for the 3-view layout (Top, Front, Right) including dimension space
total_layout_width_mm = length + view_padding + width
total_layout_height_mm = height + view_padding + width + dimension_space # Add space for bottom dimensions

# Calculate optimal scale to fit the layout within the drawing area
scale_x = drawing_area_width / total_layout_width_mm
scale_y = drawing_area_height / total_layout_height_mm
scale = min(scale_x, scale_y) * 0.85 # Use 85% of available space for a larger margin

print(f"Object dimensions (L,W,H): {length:.1f}, {width:.1f}, {height:.1f}")
print(f"Calculated scale: {scale:.2f}")

# Calculate the total scaled dimensions of the entire layout block
scaled_total_width = length * scale + view_padding + width * scale
scaled_total_height = height * scale + view_padding + width * scale + dimension_space # Add space for bottom dimensions

# Calculate the top-left starting point (origin) for the entire layout block to center it
layout_origin_x = drawing_area_x_start + (drawing_area_width - scaled_total_width) / 2
layout_origin_y = drawing_area_y_start + (drawing_area_height - scaled_total_height) / 2

# Define absolute top-left positions for each view box
# Standard layout: Top view is above Front view, Right view is to the right of Front view
top_view_pos = (layout_origin_x, layout_origin_y)
front_view_pos = (layout_origin_x, layout_origin_y + width * scale + view_padding)
right_view_pos = (layout_origin_x + length * scale + view_padding, layout_origin_y + width * scale + view_padding)

views = {
    "top":   {"dir": "top",   "pos": top_view_pos},
    "front": {"dir": "front", "pos": front_view_pos},
    "right": {"dir": "right", "pos": right_view_pos}
}

for name, view in views.items():
    print(f"Generating {name} view...")
    view_group = ET.SubElement(drawing_group, 'g', id=f'{name}View', stroke='black', fill='none', **{'stroke-width': '0.35'})

    # Get the projected min/max points for the current view direction
    projected_points = [project_point(v.Point, view['dir']) for v in shape.Vertexes]
    projected_min_x = min(p[0] for p in projected_points)
    projected_max_x = max(p[0] for p in projected_points)
    projected_min_y = min(p[1] for p in projected_points)
    projected_max_y = max(p[1] for p in projected_points) # Use max_y for top-edge alignment in SVG's Y-down coord system

    # Calculate translation to move the object's projected origin (min corner) to the view's top-left position
    trans_x = view['pos'][0] - projected_min_x * scale
    # The SVG Y-axis is inverted. To align the top edge of the shape with the view's Y position,
    # we must offset by the *highest* projected Y value.
    trans_y = view['pos'][1] + projected_max_y * scale

    paths = create_svg_path_from_edges(shape.Edges, view['dir'], scale, trans_x, trans_y)
    for path_data in paths:
        ET.SubElement(view_group, 'path', d=path_data)

    # Add hole center lines and radius dimensions
    if holes:
        for hole in holes:
            if SHOW_CENTER_LINES:
                add_hole_center_lines(view_group, hole, view, scale, trans_x, trans_y)
            if SHOW_RADIUS_DIMENSIONS:
                add_radius_dimension(view_group, hole, view, scale, trans_x, trans_y)

# --- Add Optimized Dimensions ---
print("Adding optimized dimensions...")

# Re-access positions from the views dictionary for clarity
front_view_pos = views['front']['pos']
right_view_pos = views['right']['pos']

# Front View: Length (bottom) and Height (left)
p_front_bl = (front_view_pos[0], front_view_pos[1] + height * scale) # bottom-left
p_front_br = (front_view_pos[0] + length * scale, front_view_pos[1] + height * scale) # bottom-right
p_front_tl = (front_view_pos[0], front_view_pos[1]) # top-left
add_dimension(drawing_group, p_front_bl, p_front_br, f"{length:.0f}", position='bottom')
add_dimension(drawing_group, p_front_tl, p_front_bl, f"{height:.0f}", position='left')

# Right View: Width (bottom)
p_right_bl = (right_view_pos[0], right_view_pos[1] + height * scale) # bottom-left
p_right_br = (right_view_pos[0] + width * scale, right_view_pos[1] + height * scale) # bottom-right
add_dimension(drawing_group, p_right_bl, p_right_br, f"{width:.0f}", position='bottom')

# Write the final SVG file
print(f"Writing final SVG to: {OUTPUT_SVG_PATH}")
tree.write(OUTPUT_SVG_PATH, encoding='utf-8', xml_declaration=True)

print("\nProcess completed successfully!")
FreeCAD.closeDocument(doc.Name)
