#!/usr/bin/env python3
"""
Reusable STEP to OnShape JSON Converter for FreeCAD

This script is designed to be executed by freecadcmd.exe and converts a
STEP file into an OnShape-compatible JSON format with semantic feature extraction.

Usage:
freecadcmd.exe <path_to_script> <input_step_path> <output_json_path>
"""

import os
import json
import sys
import time
import math
import itertools
import subprocess
import tempfile
import array as _array_mod
import base64
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

# Alias for fast float array construction (binary packing)
_FloatArray = _array_mod.array

# Import 3D geometry utilities
try:
    from geometry_utils_3d import (
        euclidean_distance_3d, dbscan_3d, fit_plane_3d, distance_to_plane_3d,
        bbox_3d_intersection, expand_bbox_3d, group_cylinders_by_radius,
        calculate_adaptive_epsilon, dot_product_3d
    )
    HAS_GEOMETRY_UTILS = True
except ImportError:
    HAS_GEOMETRY_UTILS = False
    print("[WARNING] geometry_utils_3d.py not found. Tier 2 detection will be limited.")

# Import robust file utilities for atomic JSON writes
try:
    import sys
    from pathlib import Path
    # Add project root to path to import from src.utils
    script_dir = Path(__file__).parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    
    from src.utils.file_utils import safe_write_json_atomic
    HAS_FILE_UTILS = True
    print("[OK] Loaded robust file utilities for atomic JSON writes")
except ImportError as e:
    HAS_FILE_UTILS = False
    print(f"[WARNING] Could not import file_utils: {e}")
    print("[WARNING] Will use standard JSON write (not atomic)")

# Try to import FreeCAD
import traceback
try:
    import FreeCAD
    import Part
    import Mesh
    import MeshPart
    import Import
except ImportError:
    print("[ERROR] FreeCAD modules not found. This script must be run via freecadcmd.exe.")
    sys.exit(1)

# Optional fast-path deps for the earcut-based perforated-plate triangulation
# (see FreeCADUtils.triangulate_planar_face_with_holes). Both are pure/compiled
# libraries with no FreeCAD dependency; if either is missing we transparently
# fall back to the OCC MeshPart path everywhere this is used.
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import mapbox_earcut as _earcut
    HAS_EARCUT = True
except ImportError:
    HAS_EARCUT = False
    print("[WARNING] mapbox_earcut not found. Perforated-plate fast triangulation disabled (falls back to OCC).")

try:
    import triangle as _triangle_lib
    HAS_TRIANGLE = True
except ImportError:
    HAS_TRIANGLE = False
    print("[WARNING] triangle (Shewchuk CDT) not found. Perforated-plate fast triangulation disabled (falls back to OCC).")

# --- Configuration ---
FREECAD_MESH_CONFIG = {
    "LINEAR_DEFLECTION": 0.1,
    "ANGULAR_DEFLECTION": 0.8,
    "RELATIVE": False
}
# Coarser mesh used only for shapes with many small holes (e.g. perforated sheets),
# where the default LINEAR_DEFLECTION (absolute, not size-relative) massively
# over-tessellates every hole. See docs/superpowers/specs perf discussion.
PERFORATED_MESH_CONFIG = {
    "LINEAR_DEFLECTION": 0.5,
    "ANGULAR_DEFLECTION": 0.8,
}
PERFORATED_SMALL_HOLE_RADIUS_MM = 25.0
PERFORATED_HOLE_COUNT_THRESHOLD = 20
# Circle discretization used by the earcut fast path for perforated-plate faces.
# Matches the facet count OCC produced at the deflection settings above, so hole
# roundness is unchanged -- earcut only replaces HOW the triangulation is computed,
# not the visual resolution.
PERFORATED_HOLE_POLYGON_SEGMENTS = 32
# Safe upper bound for the whole-shape Shape.tessellate() cache optimization.
# Verified fast (~0.7s) at 668 faces; verified to blow up (>1h, >8GB RAM) at 4434
# faces. 1200 is a conservative cutoff until the real breakpoint is profiled.
WHOLE_SHAPE_TESSELLATE_MAX_FACES = 1200
EDGE_PROCESSING_CONFIG = {
    "POINTS_PER_EDGE": 8
}
FEATURE_EXTRACTION_CONFIG = {
    "TOLERANCE": 1e-6,
    "PARALLEL_THRESHOLD": 0.99,
    "SLOT_DISTANCE_FACTOR": 1.5
}

# ── COMPACT ENCODING ─────────────────────────────────────────────────────────
# True  → output faces as Base64 binary (positions_b64) → ~93% smaller JSON
# False → output legacy verbose OnShape JSON (btType/vertices dicts)
COMPACT_FACES_ENCODING: bool = True


# --- Feature Extraction Class ---
class FeatureExtractor:
    """
    Extract semantic manufacturing features from FreeCAD shapes.
    
    Detects (Metadata-driven + Geometry-based):
    - Threaded Holes (metadata + cylindrical matching)
    - Countersinks (conical + cylindrical pairing)
    - Oblongs (metadata + bbox matching)
    - Square/Rectangular Holes (metadata + planar wall matching)
    - Bending (metadata + toroidal + cylindrical)
    
    NOT detected here (handled by enrich_features.py):
    - Regular Holes (tessellation-based)
    - Fillets (tessellation-based)
    - Rectangular/Square faces (tessellation-based)
    - Chamfers (disabled, will be implemented later)
    """
    
    TOLERANCE = FEATURE_EXTRACTION_CONFIG["TOLERANCE"]
    
    @staticmethod
    def extract_features(shape, face_id_map: Optional[Dict[int, str]] = None, metadata: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Extract manufacturing features from a FreeCAD shape.
        
        Args:
            shape: FreeCAD Shape object
            face_id_map: Optional mapping of face hash to face ID (for linking with mesh)
            metadata: Optional dictionary with 'threaded_holes', 'oblongs', and 'bending_features' metadata
        
        Returns:
            List of feature dictionaries
        """
        features = []
        face_id_map = face_id_map or {}
        metadata = metadata or {}
        
        # [OK] FIX: Read correct key 'threaded_holes' (not 'threaded')
        threaded_holes = metadata.get('threaded_holes', [])
        oblong_metadata = metadata.get('oblongs', [])
        bending_metadata = metadata.get('bending_features', [])
        box_hole_metadata = metadata.get('box_holes', [])
        
        # [OK] FIX: Convert threaded_holes array to lookup dictionary format
        threaded_info = FeatureExtractor._prepare_threaded_lookup(threaded_holes)
        
        # Group faces by surface type
        cylindrical_faces = []
        conical_faces = []
        toroidal_faces = []
        planar_faces = []
        
        for i, face in enumerate(shape.Faces):
            face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}")
            surface = face.Surface
            surface_type = surface.TypeId
            
            face_info = {
                "face": face,
                "face_id": face_id,
                "surface": surface,
                "surface_type": surface_type,
                "area": face.Area,
                "center": face.CenterOfMass
            }
            
            if "Cylinder" in surface_type:
                cylindrical_faces.append(face_info)
            elif "Cone" in surface_type:
                conical_faces.append(face_info)
            elif "Torus" in surface_type:
                toroidal_faces.append(face_info)
            elif "Plane" in surface_type:
                planar_faces.append(face_info)
        
        # Get shape bounding box for reference
        bbox = shape.BoundBox
        thickness = min(bbox.XLength, bbox.YLength, bbox.ZLength)
        
        # 🔧 PHASE 1: Estimate thickness (for bending detection)
        estimated_thickness = FeatureExtractor._estimate_thickness(planar_faces)
        
        # 🔧 PHASE 2: Detect BENDING (simplified - no fillet classification)
        bending_pairs = FeatureExtractor._pair_bending_faces(
            toroidal_faces, cylindrical_faces, estimated_thickness, bending_metadata
        )
        
        # Validate and create bending features
        bending_features = FeatureExtractor._validate_bending_pattern(
            bending_pairs, shape, planar_faces
        )
        features.extend(bending_features)
        
        # 🔧 PHASE 3: Extract bending face IDs for exclusion
        # ✅ FIX: Bending features have nested structure: inner: {face_ids: [...]}, outer: {face_ids: [...]}
        # Need to extract from both inner and outer, not from top-level face_ids (which is empty)
        bending_face_ids = set()
        for bend in bending_features:
            # Extract from inner
            inner_faces = bend.get('inner', {}).get('face_ids', [])
            if inner_faces:
                bending_face_ids.update(inner_faces)
            # Extract from outer
            outer_faces = bend.get('outer', {}).get('face_ids', [])
            if outer_faces:
                bending_face_ids.update(outer_faces)
            # Also check top-level face_ids (for backward compatibility)
            top_level_faces = bend.get('face_ids', [])
            if top_level_faces:
                bending_face_ids.update(top_level_faces)
        
        if bending_face_ids:
            print(f"[INFO] Excluding {len(bending_face_ids)} bending face(s) from hole detection: {sorted(bending_face_ids)}")
        
        # Detect features - IMPORTANT: Detect countersinks FIRST to claim cylinder faces
        # This prevents cylinder faces that are part of countersinks from being detected as regular holes
        countersinks, used_cylinder_ids = FeatureExtractor._detect_countersinks_with_cylinder(
            conical_faces, cylindrical_faces
        )
        features.extend(countersinks)
        
        # [OK] SIMPLIFIED: Only filter out cylinders used in countersinks and bending
        # No more fillet classification - let threaded holes be detected!
        available_cylindrical_faces = [
            f for f in cylindrical_faces 
            if id(f["face"]) not in used_cylinder_ids 
            and f["face_id"] not in bending_face_ids
        ]
        
        # Pass threaded_info to hole detection (now in correct lookup format)
        features.extend(FeatureExtractor._detect_holes(available_cylindrical_faces, thickness, threaded_info))
        
        features.extend(FeatureExtractor._detect_slots(cylindrical_faces))
        # [ERROR] CHAMFER DETECTION DISABLED - Will be implemented later
        # features.extend(FeatureExtractor._detect_chamfers(shape, planar_faces))
        
        # Add oblongs from metadata (metadata-driven, no geometric detection)
        if oblong_metadata:
            features.extend(FeatureExtractor._add_oblongs_from_metadata(shape, face_id_map, oblong_metadata))

        if box_hole_metadata:
            features.extend(FeatureExtractor._add_box_holes_from_metadata(shape, face_id_map, box_hole_metadata))
        
        print(f"[OK] Feature extraction: {len(features)} features detected")
        print(f"     - Holes: {len([f for f in features if f['type'] == 'hole'])}")
        
        # Count threaded holes
        threaded_holes = [f for f in features if f['type'] == 'hole' and f.get('is_threaded', False)]
        if threaded_holes:
            print(f"       * Threaded: {len(threaded_holes)}")
            for th in threaded_holes:
                print(f"         - {th.get('thread', 'Unknown')} at ({th['position']['x']}, {th['position']['y']}, {th['position']['z']})")
        
        countersink_list = [f for f in features if f['type'] == 'countersink']
        print(f"     - Countersinks: {len(countersink_list)}")
        if countersink_list:
            with_cylinder = len([c for c in countersink_list if len(c['face_ids']) == 2])
            cone_only = len([c for c in countersink_list if len(c['face_ids']) == 1])
            print(f"       * With cylinder: {with_cylinder}")
            print(f"       * Cone only: {cone_only}")
        
        # 🔧 UPDATED: Print bending features (nested inner/outer structure)
        bending_list = [f for f in features if f['type'] == 'bending']
        print(f"     - Bending: {len(bending_list)} bend(s)")
        if bending_list:
            for bend in bending_list:
                inner_radius = bend.get('inner', {}).get('radius', 0)
                outer_radius = bend.get('outer', {}).get('radius', 0)
                inner_faces = bend.get('inner', {}).get('face_ids', [])
                outer_faces = bend.get('outer', {}).get('face_ids', [])
                print(f"       * {bend['subtype']}: Inner R={inner_radius}mm (faces={inner_faces}), Outer R={outer_radius}mm (faces={outer_faces})")
        
        print(f"     - Slots: {len([f for f in features if f['type'] == 'slot'])}")
        print(f"     - Oblongs: {len([f for f in features if f['type'] == 'oblong'])}")
        print(f"     - Square holes: {len([f for f in features if f['type'] == 'square_hole'])}")
        print(f"     - Rectangular holes: {len([f for f in features if f['type'] == 'rectangular_hole'])}")
        # Chamfers: Disabled (will be implemented later)
        
        return features
    
    @staticmethod
    def _prepare_threaded_lookup(threaded_holes: List[Dict]) -> Dict[str, Dict]:
        """
        Convert threaded_holes array to lookup dictionary format.
        
        Converts from:
            [{"position": {"x": 50, "y": 50, "z": 0}, "thread_type": "M4", ...}]
        
        To:
            {
                '3d': {(50.0, 50.0, 0.0): {"thread_type": "M4", ...}},
                'xy': {(50.0, 50.0): {"thread_type": "M4", ...}}
            }
        
        Args:
            threaded_holes: List of threaded hole dictionaries from metadata
        
        Returns:
            Dictionary with '3d' and 'xy' lookup tables
        """
        lookup = {'3d': {}, 'xy': {}}
        
        if not threaded_holes:
            return lookup
        
        for hole in threaded_holes:
            pos = hole.get('position', {})
            x = round(pos.get('x', 0), 1)
            y = round(pos.get('y', 0), 1)
            z = round(pos.get('z', 0), 1)
            
            
            # Create lookup data - support both M-size and direct radius
            # Priority: thread_type (M-size) > radius from metadata > None
            thread_type = hole.get('thread_type')  # M4, M6, etc. (can be None)
            metadata_radius = hole.get('radius')
            metadata_diameter = hole.get('diameter')
            
            # [FIX] If diameter/radius = 0 but thread_type exists, calculate from thread_type
            if (metadata_diameter == 0 or metadata_radius == 0) and thread_type:
                import re
                # Extract number from thread_type (e.g., "M5" -> 5.0)
                match = re.match(r'M(\d+(?:\.\d+)?)', str(thread_type), re.IGNORECASE)
                if match:
                    thread_diameter = float(match.group(1))
                    # For tapped holes, use thread diameter as approximate hole diameter
                    # (actual tapped hole is slightly smaller, but this is close enough for matching)
                    if metadata_diameter == 0:
                        metadata_diameter = thread_diameter
                    if metadata_radius == 0:
                        metadata_radius = thread_diameter / 2.0
            
            data = {
                'thread_type': thread_type,
                'radius': metadata_radius,  # Can be None or calculated from thread_type
                'confidence': hole.get('confidence', 0.0),
                'diameter': metadata_diameter,  # Can be None or calculated from thread_type
                'depth': hole.get('depth'),
                'source': hole.get('source', 'metadata')
            }
            
            # Add to 3D lookup (exact position)
            key_3d = (x, y, z)
            lookup['3d'][key_3d] = data
            
            # Add to XY lookup (for through holes)
            key_xy = (x, y)
            # Only add if not already present (avoid overwriting with different Z)
            if key_xy not in lookup['xy']:
                lookup['xy'][key_xy] = data
        
        print(f"[INFO] Converted {len(threaded_holes)} threaded hole(s) to lookup format")
        print(f"       - 3D keys: {len(lookup['3d'])}")
        print(f"       - XY keys: {len(lookup['xy'])}")
        
        return lookup
    
    @staticmethod
    def _detect_holes(cylindrical_faces: List[Dict], thickness: float, threaded_info: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Detect holes from cylindrical surfaces and match with threaded metadata.
        
        Threaded holes are often modeled with multiple cylindrical faces (for thread geometry).
        This function groups faces at the same position into a single threaded hole feature.
        
        Uses multi-strategy matching:
        1. Exact 3D position match (highest priority)
        2. Z-tolerance match (check z=0 and z=thickness)
        3. XY-only match for through holes (lowest priority)
        
        Args:
            cylindrical_faces: List of cylindrical face dictionaries
            thickness: Part thickness for through-hole detection
            threaded_info: Optional dictionary with '3d' and 'xy' lookup tables
        
        Returns:
            List of hole feature dictionaries with threaded information
        """
        # [OK] RE-ENABLED: Process threaded holes from metadata
        # Regular holes are still handled by enrich_features.py to avoid conflicts
        # This function ONLY processes threaded holes that have metadata
        holes = []
        threaded_info = threaded_info or {'3d': {}, 'xy': {}}
        
        # Log metadata availability
        threaded_count = len(threaded_info.get('3d', {}))
        if threaded_count > 0:
            print(f"[INFO] Processing {threaded_count} threaded hole(s) from metadata")
        else:
            print("[INFO] No threaded holes metadata provided")
        
        # Helper function: Match face with metadata using multi-strategy
        def match_face_with_metadata(center, radius, threaded_info, thickness):
            """Try multiple strategies to match face with metadata"""
            x = round(center.x, 1)
            y = round(center.y, 1)
            z = round(center.z, 1)
            
            # Strategy 1: Exact 3D match (highest priority)
            key_3d = (x, y, z)
            if key_3d in threaded_info.get('3d', {}):
                return threaded_info['3d'][key_3d], key_3d, '3d_exact'
            
            # Strategy 2: Z-tolerance match (check common Z values)
            # Check z=0 (base plane) and z=thickness (top plane)
            for z_candidate in [0.0, round(thickness, 1)]:
                key_z_tol = (x, y, z_candidate)
                if key_z_tol in threaded_info.get('3d', {}):
                    return threaded_info['3d'][key_z_tol], key_z_tol, 'z_tolerance'
            
            # Strategy 3: XY-only match (for through holes)
            key_xy = (x, y)
            if key_xy in threaded_info.get('xy', {}):
                return threaded_info['xy'][key_xy], key_xy, 'xy_only'
            
            # Strategy 4: Diameter/Radius match (fallback when position doesn't match)
            # Only match if diameter matches and there's exactly one unmatched metadata entry
            if radius is not None:
                diameter = radius * 2
                unmatched_entries = []
                for key, data in threaded_info.get('3d', {}).items():
                    meta_diameter = data.get('diameter')
                    meta_radius = data.get('radius')
                    if meta_diameter and abs(meta_diameter - diameter) < 0.1:
                        unmatched_entries.append((key, data))
                    elif meta_radius and abs(meta_radius - radius) < 0.05:
                        unmatched_entries.append((key, data))
                
                # Only match if exactly one candidate (avoid ambiguity)
                if len(unmatched_entries) == 1:
                    key, data = unmatched_entries[0]
                    return data, key, 'diameter_match'
            
            return None, None, 'no_match'
        
        # PHASE 1: Match each internal cylindrical face with metadata
        face_to_thread_data = {}  # face_id -> (thread_data, metadata_key, strategy)
        
        for face_info in cylindrical_faces:
            face = face_info["face"]
            
            # Check if internal (hole)
            if not FeatureExtractor._is_internal_cylinder(face):
                continue
            
            try:
                surface = face_info["surface"]
                center = surface.Center
                radius = surface.Radius
                
                # Try to match with metadata
                thread_data, meta_key, strategy = match_face_with_metadata(center, radius, threaded_info, thickness)
                
                if thread_data:
                    face_to_thread_data[id(face)] = (thread_data, meta_key, strategy)
                    
            except AttributeError:
                continue
        
        # PHASE 2: Group faces by metadata_key (faces with same meta_key = same threaded hole)
        grouped_by_metadata = {}  # metadata_key -> [face_info, ...]
        
        for face_info in cylindrical_faces:
            face_id = id(face_info["face"])
            if face_id in face_to_thread_data:
                thread_data, meta_key, strategy = face_to_thread_data[face_id]
                
                if meta_key not in grouped_by_metadata:
                    grouped_by_metadata[meta_key] = {
                        'faces': [],
                        'thread_data': thread_data,
                        'strategy': strategy
                    }
                
                grouped_by_metadata[meta_key]['faces'].append(face_info)
        
        # PHASE 3: Create ONE threaded hole per metadata entry (grouped faces)
        matched_metadata_keys = set()
        
        for meta_key, group_data in grouped_by_metadata.items():
            matching_faces = group_data['faces']
            thread_data = group_data['thread_data']
            strategy = group_data['strategy']
            
            matched_metadata_keys.add(meta_key)
            
            # Use first face for primary geometry info
            first_face = matching_faces[0]
            surface = first_face["surface"]
            radius = surface.Radius
            axis = surface.Axis
            center = surface.Center
            
            # Calculate combined depth (max depth from all faces)
            max_depth = 0
            for face_info in matching_faces:
                face = face_info["face"]
                bounds = face.BoundBox
                
                if abs(axis.x) > 0.9:
                    depth = bounds.XLength
                elif abs(axis.y) > 0.9:
                    depth = bounds.YLength
                else:
                    depth = bounds.ZLength
                
                max_depth = max(max_depth, depth)
            
            # Determine hole type
            is_through = abs(max_depth - thickness) < FeatureExtractor.TOLERANCE * 100
            
            
            # Collect all face IDs
            face_ids = [f["face_id"] for f in matching_faces]
            
            # Determine thread info - support both M-size and direct radius
            # Priority: thread_type (M-size) > radius from metadata > geometry radius
            thread_type = thread_data.get('thread_type')  # Can be None
            metadata_radius = thread_data.get('radius')  # Can be None
            
            # Use metadata radius if provided, otherwise use geometry radius
            if metadata_radius is not None:
                final_radius = metadata_radius
                radius_source = "metadata_radius"
            else:
                final_radius = radius  # From geometry
                radius_source = "geometry"
            
            holes.append({
                "type": "hole",
                "subtype": "through" if is_through else "blind",
                "diameter": round(final_radius * 2, 3),
                "radius": round(final_radius, 3),
                "depth": round(max_depth, 3),
                "position": {
                    "x": round(center.x, 3),
                    "y": round(center.y, 3),
                    "z": round(center.z, 3)
                },
                "axis": {
                    "x": round(axis.x, 6),
                    "y": round(axis.y, 6),
                    "z": round(axis.z, 6)
                },
                "face_ids": face_ids,  # Multiple faces for threaded hole
                "thread": thread_type,  # M4, M6, etc. (can be None if only radius specified)
                "is_threaded": True,
                "thread_confidence": thread_data.get('confidence', 0.0),
                "face_count": len(matching_faces),  # Debug info
                "match_strategy": strategy,  # Debug: how it was matched
                "radius_source": radius_source  # Debug: where radius came from
            })
            
            # Display thread info (M-size or radius)
            thread_display = thread_type if thread_type else f"radius={final_radius}mm"
            print(f"[OK] Grouped {len(matching_faces)} faces into threaded hole {thread_display} at {meta_key} (strategy: {strategy}, radius_source: {radius_source})")
        
        # PHASE 4: Metadata fallback for threaded holes not matched with geometry
        # If threaded holes are tessellated as B-spline surfaces, create features from metadata
        if threaded_info and threaded_info.get('3d'):
            unmatched_metadata_keys = set(threaded_info.get('3d', {}).keys()) - matched_metadata_keys
            
            if unmatched_metadata_keys:
                print(f"[INFO] {len(unmatched_metadata_keys)} threaded hole(s) not matched with geometry (likely tessellated)")
                print(f"[INFO] Creating features from metadata as fallback...")
                
                for meta_key in unmatched_metadata_keys:
                    thread_data = threaded_info['3d'][meta_key]
                    thread_type = thread_data.get('thread_type')
                    metadata_radius = thread_data.get('radius')
                    metadata_diameter = thread_data.get('diameter')
                    
                    # Extract position from metadata key
                    if isinstance(meta_key, tuple) and len(meta_key) == 3:
                        x, y, z = meta_key
                    else:
                        # Try to get from metadata if available
                        x = thread_data.get('position', {}).get('x', 0) if isinstance(thread_data.get('position'), dict) else 0
                        y = thread_data.get('position', {}).get('y', 0) if isinstance(thread_data.get('position'), dict) else 0
                        z = thread_data.get('position', {}).get('z', 0) if isinstance(thread_data.get('position'), dict) else 0
                    
                    # Use metadata radius if available
                    if metadata_radius is not None:
                        final_radius = metadata_radius
                    elif metadata_diameter is not None:
                        final_radius = metadata_diameter / 2.0
                    else:
                        final_radius = 2.5  # Default M6 radius
                    
                    # Estimate depth from metadata or use default
                    depth = thread_data.get('depth', thickness if thickness else 5.0)
                    
                    holes.append({
                        "type": "hole",
                        "subtype": "through" if abs(depth - thickness) < 0.1 else "blind",
                        "diameter": round(final_radius * 2, 3),
                        "radius": round(final_radius, 3),
                        "depth": round(depth, 3),
                        "position": {
                            "x": round(x, 3),
                            "y": round(y, 3),
                            "z": round(z, 3)
                        },
                        "axis": {
                            "x": 0.0,
                            "y": 0.0,
                            "z": 1.0
                        },
                        "face_ids": [],  # No geometry match
                        "thread": thread_type,
                        "is_threaded": True,
                        "thread_confidence": thread_data.get('confidence', 0.85),
                        "face_count": 0,
                        "match_strategy": "metadata_fallback",
                        "radius_source": "metadata_radius",
                        "from_metadata_only": True  # Flag to indicate no geometry match
                    })
                    
                    thread_display = thread_type if thread_type else f"radius={final_radius}mm"
                    print(f"[OK] Created threaded hole {thread_display} from metadata at ({x:.1f}, {y:.1f}, {z:.1f})")
        
        # PHASE 5: Process remaining faces as regular holes (not matched with metadata)
        # 🚫 DISABLED: Regular holes are now handled by enrich_features.py to avoid conflicts
        # Only threaded holes from metadata are processed above (Phase 1-4)
        print("[INFO] Skipping regular hole detection - handled by enrich_features.py")
        
        # Original Phase 4 code (disabled to avoid conflicts):
        # for face_info in cylindrical_faces:
        #     face = face_info["face"]
        #     face_id = id(face)
        #     
        #     # Skip if already processed as threaded
        #     if face_id in face_to_thread_data:
        #         continue
        #     
        #     # Check if internal (hole)
        #     if not FeatureExtractor._is_internal_cylinder(face):
        #         continue
        #     
        #     try:
        #         surface = face_info["surface"]
        #         radius = surface.Radius
        #         axis = surface.Axis
        #         center = surface.Center
        #     except AttributeError:
        #         continue
        #     
        #     # Calculate depth from face bounds
        #     bounds = face.BoundBox
        #     if abs(axis.x) > 0.9:
        #         depth = bounds.XLength
        #     elif abs(axis.y) > 0.9:
        #         depth = bounds.YLength
        #     else:
        #         depth = bounds.ZLength
        #     
        #     # Determine hole type
        #     is_through = abs(depth - thickness) < FeatureExtractor.TOLERANCE * 100
        #     
        #     holes.append({
        #         "type": "hole",
        #         "subtype": "through" if is_through else "blind",
        #         "diameter": round(radius * 2, 3),
        #         "radius": round(radius, 3),
        #         "depth": round(depth, 3),
        #         "position": {
        #             "x": round(center.x, 3),
        #             "y": round(center.y, 3),
        #             "z": round(center.z, 3)
        #         },
        #         "axis": {
        #             "x": round(axis.x, 6),
        #             "y": round(axis.y, 6),
        #             "z": round(axis.z, 6)
        #         },
        #         "face_ids": [face_info["face_id"]],
        #         "thread": None,
        #         "is_threaded": False,
        #         "thread_confidence": 0.0
        #     })
        
        # Log metadata matching results
        if threaded_info and threaded_info.get('3d'):
            total_metadata = len(threaded_info.get('3d', {}))
            matched_count = len(matched_metadata_keys)
            unmatched_count = total_metadata - matched_count
            
            if matched_count > 0:
                print(f"[OK] Matched {matched_count}/{total_metadata} threaded holes from metadata (geometry match)")
            
            # Note: Unmatched holes are handled by metadata fallback in Phase 4 above
            # Only show warning if fallback didn't create features (shouldn't happen, but just in case)
            if unmatched_count > 0:
                # Check if fallback was used (holes with from_metadata_only flag)
                fallback_count = len([h for h in holes if h.get('from_metadata_only', False)])
                if fallback_count < unmatched_count:
                    # Some unmatched holes weren't handled by fallback
                    unmatched_keys = set(threaded_info.get('3d', {}).keys()) - matched_metadata_keys
                    print(f"[WARNING] {unmatched_count - fallback_count} threaded hole(s) from metadata not matched and not handled by fallback:")
                    for key in unmatched_keys:
                        thread_type = threaded_info['3d'][key].get('thread_type', 'Unknown')
                        print(f"         - {thread_type} at {key}")
                else:
                    # All unmatched holes handled by fallback
                    print(f"[INFO] {unmatched_count} threaded hole(s) handled by metadata fallback (tessellated geometry)")
        
        return holes
    
    @staticmethod
    def _find_matching_cylinder_for_cone(cone_info: Dict, cylindrical_faces: List[Dict], 
                                         cone_min_radius: float, cone_axis, cone_center) -> Optional[Dict]:
        """
        Find a matching cylindrical face for a conical face to form a complete countersink.
        
        Args:
            cone_info: Dictionary containing cone face information
            cylindrical_faces: List of cylindrical face dictionaries
            cone_min_radius: Minimum radius of the cone (bottom radius)
            cone_axis: Axis vector of the cone
            cone_center: Center point of the cone
        
        Returns:
            Matching cylinder face info dict, or None if no match found
        """
        AXIS_SIMILARITY_THRESHOLD = 0.99  # Axes must be nearly parallel
        RADIUS_TOLERANCE = 0.1  # mm - tolerance for radius matching
        
        best_match = None
        best_distance = float('inf')
        
        for cyl_info in cylindrical_faces:
            cyl_face = cyl_info["face"]
            cyl_surface = cyl_info["surface"]
            
            try:
                cyl_radius = cyl_surface.Radius
                cyl_axis = cyl_surface.Axis
                cyl_center = cyl_surface.Center
                
                # Check 1: Axis alignment (dot product close to 1 or -1)
                axis_dot = abs(cone_axis.dot(cyl_axis))
                if axis_dot < AXIS_SIMILARITY_THRESHOLD:
                    continue
                
                # Check 2: Radius match (cylinder ≈ cone's bottom radius)
                if abs(cyl_radius - cone_min_radius) > RADIUS_TOLERANCE:
                    continue
                
                # Check 3: Cylinder must be internal (hole)
                if not FeatureExtractor._is_internal_cylinder(cyl_face):
                    continue
                
                # Check 4: Spatial proximity (centers should be reasonably close)
                distance = cone_center.distanceToPoint(cyl_center)
                # Use a heuristic: distance should be less than 3x the radius
                if distance > cone_min_radius * 3:
                    continue
                
                # Found a valid match - keep the closest one
                if distance < best_distance:
                    best_distance = distance
                    best_match = cyl_info
                    
            except AttributeError:
                continue
        
        return best_match
    
    @staticmethod
    def _detect_countersinks_with_cylinder(conical_faces: List[Dict], 
                                           cylindrical_faces: List[Dict]) -> tuple[List[Dict[str, Any]], Set[int]]:
        """
        Detect countersinks from conical surfaces and their associated cylindrical holes.
        
        A complete countersink consists of:
        1. A conical face (the countersink chamfer)
        2. A cylindrical face (the through-hole for the screw shaft)
        
        Args:
            conical_faces: List of conical face dictionaries
            cylindrical_faces: List of cylindrical face dictionaries
        
        Returns:
            Tuple of (countersinks list, set of used cylinder face IDs)
        """
        countersinks = []
        used_cylinder_ids: Set[int] = set()
        
        for face_info in conical_faces:
            face = face_info["face"]
            surface = face_info["surface"]
            
            try:
                apex = surface.Apex
                axis = surface.Axis
                semi_angle = surface.SemiAngle
                
                # Get radii at face boundaries from circular edges
                edges = face.Edges
                radii = []
                for edge in edges:
                    try:
                        if hasattr(edge.Curve, 'Radius'):
                            radii.append(edge.Curve.Radius)
                    except Exception:
                        continue
                
                if len(radii) < 2:
                    continue
                
                max_radius = max(radii)
                min_radius = min(radii)
                diameter_top = round(max_radius * 2, 3)
                diameter_bottom = round(min_radius * 2, 3)
                
                # Calculate cone depth from geometry
                try:
                    cone_depth = abs(max_radius - min_radius) / math.tan(abs(semi_angle))
                except ZeroDivisionError:
                    cone_depth = 0
                
                # Get center position from face
                center = face.CenterOfMass
                
                # Try to find matching cylinder for this cone
                matching_cylinder = FeatureExtractor._find_matching_cylinder_for_cone(
                    face_info, cylindrical_faces, min_radius, axis, center
                )
                
                # Build countersink feature
                face_ids = [face_info["face_id"]]
                cylinder_depth = None
                total_depth = cone_depth
                
                if matching_cylinder:
                    # Found matching cylinder - this is a complete countersink
                    face_ids.append(matching_cylinder["face_id"])
                    used_cylinder_ids.add(id(matching_cylinder["face"]))
                    
                    # Calculate cylinder depth
                    cyl_bounds = matching_cylinder["face"].BoundBox
                    if abs(axis.x) > 0.9:
                        cylinder_depth = cyl_bounds.XLength
                    elif abs(axis.y) > 0.9:
                        cylinder_depth = cyl_bounds.YLength
                    else:
                        cylinder_depth = cyl_bounds.ZLength
                    
                    total_depth = cone_depth + cylinder_depth
                
                countersinks.append({
                    "type": "countersink",
                    "diameter_top": diameter_top,
                    "diameter_bottom": diameter_bottom,
                    "angle": round(math.degrees(abs(semi_angle)) * 2, 1),
                    "depth": round(cone_depth, 3),  # Cone depth only
                    "cylinder_depth": round(cylinder_depth, 3) if cylinder_depth else None,
                    "total_depth": round(total_depth, 3),
                    "position": {
                        "x": round(center.x, 3),
                        "y": round(center.y, 3),
                        "z": round(center.z, 3)
                    },
                    "axis": {
                        "x": round(axis.x, 6),
                        "y": round(axis.y, 6),
                        "z": round(axis.z, 6)
                    },
                    "face_ids": face_ids  # [cone_id] or [cone_id, cylinder_id]
                })
            except (AttributeError, ZeroDivisionError, Exception) as e:
                print(f"[WARNING] Could not process conical face: {e}")
                continue
        
        return countersinks, used_cylinder_ids
    
    @staticmethod
    def _detect_fillets(toroidal_faces: List[Dict]) -> List[Dict[str, Any]]:
        """Detect fillets from toroidal surfaces"""
        fillets = []
        
        for face_info in toroidal_faces:
            surface = face_info["surface"]
            
            try:
                minor_radius = surface.MinorRadius
                major_radius = surface.MajorRadius
                center = surface.Center
                axis = surface.Axis
                
                # Fillets typically have minor radius << major radius
                # and are partial torus sections
                fillets.append({
                    "type": "fillet",
                    "radius": round(minor_radius, 3),
                    "major_radius": round(major_radius, 3),
                    "position": {
                        "x": round(center.x, 3),
                        "y": round(center.y, 3),
                        "z": round(center.z, 3)
                    },
                    "axis": {
                        "x": round(axis.x, 6),
                        "y": round(axis.y, 6),
                        "z": round(axis.z, 6)
                    },
                    "face_ids": [face_info["face_id"]]
                })
            except AttributeError as e:
                print(f"[WARNING] Could not process toroidal face: {e}")
                continue
        
        return fillets
    
    @staticmethod
    def _detect_slots(cylindrical_faces: List[Dict]) -> List[Dict[str, Any]]:
        """
        Detect slots (oblong holes) from paired cylindrical surfaces.
        
        A slot consists of two semi-cylindrical faces at the ends with the same radius
        and parallel axes, connected by planar faces.
        """
        # 🚫 DISABLED: Slot detection conflicts with oblong metadata
        # Slots/oblongs are now detected as:
        # - Oblongs from metadata (Part.makeOblong in FreeCAD code)
        # - Or handled by enrich_features.py/face_analyzer.js for non-metadata cases
        print("[INFO] Slot detection disabled - use oblongs from metadata instead")
        return []
        
        # Original code below (kept for reference but unreachable)
        slots = []
        processed_pairs: Set[tuple] = set()
        
        # Group internal cylindrical faces by radius
        internal_cylinders = [f for f in cylindrical_faces if FeatureExtractor._is_internal_cylinder(f["face"])]
        
        by_radius: Dict[float, List[Dict]] = {}
        for face_info in internal_cylinders:
            try:
                radius = round(face_info["surface"].Radius, 2)
                if radius not in by_radius:
                    by_radius[radius] = []
                by_radius[radius].append(face_info)
            except AttributeError:
                continue
        
        # Look for pairs of semi-cylinders that could form a slot
        for radius, faces in by_radius.items():
            if len(faces) < 2:
                continue
            
            for i, face1 in enumerate(faces):
                for face2 in faces[i+1:]:
                    pair_key = tuple(sorted([id(face1["face"]), id(face2["face"])]))
                    if pair_key in processed_pairs:
                        continue
                    
                    try:
                        # Check if axes are parallel
                        axis1 = face1["surface"].Axis
                        axis2 = face2["surface"].Axis
                        
                        dot = abs(axis1.dot(axis2))
                        if dot < FEATURE_EXTRACTION_CONFIG["PARALLEL_THRESHOLD"]:
                            continue
                        
                        # Calculate distance between centers
                        c1 = face1["surface"].Center
                        c2 = face2["surface"].Center
                        
                        distance = math.sqrt(
                            (c2.x - c1.x)**2 + 
                            (c2.y - c1.y)**2 + 
                            (c2.z - c1.z)**2
                        )
                        
                        # If distance > diameter * factor, it's likely a slot
                        if distance > radius * 2 * FEATURE_EXTRACTION_CONFIG["SLOT_DISTANCE_FACTOR"]:
                            bounds1 = face1["face"].BoundBox
                            
                            # Calculate depth (smallest dimension along the axis)
                            if abs(axis1.z) > 0.9:
                                depth = bounds1.ZLength
                            elif abs(axis1.y) > 0.9:
                                depth = bounds1.YLength
                            else:
                                depth = bounds1.XLength
                            
                            slots.append({
                                "type": "slot",
                                "width": round(radius * 2, 3),
                                "length": round(distance + radius * 2, 3),
                                "depth": round(depth, 3),
                                "position": {
                                    "x": round((c1.x + c2.x) / 2, 3),
                                    "y": round((c1.y + c2.y) / 2, 3),
                                    "z": round((c1.z + c2.z) / 2, 3)
                                },
                                "orientation": round(math.degrees(math.atan2(c2.y - c1.y, c2.x - c1.x)), 1),
                                "axis": {
                                    "x": round(axis1.x, 6),
                                    "y": round(axis1.y, 6),
                                    "z": round(axis1.z, 6)
                                },
                                "face_ids": [face1["face_id"], face2["face_id"]]
                            })
                            
                            processed_pairs.add(pair_key)
                    except Exception as e:
                        pass
                        print(f"[WARNING] Could not process cylinder pair for slot detection: {e}")
                        continue
        
        return slots
    
    @staticmethod
    def _detect_chamfers(shape, planar_faces: List[Dict]) -> List[Dict[str, Any]]:
        """
        Detect chamfers from planar faces at angles.
        
        Chamfers are typically narrow planar faces at 45° (or other angles) 
        between two other faces.
        """
        chamfers = []
        
        # Get all edges to find chamfer candidates
        for face_info in planar_faces:
            face = face_info["face"]
            
            try:
                # Check if this is a narrow face (potential chamfer)
                bounds = face.BoundBox
                dimensions = sorted([bounds.XLength, bounds.YLength, bounds.ZLength])
                
                # Chamfer faces are typically narrow strips
                if dimensions[0] < FeatureExtractor.TOLERANCE:
                    # 2D face, check aspect ratio
                    if dimensions[1] > 0 and dimensions[2] / dimensions[1] > 5:
                        # Long narrow face - potential chamfer
                        normal = face.normalAt(0, 0)
                        
                        # Check if normal is at an angle (not aligned with principal axes)
                        is_angled = (
                            abs(abs(normal.x) - 1) > 0.1 and
                            abs(abs(normal.y) - 1) > 0.1 and
                            abs(abs(normal.z) - 1) > 0.1
                        )
                        
                        if is_angled:
                            # Calculate chamfer size from the narrow dimension
                            chamfer_size = dimensions[1]
                            
                            # Estimate angle from normal
                            angle = math.degrees(math.acos(abs(normal.z))) if abs(normal.z) < 1 else 45
                            
                            center = face.CenterOfMass
                            
                            chamfers.append({
                                "type": "chamfer",
                                "size": round(chamfer_size, 3),
                                "angle": round(angle, 1),
                                "length": round(dimensions[2], 3),
                                "position": {
                                    "x": round(center.x, 3),
                                    "y": round(center.y, 3),
                                    "z": round(center.z, 3)
                                },
                                "normal": {
                                    "x": round(normal.x, 6),
                                    "y": round(normal.y, 6),
                                    "z": round(normal.z, 6)
                                },
                                "face_ids": [face_info["face_id"]]
                            })
            except Exception as e:
                pass
                print(f"[WARNING] Could not process planar face for chamfer detection: {e}")
                continue
        
        return chamfers
    
    @staticmethod
    def _is_internal_cylinder(face) -> bool:
        """
        Determine if a cylindrical face is internal (hole) or external.
        
        An internal cylinder has its normal pointing inward (toward the axis),
        while an external cylinder has its normal pointing outward.
        """
        try:
            surface = face.Surface
            center = surface.Center
            axis = surface.Axis
            radius = surface.Radius
            
            # IMPORTANT: CenterOfMass of a cylindrical face often lies ON the axis,
            # which makes radial vector = 0. Instead, we need a point ON the surface.
            # Strategy: Get a point from one of the circular edges
            
            point_on_surface = None
            
            # Try to get a point from circular edges
            for edge in face.Edges:
                if hasattr(edge.Curve, 'Radius'):
                    # This is a circular edge - get a point from it
                    try:
                        point_on_surface = edge.valueAt(edge.FirstParameter)
                        break
                    except Exception:
                        continue
            
            # Fallback: if no circular edge found, use a parametric point on the surface
            if point_on_surface is None:
                # Use parametric evaluation: u=0, v=0 should give a point on the surface
                try:
                    point_on_surface = face.valueAt(0, 0)
                except Exception:
                    # Last resort: offset from center perpendicular to axis
                    # Find a vector perpendicular to axis
                    if abs(axis.x) < 0.9:
                        perp = FreeCAD.Vector(1, 0, 0).cross(axis)
                    else:
                        perp = FreeCAD.Vector(0, 1, 0).cross(axis)
                    perp.normalize()
                    point_on_surface = center + perp * radius
            
            # Get normal at this point
            uv = face.Surface.parameter(point_on_surface)
            normal = face.normalAt(uv[0], uv[1])
            
            # Calculate radial direction (from axis to point on surface)
            # Project point onto axis line
            t = (FreeCAD.Vector(point_on_surface.x, point_on_surface.y, point_on_surface.z) - center).dot(axis)
            axis_point = center + axis * t
            radial = FreeCAD.Vector(point_on_surface.x, point_on_surface.y, point_on_surface.z) - axis_point
            
            if radial.Length > FeatureExtractor.TOLERANCE:
                radial.normalize()
                # If normal points inward (opposite to radial direction), it's internal
                return normal.dot(radial) < 0
            
            return False
        except Exception:
            return False


    @staticmethod
    def _is_internal_cylinder(face) -> bool:
        """
        Determine if a cylindrical face is internal (hole) or external (bend/fillet).
        
        Method: Check if the normal vector points inward (toward axis) or outward.
        - Internal (hole): Normal points toward axis center
        - External (bend): Normal points away from axis center
        
        Args:
            face: FreeCAD face object with cylindrical surface
            
        Returns:
            True if internal (hole), False if external (bend/fillet)
        """
        import FreeCAD
        
        try:
            surface = face.Surface
            
            # Get a point on the surface
            if face.Vertexes:
                point_on_surface = face.Vertexes[0].Point
            else:
                try:
                    point_on_surface = face.valueAt(0, 0)
                except:
                    return False
            
            # Get normal at this point
            try:
                uv = surface.parameter(point_on_surface)
                normal = face.normalAt(uv[0], uv[1])
            except:
                return False
            
            # Get cylinder axis and center
            center = surface.Center
            axis = surface.Axis
            
            # Calculate radial direction (from axis to point on surface)
            point_vec = FreeCAD.Vector(point_on_surface.x, point_on_surface.y, point_on_surface.z)
            t = (point_vec - center).dot(axis)
            axis_point = center + axis * t
            radial_dir = point_vec - axis_point
            
            if radial_dir.Length < 1e-6:
                return False
            
            radial_dir.normalize()
            
            # Check if normal points inward or outward
            # Internal: normal points toward axis (dot product < 0)
            # External: normal points away from axis (dot product > 0)
            dot_product = normal.dot(radial_dir)
            
            is_internal = dot_product < 0
            
            return is_internal
            
        except Exception as e:
            return False

    @staticmethod
    def _estimate_thickness(planar_faces: List[Dict]) -> float:
        """
        Estimate sheet metal thickness from parallel planar faces.
        
        Finds pairs of large parallel planar faces and measures distance.
        The minimum distance is likely the thickness.
        
        Returns:
            Estimated thickness in mm (default 2.0 if not found)
        """
        import FreeCAD
        
        min_distance = float('inf')
        found_parallel_pair = False
        
        # Only consider large planar faces (likely plate sections)
        large_planar = [f for f in planar_faces if f['area'] > 100]  # mm²
        
        for i, face1 in enumerate(large_planar):
            for face2 in large_planar[i+1:]:
                try:
                    # Get normals
                    normal1 = face1['face'].normalAt(0, 0)
                    normal2 = face2['face'].normalAt(0, 0)
                    
                    # Check if parallel (dot product ≈ ±1)
                    dot = abs(normal1.dot(normal2))
                    if dot > 0.95:  # Parallel
                        # Measure distance between face centers
                        center1 = face1['center']
                        center2 = face2['center']
                        distance = center1.distanceToPoint(center2)
                        
                        if distance < min_distance and distance > 0.5:  # Ignore very small distances
                            min_distance = distance
                            found_parallel_pair = True
                except:
                    continue
        
        if found_parallel_pair:
            print(f"[THICKNESS] Estimated thickness: {min_distance:.2f}mm")
            return min_distance
        else:
            print(f"[THICKNESS] Could not estimate thickness, using default 2.0mm")
            return 2.0  # Default fallback
    
    @staticmethod
    def _pair_bending_faces(toroidal_faces: List[Dict], cylindrical_faces: List[Dict], 
                           thickness: float, bending_metadata: List[Dict]) -> List[Dict]:
        """
        Pair bending faces - SIMPLIFIED to only detect bending, no fillet classification.
        
        Real-world SheetMetal bends create:
        - Inner bend: Cylindrical surface (R = bend_radius)
        - Outer bend: Cylindrical surface (R = bend_radius + thickness)
        
        Algorithm:
        1. Separate long cylindrical faces (bends) from short ones by arc length
        2. For each potential inner bend cylinder:
           - Calculate expected outer radius = inner_radius + thickness
           - Find matching outer cylinder with parallel axis
        3. Return paired cylinders as bending features
        
        Returns:
            List of bending_pairs: [{inner_face_id, outer_face_id, bend_radius, confidence}]
        """
        import FreeCAD
        
        bending_pairs = []
        paired_cylindrical_ids = set()
        
        print(f"\n[BENDING] Starting pairing: {len(toroidal_faces)} toroidal, {len(cylindrical_faces)} cylindrical")
        
        # 🔥 STEP 0: Extract metadata rules and calculate adaptive threshold
        min_arc_length_threshold = 50.0  # Default for L/U/Z shapes
        is_tub_shape = False
        tub_metadata = None
        metadata_radii = {}  # Store expected radii from metadata
        
        for bend_meta in bending_metadata:
            bend_type = bend_meta.get('bend_type')
            
            if bend_type == 'tub':
                is_tub_shape = True
                tub_metadata = bend_meta
                # TUB has shorter arc lengths due to 4 corners
                min_arc_length_threshold = bend_meta.get('min_arc_length', 30.0)
                print(f"[BENDING] [INFO] TUB detected from metadata: min_arc_length={min_arc_length_threshold}mm")
                print(f"[BENDING]    Expected: {bend_meta.get('bend_count', 4)} bends, R_inner={bend_meta.get('bend_radius')}mm, R_outer={bend_meta.get('outer_radius')}mm")
                
            elif bend_type in ['L_shape', 'U_shape', 'Z_shape']:
                # ✅ FIX: Extract dim_y from metadata to calculate adaptive threshold
                dimensions = bend_meta.get('dimensions', {})
                dim_y = dimensions.get('dim_y', 50.0)
                
                # ✅ Dynamic threshold: 40% of dim_y, min 15mm, max 50mm
                # This allows small L-shapes (dim_y=30mm → threshold=15mm) to pass
                calculated_threshold = max(15.0, min(dim_y * 0.4, 50.0))
                min_arc_length_threshold = min(min_arc_length_threshold, calculated_threshold)
                
                print(f"[BENDING] [INFO] {bend_type} detected from metadata:")
                print(f"[BENDING]    dim_y={dim_y}mm -> adaptive threshold={calculated_threshold:.1f}mm (using min={min_arc_length_threshold:.1f}mm)")
                print(f"[BENDING]    Expected: R_inner={bend_meta.get('bend_radius')}mm, R_outer={bend_meta.get('outer_radius')}mm")
            
            # Store metadata radii for verification
            meta_inner_r = bend_meta.get('bend_radius')
            meta_outer_r = bend_meta.get('outer_radius')
            if meta_inner_r and meta_outer_r:
                metadata_radii[bend_type] = {
                    'inner': meta_inner_r,
                    'outer': meta_outer_r,
                    'bend_meta': bend_meta
                }
        
        print(f"[BENDING] Final arc_length threshold: {min_arc_length_threshold:.1f}mm")
        
        # STEP 1: Separate cylindrical faces by arc length
        # 🔥 UPDATED: Use dynamic threshold based on shape type
        # - TUB: min_arc_length >= 30mm (shorter due to 4 corners)
        # - L/U/Z: min_arc_length >= 50mm (longer single bends)
        long_cylinders = []
        
        for cyl_info in cylindrical_faces:
            cyl_face = cyl_info['face']
            radius = cyl_info['surface'].Radius
            
            # Skip very small radius cylinders (likely holes, not bends)
            # Bends typically have radius >= 1mm
            if radius < 0.8:  # mm
                print(f"[BENDING] Skipping {cyl_info['face_id']}: too small radius (R={radius:.2f}mm, likely hole)")
                continue
            
            # Measure arc length using max edge length (more accurate than BoundBox)
            max_edge_length = 0
            for edge in cyl_face.Edges:
                if edge.Length > max_edge_length:
                    max_edge_length = edge.Length
            
            arc_length = max_edge_length
            
            # 🔥 CRITICAL: Use dynamic threshold based on shape type
            if arc_length > min_arc_length_threshold:  # Bend (threshold varies: 30mm for tub, 50mm for L/U/Z)
                long_cylinders.append({**cyl_info, 'arc_length': arc_length})
                print(f"[BENDING] {cyl_info['face_id']}: LONG (R={radius:.2f}mm, L={arc_length:.2f}mm) -> POTENTIAL BEND")
        
        print(f"[BENDING] Found {len(long_cylinders)} potential bend cylinders")
        
        # STEP 2: Pair long cylinders as inner + outer bends
        for inner_cyl in long_cylinders:
            inner_face = inner_cyl['face']
            inner_id = id(inner_face)
            
            # Skip if already paired
            if inner_id in paired_cylindrical_ids:
                continue
            
            try:
                inner_surface = inner_cyl['surface']
                R_inner = inner_surface.Radius
                center_inner = inner_surface.Center
                axis_inner = inner_surface.Axis
                arc_length_inner = inner_cyl['arc_length']
                
                # Calculate expected outer radius
                R_outer_expected = R_inner + thickness
                
                print(f"[BENDING] Trying to pair {inner_cyl['face_id']} (R={R_inner:.2f}mm, expecting outer R={R_outer_expected:.2f}mm)")
                
                # Search for matching outer cylinder
                best_match = None
                best_score = 0
                
                for outer_cyl in long_cylinders:
                    outer_id = id(outer_cyl['face'])
                    
                    # Skip self and already paired
                    if outer_id == inner_id or outer_id in paired_cylindrical_ids:
                        continue
                    
                    try:
                        outer_surface = outer_cyl['surface']
                        R_outer = outer_surface.Radius
                        center_outer = outer_surface.Center
                        axis_outer = outer_surface.Axis
                        arc_length_outer = outer_cyl['arc_length']
                        
                        print(f"  [BENDING] Checking {outer_cyl['face_id']} (R={R_outer:.2f}mm)...")
                        
                        # Check 1: Radius match (outer ≈ inner + thickness)
                        radius_diff = abs(R_outer - R_outer_expected)
                        print(f"    CHECK 1 - Radius: diff={radius_diff:.3f}mm (expected={R_outer_expected:.2f}, actual={R_outer:.2f})")
                        if radius_diff > 0.5:  # mm tolerance
                            print(f"    FAILED: Radius diff too large")
                            continue
                        
                        # Check 2: Parallel axes
                        axis_dot = abs(axis_inner.dot(axis_outer))
                        print(f"    CHECK 2 - Axis parallel: dot={axis_dot:.3f}")
                        if axis_dot < 0.98:  # Nearly parallel
                            print(f"    FAILED: Axes not parallel enough")
                            continue
                        
                        # Check 3: Similar arc lengths
                        arc_diff = abs(arc_length_outer - arc_length_inner)
                        print(f"    CHECK 3 - Arc length: diff={arc_diff:.2f}mm (inner={arc_length_inner:.2f}, outer={arc_length_outer:.2f})")
                        if arc_diff > 5.0:  # mm tolerance
                            print(f"    FAILED: Arc length diff too large")
                            continue
                        
                        # Check 4: Concentric or nearby (for bends, cylinders should be concentric)
                        # Calculate radial distance (perpendicular to axis)
                        center_diff = center_outer - center_inner
                        axial_component = center_diff.dot(axis_inner) * axis_inner
                        radial_diff = center_diff - axial_component
                        radial_distance = radial_diff.Length
                        
                        print(f"    CHECK 4 - Radial distance: {radial_distance:.2f}mm")
                        # For bends, cylinders should be roughly concentric (radial distance ≈ 0)
                        # But allow some tolerance for imperfect geometry
                        if radial_distance > 5.0:  # mm - relaxed tolerance
                            print(f"    FAILED: Not concentric (radial offset too large)")
                            continue
                        
                        # Calculate match score (higher is better)
                        score = (
                            (1.0 - radius_diff / 0.5) * 0.4 +  # Radius match (40%)
                            axis_dot * 0.3 +                    # Axis alignment (30%)
                            (1.0 - arc_diff / 5.0) * 0.2 +     # Arc length match (20%)
                            (1.0 - min(radial_distance / 5.0, 1.0)) * 0.1  # Concentricity (10%)
                        )
                        
                        print(f"    PASSED ALL CHECKS! Score={score:.3f}")
                        
                        if score > best_score:
                            best_score = score
                            best_match = outer_cyl
                    
                    except Exception as e:
                        print(f"    ERROR: {e}")
                        continue
                
                # If found a good match, create bending pair
                if best_match and best_score > 0.7:  # Confidence threshold
                    confidence = round(best_score, 2)
                    bend_type = 'cylindrical_bend'
                    
                    # ✅ STEP 2.5: Verify pair with metadata (boost confidence if match)
                    R_outer_actual = best_match['surface'].Radius
                    matched_metadata = False
                    bend_angle = None  # Will be extracted from metadata if available
                    
                    for meta_type, meta_data in metadata_radii.items():
                        meta_inner_r = meta_data['inner']
                        meta_outer_r = meta_data['outer']
                        
                        # Check if this pair matches metadata radii
                        radius_inner_match = abs(R_inner - meta_inner_r) < 0.5
                        radius_outer_match = abs(R_outer_actual - meta_outer_r) < 0.5
                        
                        if radius_inner_match and radius_outer_match:
                            # FOUND METADATA MATCH!
                            confidence = min(1.0, confidence + 0.15)  # Boost confidence
                            bend_type = meta_type
                            matched_metadata = True
                            
                            # ✅ Extract bend_angle from metadata
                            bend_meta = meta_data.get('bend_meta', {})
                            bend_angle = bend_meta.get('bend_angle')
                            
                            # For tub, always 90° if not in metadata
                            if bend_type == 'tub' and bend_angle is None:
                                bend_angle = 90.0
                            
                            print(f"[BENDING] [OK] Matched with metadata: {meta_type} (R_inner={meta_inner_r}, R_outer={meta_outer_r}, angle={bend_angle}°)")
                            print(f"[BENDING]    Confidence boosted: {best_score:.2f} -> {confidence:.2f}")
                            break
                    
                    if not matched_metadata:
                        print(f"[BENDING] ⚠️  No metadata match found (using generic type)")
                        # ✅ FIX: Try to get bend_angle from metadata based on radius match (not just bend_type)
                        # This handles cases where bend_type is 'cylindrical_bend' but metadata has 'L_shape'
                        for bend_meta in bending_metadata:
                            meta_inner_r = bend_meta.get('bend_radius')
                            meta_outer_r = bend_meta.get('outer_radius')
                            
                            # Check if radii are close (within 1mm tolerance)
                            if meta_inner_r and meta_outer_r:
                                radius_inner_close = abs(R_inner - meta_inner_r) < 1.0
                                radius_outer_close = abs(R_outer_actual - meta_outer_r) < 1.0
                                
                                if radius_inner_close and radius_outer_close:
                                    # Found metadata with matching radii, extract bend_angle
                                    bend_angle = bend_meta.get('bend_angle')
                                    if bend_angle is not None:
                                        print(f"[BENDING]    Using bend_angle={bend_angle}° from metadata (radius-based fallback)")
                                        break
                        
                        # ✅ Additional fallback: Try to get bend_angle from any metadata if still not found
                        if bend_angle is None:
                            for bend_meta in bending_metadata:
                                # Check if this metadata has matching radii (looser tolerance)
                                meta_inner_r = bend_meta.get('bend_radius')
                                meta_outer_r = bend_meta.get('outer_radius')
                                if meta_inner_r and meta_outer_r:
                                    if abs(R_inner - meta_inner_r) < 2.0 and abs(R_outer_actual - meta_outer_r) < 2.0:
                                        bend_angle = bend_meta.get('bend_angle')
                                        if bend_angle is not None:
                                            print(f"[BENDING]    Using bend_angle={bend_angle}° from metadata (loose radius match)")
                                            break
                        
                        # ✅ Final fallback: If only one bending feature in metadata, use its bend_angle
                        if bend_angle is None and len(bending_metadata) == 1:
                            bend_angle = bending_metadata[0].get('bend_angle')
                            if bend_angle is not None:
                                print(f"[BENDING]    Using bend_angle={bend_angle}° from metadata (single feature fallback)")
                    
                    # Default to 90° for tub if still not found
                    if bend_angle is None and bend_type == 'tub':
                        bend_angle = 90.0
                    
                    bending_pairs.append({
                        'inner_face_id': inner_cyl['face_id'],
                        'outer_face_id': best_match['face_id'],
                        'bend_radius': round(R_inner, 3),
                        'outer_radius': round(R_outer_actual, 3),
                        'arc_length': round(arc_length_inner, 3),
                        'confidence': confidence,
                        'bend_type': bend_type,
                        'bend_angle': round(bend_angle, 1) if bend_angle is not None else None
                    })
                    
                    paired_cylindrical_ids.add(inner_id)
                    paired_cylindrical_ids.add(id(best_match['face']))
                    
                    print(f"[BENDING] [OK] Paired: {inner_cyl['face_id']} (R={R_inner:.2f}mm) + {best_match['face_id']} (R={R_outer_actual:.2f}mm) | Type={bend_type} | Confidence={confidence:.2f}")
            
            except Exception as e:
                print(f"[BENDING] Warning: Could not process cylindrical face: {e}")
                continue
        
        print(f"[BENDING] Pairing complete: {len(bending_pairs)} bend(s) detected")
        
        # ✅ STEP 3: If no geometry matches but metadata exists, create features from metadata
        # This handles cases where U-shape is tessellated as B-spline surfaces
        if len(bending_pairs) == 0 and len(bending_metadata) > 0:
            print(f"[BENDING] [INFO] No geometry matches found, creating features from metadata...")
            
            for bend_meta in bending_metadata:
                bend_type = bend_meta.get('bend_type')
                bend_radius = bend_meta.get('bend_radius', 0)
                outer_radius = bend_meta.get('outer_radius', 0)
                bend_angle = bend_meta.get('bend_angle', 90.0)
                bend_count = bend_meta.get('bend_count', 1)
                
                # For U-shape with 2 bends, create 2 pairs
                if bend_type == 'U_shape' and bend_count == 2:
                    # Create 2 bending pairs (left and right bends)
                    for i in range(2):
                        bending_pairs.append({
                            'inner_face_id': f"metadata_inner_{bend_type}_{i}",
                            'outer_face_id': f"metadata_outer_{bend_type}_{i}",
                            'bend_radius': round(bend_radius, 3),
                            'outer_radius': round(outer_radius, 3),
                            'arc_length': 0.0,  # Unknown from metadata
                            'confidence': 0.85,  # Lower confidence (metadata-only)
                            'bend_type': bend_type,
                            'bend_angle': round(bend_angle, 1) if bend_angle else None,
                            'from_metadata_only': True  # Flag to indicate no geometry match
                        })
                    print(f"[BENDING] [OK] Created {bend_count} bending pair(s) from metadata (U-shape)")
                else:
                    # Single bend (L-shape) or other types
                    bending_pairs.append({
                        'inner_face_id': f"metadata_inner_{bend_type}",
                        'outer_face_id': f"metadata_outer_{bend_type}",
                        'bend_radius': round(bend_radius, 3),
                        'outer_radius': round(outer_radius, 3),
                        'arc_length': 0.0,
                        'confidence': 0.85,
                        'bend_type': bend_type,
                        'bend_angle': round(bend_angle, 1) if bend_angle else None,
                        'from_metadata_only': True
                    })
                    print(f"[BENDING] [OK] Created 1 bending pair from metadata ({bend_type})")
        
        return bending_pairs
    
    
    @staticmethod
    def _validate_bending_pattern(bending_pairs: List[Dict], shape, planar_faces: List[Dict]) -> List[Dict]:
        """
        Validate bending pairs by checking structural pattern.
        
        Creates ONE feature per bend with nested inner/outer structure:
        - type: "bending"
        - inner: {face_ids: [...], radius: ...}
        - outer: {face_ids: [...], radius: ...}
        
        This allows displaying both inner and outer information when clicking on either face.
        
        Checks:
        1. Adjacent large planar faces (plate sections)
        2. Bend angle between plates
        3. Structural continuity
        
        Returns:
            List of validated bending features (1 feature per bend pair)
        """
        import FreeCAD
        import math
        
        bending_features = []
        
        for pair in bending_pairs:
            # Extract properties
            bend_type = pair.get('bend_type', 'detected')
            bend_radius = pair['bend_radius']
            outer_radius = pair['outer_radius']
            arc_length = pair['arc_length']
            confidence = pair['confidence']
            inner_face_id = pair['inner_face_id']
            outer_face_id = pair['outer_face_id']
            bend_angle = pair.get('bend_angle')  # ✅ Extract bend_angle from pair
            
            # Create single bending feature with nested inner/outer
            feature = {
                'type': 'bending',
                'subtype': bend_type,
                'inner': {
                    'face_ids': [inner_face_id],
                    'radius': round(bend_radius, 3)
                },
                'outer': {
                    'face_ids': [outer_face_id],
                    'radius': round(outer_radius, 3)
                },
                'arc_length': round(arc_length, 3),
                'confidence': confidence,
                'face_ids': [],  # Empty - frontend will set this based on clicked face
                'pair_id': f"{inner_face_id}_{outer_face_id}"
            }
            
            # ✅ Add bend_angle if available
            if bend_angle is not None:
                feature['bend_angle'] = round(bend_angle, 1)
            
            bending_features.append(feature)
        
        return bending_features

    @staticmethod
    def _calculate_adaptive_tolerance(oblong_metadata: Dict) -> float:
        """
        Calculate adaptive tolerance based on oblong size.
        Larger oblongs get larger tolerance (5% of max dimension).
        
        Returns:
            Tolerance in mm (min 0.5mm, max 3.0mm)
        """
        max_dimension = max(
            oblong_metadata.get('total_length', oblong_metadata.get('straight_length', 0) + oblong_metadata.get('width', 0)),
            oblong_metadata.get('width', 0),
            oblong_metadata.get('depth', 0)
        )
        
        # 5% of max dimension, clamped to [0.5, 3.0]
        tolerance = max(0.5, min(3.0, max_dimension * 0.05))
        return tolerance
    
    @staticmethod
    def _expand_bbox_for_through_cuts(bbox: Dict, oblong_metadata: Dict, expansion: float = 1.5) -> Dict:
        """
        Expand bounding box to handle through-cut oblongs with negative Z.
        
        Args:
            bbox: Original bounding box
            oblong_metadata: Oblong metadata with is_through_cut flag
            expansion: Expansion amount in mm (default 1.5mm)
            
        Returns:
            Expanded bounding box
        """
        expanded_bbox = {
            "min": bbox['min'].copy(),
            "max": bbox['max'].copy()
        }
        
        # Expand Z range for through-cuts to catch all faces
        if oblong_metadata.get('is_through_cut', False):
            expanded_bbox['min']['z'] -= expansion
            expanded_bbox['max']['z'] += expansion
            print(f"[INFO] Expanded bbox for through-cut: Z range {bbox['min']['z']} -> {expanded_bbox['min']['z']} to {bbox['max']['z']} -> {expanded_bbox['max']['z']}")
        
        return expanded_bbox
    
    
    @staticmethod
    def _match_oblong_faces_with_bbox(shape, face_id_map, oblong, bbox, tolerance, 
                                       expected_radius, direction_vec, 
                                       expected_planar, expected_cylindrical):
        """Match oblong faces using bbox filtering + validation layers."""
        import FreeCAD
        import math
        
        # [FIX] Layer 1: Bbox filtering with adaptive tolerance
        # Calculate adaptive tolerance based on bbox size to handle:
        # 1. Rotated oblongs where metadata bbox may be slightly off
        # 2. Complex shapes where bbox calculation has small errors
        # Use 10% of largest dimension or minimum 2mm for robustness
        bbox_x_size = bbox['max']['x'] - bbox['min']['x']
        bbox_y_size = bbox['max']['y'] - bbox['min']['y']
        bbox_z_size = bbox['max']['z'] - bbox['min']['z']
        max_dimension = max(bbox_x_size, bbox_y_size, bbox_z_size)
        adaptive_bbox_tolerance = max(tolerance, max_dimension * 0.1, 2.0)
        
        candidate_faces = []
        for i, face in enumerate(shape.Faces):
            face_bbox = face.BoundBox
            
            # Use intersection check instead of strict containment for robustness
            # This allows faces that slightly extend beyond bbox due to:
            # - Rounded ends extending beyond calculated bbox
            # - Small calculation errors in metadata
            # - Rotated oblongs where orientation may cause slight misalignment
            intersects = not (
                face_bbox.XMax < bbox['min']['x'] - adaptive_bbox_tolerance or
                face_bbox.XMin > bbox['max']['x'] + adaptive_bbox_tolerance or
                face_bbox.YMax < bbox['min']['y'] - adaptive_bbox_tolerance or
                face_bbox.YMin > bbox['max']['y'] + adaptive_bbox_tolerance or
                face_bbox.ZMax < bbox['min']['z'] - adaptive_bbox_tolerance or
                face_bbox.ZMin > bbox['max']['z'] + adaptive_bbox_tolerance
            )
            
            if intersects:
                face_id = face_id_map.get(
                    face.hashCode(),
                    f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}"
                )
                candidate_faces.append({'face': face, 'face_id': face_id, 'index': i})
        
        if not candidate_faces:
            return None
        
        # Layer 2-5: Type, normal, radius, area validation
        planar_candidates = [f for f in candidate_faces if 'Plane' in f['face'].Surface.TypeId]
        cylindrical_candidates = [f for f in candidate_faces if 'Cylinder' in f['face'].Surface.TypeId]
        
        # Layer 3: Normal validation for planar faces
        # For through-cut oblongs, planar faces can be:
        # - Parallel to direction (dot > 0.9): top/bottom faces
        # - Perpendicular to direction (dot < 0.1): wall faces
        top_bottom_faces = []
        wall_faces = []
        
        for f in planar_candidates:
            try:
                normal = f['face'].normalAt(0, 0)
                dot = abs(normal.dot(direction_vec))
                
                if dot > 0.9:  # Parallel -> top/bottom
                    top_bottom_faces.append(f)
                elif dot < 0.1:  # Perpendicular -> walls (for through-cut)
                    wall_faces.append(f)
            except:
                pass
        
        # Accept both types for through-cut oblongs
        valid_planar = top_bottom_faces + wall_faces
        
        valid_cylindrical = []
        radius_tol = max(0.1, expected_radius * 0.1)
        for f in cylindrical_candidates:
            try:
                if hasattr(f['face'].Surface, 'Radius'):
                    if abs(f['face'].Surface.Radius - expected_radius) < radius_tol:
                        valid_cylindrical.append(f)
            except:
                pass
        
        all_matched = valid_planar + valid_cylindrical
        
        if not all_matched:
            return None
        
        return {
            'face_ids': [f['face_id'] for f in all_matched],
            'planar': len(valid_planar),
            'cylindrical': len(valid_cylindrical),
            'total': len(all_matched)
        }
    
    @staticmethod
    def _match_oblong_face_by_plane(face_bbox, oblong, direction_vec, tolerance):
        """
        Match a face bbox with oblong based on plane intersection.
        
        Args:
            face_bbox: FreeCAD BoundBox of the face
            oblong: Oblong metadata dict
            direction_vec: FreeCAD Vector for direction
            tolerance: Tolerance in mm
            
        Returns:
            bool: True if face bbox intersects with oblong bbox
        """
        bbox = oblong.get('bounding_box', {})
        if not bbox:
            return False
        
        min_pt = bbox.get('min', {})
        max_pt = bbox.get('max', {})
        
        # [FIX] Check if face bbox INTERSECTS with oblong bbox (not strict containment)
        # Two bboxes intersect if they overlap in all 3 dimensions
        intersects = (
            face_bbox.XMin <= max_pt.get('x', 0) + tolerance and
            face_bbox.XMax >= min_pt.get('x', 0) - tolerance and
            face_bbox.YMin <= max_pt.get('y', 0) + tolerance and
            face_bbox.YMax >= min_pt.get('y', 0) - tolerance and
            face_bbox.ZMin <= max_pt.get('z', 0) + tolerance and
            face_bbox.ZMax >= min_pt.get('z', 0) - tolerance
        )
        
        return intersects
    
    @staticmethod
    def _match_oblong_faces_by_center(shape, face_id_map, oblong, center, search_radius,
                                       expected_radius, direction_vec,
                                       expected_planar, expected_cylindrical):
        """Match faces near oblong center (fallback strategy)."""
        import FreeCAD
        
        center_vec = FreeCAD.Vector(center['x'], center['y'], center['z'])
        candidate_faces = []
        
        for i, face in enumerate(shape.Faces):
            if center_vec.distanceToPoint(face.CenterOfMass) <= search_radius:
                face_id = face_id_map.get(
                    face.hashCode(),
                    f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}"
                )
                candidate_faces.append({'face': face, 'face_id': face_id})
        
        if not candidate_faces:
            return None
        
        planar_faces = []
        cylindrical_faces = []
        
        for f in candidate_faces:
            surface_type = f['face'].Surface.TypeId
            
            if 'Plane' in surface_type:
                try:
                    if abs(f['face'].normalAt(0, 0).dot(direction_vec)) > 0.9:
                        planar_faces.append(f)
                except:
                    pass
            elif 'Cylinder' in surface_type:
                try:
                    if abs(f['face'].Surface.Radius - expected_radius) < expected_radius * 0.15:
                        cylindrical_faces.append(f)
                except:
                    pass
        
        all_matched = planar_faces + cylindrical_faces
        
        if not all_matched:
            return None
        
        return {
            'face_ids': [f['face_id'] for f in all_matched],
            'planar': len(planar_faces),
            'cylindrical': len(cylindrical_faces),
            'total': len(all_matched)
        }
    
    @staticmethod
    def _geometric_oblong_detection(shape, face_id_map, hint_metadata):
        """Detect oblong from geometry alone (last resort fallback)."""
        planar_faces = []
        
        for i, face in enumerate(shape.Faces):
            if 'Plane' in face.Surface.TypeId:
                face_id = face_id_map.get(
                    face.hashCode(),
                    f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}"
                )
                planar_faces.append({'face': face, 'face_id': face_id, 'area': face.Area})
        
        if len(planar_faces) >= 2:
            planar_faces.sort(key=lambda x: x['area'])
            matched = planar_faces[:2]
            
            return {
                'face_ids': [f['face_id'] for f in matched],
                'planar': len(matched),
                'cylindrical': 0,
                'total': len(matched)
            }
        
        return None

    @staticmethod
    def _deduplicate_oblong_faces(oblong_features: List[Dict], shape, face_id_map: Dict) -> List[Dict]:
        """
        Remove duplicate faces from oblongs by assigning each face to the closest oblong.
        
        Args:
            oblong_features: List of oblong dictionaries with face_ids
            shape: FreeCAD Shape object
            face_id_map: Mapping of face hash to face ID
            
        Returns:
            List of oblongs with deduplicated faces
        """
        import FreeCAD
        
        # Build face-to-oblongs mapping
        face_to_oblongs = {}
        for oblong in oblong_features:
            for face_id in oblong.get('face_ids', []):
                if face_id not in face_to_oblongs:
                    face_to_oblongs[face_id] = []
                face_to_oblongs[face_id].append(oblong)
        
        # Resolve shared faces
        for face_id, sharing_oblongs in face_to_oblongs.items():
            if len(sharing_oblongs) > 1:
                # This face is shared by multiple oblongs!
                print(f"[DEDUP] Face {face_id} is shared by {len(sharing_oblongs)} oblongs")
                
                # Find the face object
                face = None
                for i, f in enumerate(shape.Faces):
                    if face_id_map.get(f.hashCode()) == face_id:
                        face = f
                        break
                
                if not face:
                    continue
                
                face_center = face.CenterOfMass
                
                # Find best oblong by QUALITY first, then distance
                # Priority: PERFECT > GOOD > PARTIAL
                quality_priority = {'PERFECT': 3, 'GOOD': 2, 'PARTIAL': 1, 'POOR': 0}
                
                best_oblong = None
                best_score = -1
                
                for oblong in sharing_oblongs:
                    oblong_center = oblong.get('position', {})
                    center_vec = FreeCAD.Vector(
                        oblong_center.get('x', 0),
                        oblong_center.get('y', 0),
                        oblong_center.get('z', 0)
                    )
                    
                    distance = face_center.distanceToPoint(center_vec)
                    quality = oblong.get('match_quality', 'PARTIAL')
                    
                    # Score = quality_priority * 1000 - distance
                    # This ensures quality is primary, distance is tiebreaker
                    score = quality_priority.get(quality, 0) * 1000 - distance
                    
                    if score > best_score:
                        best_score = score
                        best_oblong = oblong
                
                # Remove face from all oblongs except the best
                for oblong in sharing_oblongs:
                    if oblong != best_oblong:
                        oblong['face_ids'].remove(face_id)
                        oblong['face_breakdown']['total'] -= 1
                        
                        # Update type count
                        surface_type = face.Surface.TypeId
                        if 'Plane' in surface_type:
                            oblong['face_breakdown']['planar'] -= 1
                        elif 'Cylinder' in surface_type:
                            oblong['face_breakdown']['cylindrical'] -= 1
                        
                        print(f"[DEDUP] Removed {face_id} from oblong {oblong.get('id')} (distance={distance:.2f}mm)")
                
                print(f"[DEDUP] Assigned {face_id} to oblong {best_oblong.get('id')} (quality={best_oblong.get('match_quality')})")
        
        return oblong_features
    
    @staticmethod
    def _validate_oblong_completeness(oblong_features: List[Dict], min_faces: int = 5) -> List[Dict]:
        """
        Validate oblongs have minimum required faces and update quality.
        
        [TARGET] FLEXIBLE VALIDATION for oblongs with varying face counts (5-8 faces)
        
        Args:
            oblong_features: List of oblong dictionaries
            min_faces: Minimum number of faces required (default: 5)
            
        Returns:
            List of valid oblongs (filtered and updated)
        """
        valid_oblongs = []
        
        for oblong in oblong_features:
            total_faces = len(oblong.get('face_ids', []))
            planar = oblong.get('face_breakdown', {}).get('planar', 0)
            cylindrical = oblong.get('face_breakdown', {}).get('cylindrical', 0)
            
            # [TARGET] FLEXIBLE VALIDATION CRITERIA
            # Accept oblongs with:
            # - 4 cylinders (required)
            # - 1-4 planar faces (walls, shared walls, top/bottom)
            # - Total 5-8 faces
            
            if cylindrical == 4 and 1 <= planar <= 4 and total_faces >= 6:
                # [OK] PERFECT oblong (6-8 faces with 4 cylinders)
                oblong['confidence'] = 0.95
                oblong['match_quality'] = 'PERFECT'
                valid_oblongs.append(oblong)
                print(f"[VALIDATE] [OK] Oblong {oblong.get('id')}: {total_faces} faces (PERFECT - {planar}P + {cylindrical}C)")
                
            elif cylindrical == 4 and planar >= 1 and total_faces >= 5:
                # [OK] GOOD oblong (5 faces minimum)
                oblong['confidence'] = 0.85
                oblong['match_quality'] = 'GOOD'
                valid_oblongs.append(oblong)
                print(f"[VALIDATE] [OK] Oblong {oblong.get('id')}: {total_faces} faces (GOOD - {planar}P + {cylindrical}C)")
                
            elif total_faces >= 4:
                # [WARNING] PARTIAL oblong (incomplete but might be usable)
                oblong['confidence'] = 0.6
                oblong['match_quality'] = 'PARTIAL'
                valid_oblongs.append(oblong)
                print(f"[VALIDATE] [WARNING]  Oblong {oblong.get('id')}: {total_faces} faces (PARTIAL - {planar}P + {cylindrical}C)")
                
            else:
                # [ERROR] INVALID oblong (too few faces)
                print(f"[VALIDATE] [ERROR] Oblong {oblong.get('id')}: {total_faces} faces (REJECTED - too few)")
                # Don't add to valid_oblongs
        
        print(f"[VALIDATE] Summary: {len(valid_oblongs)}/{len(oblong_features)} oblongs validated")
        return valid_oblongs
    
    @staticmethod
    def _log_oblong_summary(oblong_features: List[Dict]):
        """
        Log summary of oblong detection results.
        
        Args:
            oblong_features: List of oblong dictionaries
        """
        if not oblong_features:
            print("[SUMMARY] No oblongs detected")
            return
        
        print(f"\n[SUMMARY] Detected {len(oblong_features)} oblong(s):")
        for oblong in oblong_features:
            oblong_id = oblong.get('id', 'unknown')
            total_faces = len(oblong.get('face_ids', []))
            quality = oblong.get('match_quality', 'N/A')
            confidence = oblong.get('confidence', 0)
            
            breakdown = oblong.get('face_breakdown', {})
            planar = breakdown.get('planar', 0)
            cylindrical = breakdown.get('cylindrical', 0)
            
            print(f"  - {oblong_id}: {total_faces} faces ({planar}P + {cylindrical}C) - {quality} (conf={confidence:.2f})")
        print()
    
    @staticmethod
    def _detect_oblongs_with_dual_orientation(shape, face_id_map: Dict, expected_radius: float,
                                               oblong_metadata: Dict, excluded_faces: set = None) -> Optional[Dict]:
        """
        [TARGET] PHASE 2: DUAL-ORIENTATION DETECTION WRAPPER
        
        Tries both horizontal and vertical orientations using Phase 1 metadata.
        
        Algorithm:
        1. Check if dual-orientation metadata exists
        2. Try horizontal orientation first (most common)
        3. If fails or low confidence, try vertical orientation
        4. Return best result
        
        Args:
            shape: FreeCAD Shape
            face_id_map: Face ID mapping
            expected_radius: Expected fillet radius
            oblong_metadata: Enhanced metadata with 'orientations' key
            excluded_faces: Set of already-matched face IDs
            
        Returns:
            Dict with face_ids and breakdown, or None if not found
        """
        orientations = oblong_metadata.get('orientations', {})
        
        if not orientations:
            # No dual-orientation metadata, use original detection
            print("[DUAL-ORIENT] No orientation metadata, using original detection")
            return FeatureExtractor._detect_oblongs_geometric(
                shape, face_id_map, expected_radius, oblong_metadata, excluded_faces
            )
        
        print("[DUAL-ORIENT] [TARGET] Trying both orientations...")
        
        results = []
        
        # Try horizontal orientation (0°)
        if 'horizontal' in orientations:
            print("[DUAL-ORIENT] -> Trying HORIZONTAL orientation...")
            
            # Create temporary metadata with horizontal orientation
            temp_metadata = oblong_metadata.copy()
            temp_metadata['bounding_box'] = orientations['horizontal'].get('bounding_box', {})
            
            # Update cylinder info for horizontal
            if 'cylindrical_faces_info' in temp_metadata:
                cyl_info = temp_metadata['cylindrical_faces_info'].copy()
                h_cyl = orientations['horizontal'].get('cylinder_positions', {})
                if 'left_end' in h_cyl:
                    cyl_info['left_end'] = h_cyl['left_end']
                if 'right_end' in h_cyl:
                    cyl_info['right_end'] = h_cyl['right_end']
                temp_metadata['cylindrical_faces_info'] = cyl_info
            
            result_h = FeatureExtractor._detect_oblongs_geometric(
                shape, face_id_map, expected_radius, temp_metadata, excluded_faces
            )
            
            if result_h:
                result_h['orientation'] = 'horizontal'
                result_h['orientation_confidence'] = 1.0 if result_h.get('total', 0) >= 6 else 0.7
                results.append(result_h)
                print(f"[DUAL-ORIENT] [OK] Horizontal: {result_h.get('total', 0)} faces")
        
        # Try vertical orientation (90°)
        if 'vertical' in orientations:
            print("[DUAL-ORIENT] -> Trying VERTICAL orientation...")
            
            # Create temporary metadata with vertical orientation
            temp_metadata = oblong_metadata.copy()
            temp_metadata['bounding_box'] = orientations['vertical'].get('bounding_box', {})
            
            # Update cylinder info for vertical
            if 'cylindrical_faces_info' in temp_metadata:
                cyl_info = temp_metadata['cylindrical_faces_info'].copy()
                v_cyl = orientations['vertical'].get('cylinder_positions', {})
                if 'left_end' in v_cyl:
                    cyl_info['left_end'] = v_cyl['left_end']
                if 'right_end' in v_cyl:
                    cyl_info['right_end'] = v_cyl['right_end']
                temp_metadata['cylindrical_faces_info'] = cyl_info
            
            result_v = FeatureExtractor._detect_oblongs_geometric(
                shape, face_id_map, expected_radius, temp_metadata, excluded_faces
            )
            
            if result_v:
                result_v['orientation'] = 'vertical'
                result_v['orientation_confidence'] = 1.0 if result_v.get('total', 0) >= 6 else 0.7
                results.append(result_v)
                print(f"[DUAL-ORIENT] [OK] Vertical: {result_v.get('total', 0)} faces")
        
        # Select best result
        if not results:
            print("[DUAL-ORIENT] [ERROR] Both orientations failed")
            return None
        
        # Sort by: 1) total faces, 2) orientation confidence
        results.sort(key=lambda r: (r.get('total', 0), r.get('orientation_confidence', 0)), reverse=True)
        best = results[0]
        
        print(f"[DUAL-ORIENT] [OK] Selected {best['orientation'].upper()} orientation ({best.get('total', 0)} faces)")
        
        return best
    
    @staticmethod
    def _validate_rectangle_pattern(cylinders, expected_length, expected_width, hint_center):
        """
        [NEW] Score how well 4 cylinders form expected rectangle pattern.
        
        Used to prevent selecting wrong cylinders in closely-spaced oblongs (gap < 10mm).
        
        Args:
            cylinders: List of 4 cylinder dicts with 'center', 'radius', 'face_id'
            expected_length: Expected oblong length (mm)
            expected_width: Expected oblong width (mm)
            hint_center: FreeCAD.Vector - approximate center from metadata
        
        Returns:
            float: Score from 0.0 (bad) to 1.0 (perfect)
        """
        if len(cylinders) != 4:
            return 0.0
        
        centers = [c['center'] for c in cylinders]
        
        # Calculate bbox from cylinder centers
        xs = [c.x for c in centers]
        ys = [c.y for c in centers]
        zs = [c.z for c in centers]
        
        bbox_min_x, bbox_max_x = min(xs), max(xs)
        bbox_min_y, bbox_max_y = min(ys), max(ys)
        bbox_min_z, bbox_max_z = min(zs), max(zs)
        
        # Check if cylinders spread enough (not all in a line)
        x_spread = bbox_max_x - bbox_min_x
        y_spread = bbox_max_y - bbox_min_y
        
        if x_spread < 1.0 or y_spread < 1.0:
            return 0.0  # Cylinders too close in one dimension
        
        # Dimension matching (try both orientations)
        actual_dim_x = x_spread
        actual_dim_y = y_spread
        
        # Horizontal: length-X, width-Y
        dim_error_h = (
            abs(actual_dim_x - expected_length) / max(expected_length, 1) +
            abs(actual_dim_y - expected_width) / max(expected_width, 1)
        ) / 2.0
        
        # Vertical: length-Y, width-X (90° rotated)
        dim_error_v = (
            abs(actual_dim_x - expected_width) / max(expected_width, 1) +
            abs(actual_dim_y - expected_length) / max(expected_length, 1)
        ) / 2.0
        
        dim_error = min(dim_error_h, dim_error_v)
        dim_score = max(0, 1.0 - dim_error)
        
        if dim_score < 0.7:
            return 0.0  # Dimensions don't match well enough
        
        # Center proximity to hint
        actual_center_x = (bbox_min_x + bbox_max_x) / 2.0
        actual_center_y = (bbox_min_y + bbox_max_y) / 2.0
        actual_center_z = (bbox_min_z + bbox_max_z) / 2.0
        actual_center = FreeCAD.Vector(actual_center_x, actual_center_y, actual_center_z)
        
        distance = actual_center.distanceToPoint(hint_center)
        proximity_score = max(0, 1.0 - distance / 20.0)  # 20mm tolerance
        
        # Rectangle pattern: check if cylinders are near corners
        corners = [
            (bbox_min_x, bbox_min_y),
            (bbox_min_x, bbox_max_y),
            (bbox_max_x, bbox_min_y),
            (bbox_max_x, bbox_max_y)
        ]
        
        corners_filled = 0
        for corner_x, corner_y in corners:
            for center in centers:
                dx = abs(center.x - corner_x)
                dy = abs(center.y - corner_y)
                if dx < 2.0 and dy < 2.0:  # 2mm tolerance
                    corners_filled += 1
                    break
        
        corner_score = corners_filled / 4.0
        
        # Combined score (weighted)
        final_score = (
            dim_score * 0.5 +        # Dimensions most important
            corner_score * 0.3 +     # Rectangle pattern
            proximity_score * 0.2    # Proximity to hint
        )
        
        return final_score
    
    @staticmethod
    def _find_best_rectangle_combo(all_cylinders, expected_length, expected_width, hint_center):
        """
        [NEW] Find best combination of 4 cylinders forming a rectangle.
        
        Fixes issue where "4 closest" cylinders may include cylinders from adjacent oblongs.
        Uses combinatorial search with rectangle pattern validation.
        
        Args:
            all_cylinders: List of cylinder candidates
            expected_length: Expected oblong length (mm)
            expected_width: Expected oblong width (mm)
            hint_center: FreeCAD.Vector - approximate center from metadata
        
        Returns:
            List of 4 cylinders forming best rectangle, or None
        """
        from itertools import combinations
        
        if len(all_cylinders) < 4:
            return None
        
        best_combo = None
        best_score = 0.6  # Minimum threshold
        
        # Limit combinations to avoid performance issues
        max_combinations = min(50, len(list(combinations(range(len(all_cylinders)), 4))))
        combination_count = 0
        
        for combo in combinations(all_cylinders, 4):
            combination_count += 1
            if combination_count > max_combinations:
                break
            
            score = FeatureExtractor._validate_rectangle_pattern(
                combo,
                expected_length,
                expected_width,
                hint_center
            )
            
            if score > best_score:
                best_score = score
                best_combo = list(combo)
        
        if best_combo:
            print(f"[GEOMETRIC] [OK] Best rectangle: score={best_score:.2f} (tested {combination_count} combos)")
        
        return best_combo

    @staticmethod
    def _detect_oblongs_geometric(shape, face_id_map: Dict, expected_radius: float, 
                                   oblong_metadata: Dict, excluded_faces: set = None) -> Optional[Dict]:
        """
        [TARGET] PHASE 2: OPTIMIZED GEOMETRIC DETECTION with Enhanced Metadata
        
        Uses Phase 1 enhanced metadata for accurate detection of closely-spaced oblongs.
        
        Algorithm:
        1. BBox Pre-filtering (with Y-range for closely-spaced oblongs)
        2. End-based Cylinder Grouping (left_end vs right_end)
        3. Multi-layer Validation (radius+epsilon, arc length, distances)
        4. Rectangle Pattern Validation
        5. Planar Face Matching
        
        Args:
            shape: FreeCAD Shape
            face_id_map: Face ID mapping
            expected_radius: Expected fillet radius (width/2)
            oblong_metadata: Enhanced metadata from Phase 1
            excluded_faces: Set of already-matched face IDs
            
        Returns:
            Dict with face_ids and breakdown, or None if not found
        """
        import FreeCAD
        import math
        
        excluded_faces = excluded_faces or set()
        
        # [TARGET] STEP 1: EXTRACT ENHANCED METADATA
        cyl_info = oblong_metadata.get('cylindrical_faces_info', {})
        validation_hints = oblong_metadata.get('validation_hints', {})
        bbox = oblong_metadata.get('bounding_box', {})
        planar_info = oblong_metadata.get('planar_faces_info', {})
        
        # Get center from metadata
        center_dict = oblong_metadata.get('center', {})
        if center_dict:
            metadata_center = FreeCAD.Vector(
                center_dict.get('x', 0),
                center_dict.get('y', 0),
                center_dict.get('z', 0)
            )
        elif bbox:
            # Calculate center from bbox
            metadata_center = FreeCAD.Vector(
                (bbox.get('min', {}).get('x', 0) + bbox.get('max', {}).get('x', 0)) / 2.0,
                (bbox.get('min', {}).get('y', 0) + bbox.get('max', {}).get('y', 0)) / 2.0,
                (bbox.get('min', {}).get('z', 0) + bbox.get('max', {}).get('z', 0)) / 2.0
            )
        else:
            metadata_center = FreeCAD.Vector(0, 0, 0)
        
        # Get precise parameters from metadata
        actual_radius = cyl_info.get('radius', expected_radius)  # With epsilon
        radius_tolerance = cyl_info.get('radius_tolerance', 0.5)
        expected_arc_length = cyl_info.get('arc_length', 0)
        
        left_end = cyl_info.get('left_end', {})
        right_end = cyl_info.get('right_end', {})
        
        expected_distances = validation_hints.get('expected_distances', {})
        within_end_dist = expected_distances.get('within_end', 0)
        between_ends_dist = expected_distances.get('between_ends', 0)
        
        print(f"\n[GEOMETRIC] [TARGET] Phase 2: Enhanced detection")
        print(f"[GEOMETRIC] Radius: {actual_radius:.6f}mm (±{radius_tolerance}mm)")
        print(f"[GEOMETRIC] Arc length: {expected_arc_length}mm")
        print(f"[GEOMETRIC] Left end: X={left_end.get('x')}, Y={left_end.get('y_range')}")
        print(f"[GEOMETRIC] Right end: X={right_end.get('x')}, Y={right_end.get('y_range')}")
        
        # [TARGET] STEP 2: FIND ALL CYLINDERS WITH CORRECT RADIUS (NO BBOX FILTER)
        # For oblongs that can be rotated 90°, bbox from metadata may not match actual geometry
        all_candidate_cylinders = []
        
        for i, face in enumerate(shape.Faces):
            if 'Cylinder' not in face.Surface.TypeId:
                continue
                
            if not hasattr(face.Surface, 'Radius'):
                continue
            
            radius = face.Surface.Radius
            face_center = face.CenterOfMass
            face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + i % 26)}")
            
            # Skip if already matched
            if face_id in excluded_faces:
                continue
            
            # [FIX] LAYER 1: Radius check with adaptive tolerance
            # Use relative tolerance (30% of radius) or minimum from metadata
            adaptive_tolerance = max(radius_tolerance, actual_radius * 0.3)
            if abs(radius - actual_radius) > adaptive_tolerance:
                continue
            print(f"[GEOMETRIC] Cylinder {face_id}: radius={radius:.2f}mm (expected={actual_radius:.2f}mm, tolerance={adaptive_tolerance:.2f}mm) - MATCHED")
            
            # [OK] LAYER 2: Arc length validation
            max_edge_length = max((edge.Length for edge in face.Edges), default=0)
            if expected_arc_length > 0:
                arc_diff = abs(max_edge_length - expected_arc_length)
                if arc_diff > 2.0:  # 2mm tolerance
                    continue
            
            all_candidate_cylinders.append({
                'face': face,
                'face_id': face_id,
                'center': face_center,
                'radius': radius,
                'arc_length': max_edge_length
            })
        
            print(f"[GEOMETRIC] Found {len(all_candidate_cylinders)} cylinders with R~{actual_radius:.3f}mm")
        
        if len(all_candidate_cylinders) < 4:
            print(f"[GEOMETRIC] [ERROR] Not enough cylinders (need 4, found {len(all_candidate_cylinders)})")
            return None
        
        # [FIX] STEP 2.5: FILTER BY BBOX 3D (X, Y, Z) thay vì chỉ Y
        # Nguyên nhân shared faces: Chỉ filter theo Y → không đủ để phân biệt oblongs
        # Giải pháp: Dùng bbox 3D từ metadata (từ pnt) để filter chính xác hơn
        bbox = oblong_metadata.get('bounding_box', {})
        if not bbox:
            print(f"[GEOMETRIC] [ERROR] Missing bbox in metadata, cannot filter by 3D bbox")
            return None
        
        bbox_min = bbox.get('min', {})
        bbox_max = bbox.get('max', {})
        
        # Filter cylinders bằng bbox 3D (X, Y, Z) với tolerance
        bbox_tolerance = 5.0  # mm tolerance
        candidate_cylinders = []
        for c in all_candidate_cylinders:
            center = c['center']
            # Kiểm tra cylinder có nằm trong bbox 3D không (X, Y, Z)
            is_in_bbox_3d = (
                bbox_min.get('x', -float('inf')) - bbox_tolerance <= center.x <= bbox_max.get('x', float('inf')) + bbox_tolerance and
                bbox_min.get('y', -float('inf')) - bbox_tolerance <= center.y <= bbox_max.get('y', float('inf')) + bbox_tolerance and
                bbox_min.get('z', -float('inf')) - bbox_tolerance <= center.z <= bbox_max.get('z', float('inf')) + bbox_tolerance
            )
            if is_in_bbox_3d:
                candidate_cylinders.append(c)
        
        print(f"[GEOMETRIC] After bbox 3D filter (X, Y, Z): {len(candidate_cylinders)} candidates")
        print(f"[GEOMETRIC] Bbox 3D: X=[{bbox_min.get('x', 0):.1f}, {bbox_max.get('x', 0):.1f}], "
              f"Y=[{bbox_min.get('y', 0):.1f}, {bbox_max.get('y', 0):.1f}], "
              f"Z=[{bbox_min.get('z', 0):.1f}, {bbox_max.get('z', 0):.1f}]")
        
        if len(candidate_cylinders) < 4:
            print(f"[GEOMETRIC] [ERROR] Not enough nearby cylinders (need 4, found {len(candidate_cylinders)})")
            return None
        
        # [IMPROVED] STEP 3: FIND BEST RECTANGLE COMBINATION
        # Don't just pick 4 closest - validate rectangle geometry to prevent selecting
        # cylinders from adjacent oblongs (fixes gap 5mm issue)
        print(f"[GEOMETRIC] Searching for best rectangle pattern...")
        
        selected_cylinders = FeatureExtractor._find_best_rectangle_combo(
            candidate_cylinders,
            expected_length=oblong_metadata.get('total_length', 15.0),
            expected_width=oblong_metadata.get('width', 5.0),
            hint_center=metadata_center
        )
        
        if not selected_cylinders:
            print(f"[GEOMETRIC] [ERROR] No valid rectangle pattern found")
            return None
        
        print(f"[GEOMETRIC] Selected cylinders: {[c['face_id'] for c in selected_cylinders]}")
        
        # Calculate actual bbox from selected cylinders
        all_x = [c['center'].x for c in selected_cylinders]
        all_y = [c['center'].y for c in selected_cylinders]
        all_z = [c['center'].z for c in selected_cylinders]
        
        actual_bbox_min_x, actual_bbox_max_x = min(all_x), max(all_x)
        actual_bbox_min_y, actual_bbox_max_y = min(all_y), max(all_y)
        actual_bbox_min_z, actual_bbox_max_z = min(all_z), max(all_z)
        
        actual_length_x = actual_bbox_max_x - actual_bbox_min_x
        actual_width_y = actual_bbox_max_y - actual_bbox_min_y
        
        print(f"[GEOMETRIC] Actual dimensions: X={actual_length_x:.1f}mm, Y={actual_width_y:.1f}mm, Z={actual_bbox_max_z - actual_bbox_min_z:.1f}mm")
        
        # [TARGET] STEP 3.5: DUAL-ORIENTATION VALIDATION
        expected_total_length = oblong_metadata.get('total_length', 0)
        expected_width = oblong_metadata.get('width', 0)
        
        dim_tolerance = 2.0  # mm
        
        # Check orientation 1: Horizontal (length along X, width along Y)
        horizontal_match = (
            abs(actual_length_x - expected_total_length) < dim_tolerance and
            abs(actual_width_y - expected_width) < dim_tolerance
        )
        
        # Check orientation 2: Vertical (length along Y, width along X) - ROTATED 90°
        vertical_match = (
            abs(actual_width_y - expected_total_length) < dim_tolerance and
            abs(actual_length_x - expected_width) < dim_tolerance
        )
        
        if horizontal_match:
            print(f"[GEOMETRIC] [OK] Horizontal orientation: L={actual_length_x:.1f}mm (X), W={actual_width_y:.1f}mm (Y)")
        elif vertical_match:
            print(f"[GEOMETRIC] [OK] Vertical orientation (90° rotated): L={actual_width_y:.1f}mm (Y), W={actual_length_x:.1f}mm (X)")
        else:
            print(f"[GEOMETRIC] [WARNING]  Dimension mismatch:")
            print(f"  Actual: X={actual_length_x:.1f}mm, Y={actual_width_y:.1f}mm")
            print(f"  Expected: L={expected_total_length}mm, W={expected_width}mm")
            print(f"  Neither horizontal nor vertical orientation matches!")
            # Continue anyway - might still be valid
        
        # [TARGET] STEP 4: RECTANGLE PATTERN VALIDATION
        # Validate 4 cylinders form a rectangle
        centers = [c['center'] for c in selected_cylinders]
        distances = []
        for i in range(len(centers)):
            for j in range(i+1, len(centers)):
                dist = centers[i].distanceToPoint(centers[j])
                distances.append(dist)
        
        distances.sort()
        
        dist_tolerance = 2.0  # mm
        
        # Expected pattern: 2×within_end, 2×between_ends, 2×diagonal
        expected_diagonal = expected_distances.get('diagonal', 0)
        pattern_match = (
            abs(distances[0] - within_end_dist) < dist_tolerance and
            abs(distances[1] - within_end_dist) < dist_tolerance and
            abs(distances[2] - between_ends_dist) < dist_tolerance * 2 and
            abs(distances[3] - between_ends_dist) < dist_tolerance * 2
        )
        
        if pattern_match:
            print(f"[GEOMETRIC] [OK] Rectangle pattern validated")
        else:
            print(f"[GEOMETRIC] [WARNING]  Rectangle pattern weak (distances: {[f'{d:.1f}' for d in distances]})")
        
        # [NEW] STEP 4.5: CALCULATE BBOX 3D TỪ 4 CYLINDERS (trong flow chính)
        # Mục đích: Tạo hình hộp chữ nhật bao quanh oblongs từ 4 cylinders
        # Bbox 3D này sẽ được dùng để filter planar faces chính xác hơn (X, Y, Z)
        cylinders_bbox_3d = {
            'min': {
                'x': actual_bbox_min_x,
                'y': actual_bbox_min_y,
                'z': actual_bbox_min_z
            },
            'max': {
                'x': actual_bbox_max_x,
                'y': actual_bbox_max_y,
                'z': actual_bbox_max_z
            }
        }
        
        # Mở rộng bbox 3D một chút để bao quát planar faces
        bbox_padding = expected_radius + 2.0  # radius + 2mm padding
        cylinders_bbox_3d['min']['x'] -= bbox_padding
        cylinders_bbox_3d['min']['y'] -= bbox_padding
        cylinders_bbox_3d['min']['z'] -= bbox_padding
        cylinders_bbox_3d['max']['x'] += bbox_padding
        cylinders_bbox_3d['max']['y'] += bbox_padding
        cylinders_bbox_3d['max']['z'] += bbox_padding
        
        print(f"[GEOMETRIC] Bbox 3D from 4 cylinders: "
              f"X=[{cylinders_bbox_3d['min']['x']:.1f}, {cylinders_bbox_3d['max']['x']:.1f}], "
              f"Y=[{cylinders_bbox_3d['min']['y']:.1f}, {cylinders_bbox_3d['max']['y']:.1f}], "
              f"Z=[{cylinders_bbox_3d['min']['z']:.1f}, {cylinders_bbox_3d['max']['z']:.1f}]")
        
        # [TARGET] STEP 5: FIND PLANAR FACES - PRIORITY-BASED SELECTION
        expected_area = planar_info.get('area_expected', 0)
        area_tolerance = planar_info.get('area_tolerance', 0.15)
        
        # [FIX] Sử dụng bbox 3D từ 4 cylinders để filter planar faces
        # Mục đích: Bbox 3D tạo hình hộp chữ nhật bao quanh oblongs
        # → Faces nào nằm trong bbox 3D này → thuộc về oblong này
        # → Không còn shared faces vì mỗi oblong có bbox 3D riêng
        
        # Calculate cylinder cluster center
        cluster_center = FreeCAD.Vector(
            sum(c['center'].x for c in selected_cylinders) / len(selected_cylinders),
            sum(c['center'].y for c in selected_cylinders) / len(selected_cylinders),
            sum(c['center'].z for c in selected_cylinders) / len(selected_cylinders)
        )
        
        # Classify planar faces: top/bottom vs walls
        top_bottom_faces = []
        wall_faces = []
        
        for i, face in enumerate(shape.Faces):
            if 'Plane' not in face.Surface.TypeId:
                continue
            
            face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + i % 26)}")
            
            if face_id in excluded_faces:
                continue
            
            face_center = face.CenterOfMass
            face_bbox = face.BoundBox  # Bbox của 1 face (2 điểm: min, max)
            
            # [FIX] Check if face bbox nằm trong bbox 3D từ cylinders (X, Y, Z)
            # Không chỉ check center, mà check cả bbox của face
            is_in_bbox_3d = (
                cylinders_bbox_3d['min']['x'] <= face_bbox.XMin and
                face_bbox.XMax <= cylinders_bbox_3d['max']['x'] and
                cylinders_bbox_3d['min']['y'] <= face_bbox.YMin and
                face_bbox.YMax <= cylinders_bbox_3d['max']['y'] and
                cylinders_bbox_3d['min']['z'] <= face_bbox.ZMin and
                face_bbox.ZMax <= cylinders_bbox_3d['max']['z']
            )
            
            if not is_in_bbox_3d:
                continue
            
            area = face.Area
            normal = face.normalAt(0, 0)
            distance = face_center.distanceToPoint(cluster_center)
            
            face_info = {
                'face': face,
                'face_id': face_id,
                'center': face_center,
                'area': area,
                'normal': normal,
                'distance_to_cluster': distance
            }
            
            # Classify by normal direction
            if abs(normal.z) > 0.9:
                # Top/Bottom face (parallel to Z-axis)
                top_bottom_faces.append(face_info)
            elif abs(normal.z) < 0.1:
                # Wall face (perpendicular to Z-axis)
                wall_faces.append(face_info)
        
        # Sort by distance to cluster (closest first)
        top_bottom_faces.sort(key=lambda f: f['distance_to_cluster'])
        wall_faces.sort(key=lambda f: f['distance_to_cluster'])
        
        print(f"[GEOMETRIC] Classified planar: {len(top_bottom_faces)} top/bottom, {len(wall_faces)} walls")
        
        # PRIORITY-BASED SELECTION
        selected_planar = []
        
        # PRIORITY 1: Top/Bottom faces (ALWAYS take 2 if available)
        if len(top_bottom_faces) >= 2:
            selected_planar = top_bottom_faces[:2]
            print(f"[GEOMETRIC] Selected 2 top/bottom faces: {[f['face_id'] for f in selected_planar]}")
        elif len(top_bottom_faces) == 1:
            selected_planar = top_bottom_faces[:1]
            print(f"[GEOMETRIC] Selected 1 top/bottom face: {[f['face_id'] for f in selected_planar]}")
            # Need 1 more - take from walls
            if len(wall_faces) > 0:
                selected_planar.append(wall_faces[0])
                print(f"[GEOMETRIC] Added 1 wall face to reach 2: {wall_faces[0]['face_id']}")
        else:
            # No top/bottom - take 2 walls
            if len(wall_faces) >= 2:
                selected_planar = wall_faces[:2]
                print(f"[GEOMETRIC] No top/bottom, selected 2 wall faces: {[f['face_id'] for f in selected_planar]}")
            elif len(wall_faces) == 1:
                selected_planar = wall_faces[:1]
                print(f"[GEOMETRIC] Only 1 wall face available")
        
        # PRIORITY 2: Shared wall for vertical oblongs (OPTIONAL)
        # Detect if oblong is vertical (rotated 90°)
        is_vertical = (
            actual_width_y > actual_length_x and
            abs(actual_width_y - expected_total_length) < 3.0
        )
        
        if is_vertical and len(selected_planar) == 2 and len(wall_faces) > 0:
            # Vertical oblong may have shared wall - add 1 more wall face
            # Find wall not already selected
            unused_walls = [w for w in wall_faces if w not in selected_planar]
            if len(unused_walls) > 0:
                selected_planar.append(unused_walls[0])
                print(f"[GEOMETRIC] Vertical oblong - added shared wall: {unused_walls[0]['face_id']}")
        
        if len(selected_planar) < 1:
            print(f"[GEOMETRIC] [WARNING] No planar faces found")
            # Still return cylinders if we have them
            if len(selected_cylinders) == 4:
                print(f"[GEOMETRIC] [WARNING] Returning with {len(selected_cylinders)} cylinders only")
                return {
                    'face_ids': [c['face_id'] for c in selected_cylinders],
                    'planar': 0,
                    'cylindrical': len(selected_cylinders),
                    'total': len(selected_cylinders)
                }
            return None
        
        print(f"[GEOMETRIC] Final selection: {[p['face_id'] for p in selected_planar]} ({len(selected_planar)} planar faces)")
        
        # [TARGET] STEP 8: CALCULATE BBOX 3D TỔNG HỢP TỪ TẤT CẢ FACES (6-8 faces)
        # Mục đích: Tạo hình hộp chữ nhật bao quanh toàn bộ oblong từ actual faces
        all_faces = selected_cylinders + selected_planar
        all_face_bboxes = []
        for face_info in all_faces:
            if 'face' in face_info:
                all_face_bboxes.append(face_info['face'].BoundBox)
        
        # Bbox 3D tổng hợp = min(min) và max(max) của tất cả faces
        final_bbox_3d = None
        if all_face_bboxes:
            final_bbox_3d = {
                'min': {
                    'x': min(b.XMin for b in all_face_bboxes),
                    'y': min(b.YMin for b in all_face_bboxes),
                    'z': min(b.ZMin for b in all_face_bboxes)
                },
                'max': {
                    'x': max(b.XMax for b in all_face_bboxes),
                    'y': max(b.YMax for b in all_face_bboxes),
                    'z': max(b.ZMax for b in all_face_bboxes)
                }
            }
            print(f"[GEOMETRIC] Final bbox 3D from {len(all_face_bboxes)} faces: "
                  f"X=[{final_bbox_3d['min']['x']:.1f}, {final_bbox_3d['max']['x']:.1f}], "
                  f"Y=[{final_bbox_3d['min']['y']:.1f}, {final_bbox_3d['max']['y']:.1f}], "
                  f"Z=[{final_bbox_3d['min']['z']:.1f}, {final_bbox_3d['max']['z']:.1f}]")

        # [TARGET] STEP 9: COMBINE AND RETURN
        
        return {
            'face_ids': [f['face_id'] for f in all_faces],
            'planar': len(selected_planar),
            'cylindrical': len(selected_cylinders),
            'total': len(all_faces),
            'bbox_3d': final_bbox_3d  # [NEW] Bbox 3D từ actual faces
        }
    
    @staticmethod
    def _detect_oblong_by_geometry_tier2(shape, face_id_map: Dict, oblong_metadata: Dict, 
                                         excluded_faces: Set = None) -> Optional[Dict]:
        """
        [NEW] TIER 2: Cylinder-First Detection with DBSCAN Clustering
        
        This is the CRITICAL function to fix nested oblongs (case 4).
        Strategy: Find cylinders first, cluster them, validate rectangle pattern.
        
        Args:
            shape: FreeCAD Shape
            face_id_map: Face ID mapping
            oblong_metadata: Oblong metadata (dimensions only, NOT bbox!)
            excluded_faces: Set of already-matched face IDs
        
        Returns:
            Dict with face_ids and breakdown, or None if not found
        """
        if not HAS_GEOMETRY_UTILS:
            print("[TIER 2] [ERROR] geometry_utils_3d not available, skipping Tier 2")
            return None
        
        import FreeCAD
        
        excluded_faces = excluded_faces or set()
        
        # Extract metadata
        total_length = oblong_metadata.get('total_length', oblong_metadata.get('straight_length', 0) + oblong_metadata.get('width', 0))
        width = oblong_metadata.get('width', 0)
        expected_radius = width / 2.0
        depth = oblong_metadata.get('depth', oblong_metadata.get('height', 0))
        
        print(f"\n[TIER 2] Starting cylinder-first detection...")
        print(f"[TIER 2] Expected: length={total_length:.1f}mm, width={width:.1f}mm, radius={expected_radius:.2f}mm")
        
        # STEP 1: Find ALL cylinders with correct radius (NO BBOX FILTER!)
        all_cylinders = []
        for i, face in enumerate(shape.Faces):
            if 'Cylinder' not in face.Surface.TypeId:
                continue
            
            if not hasattr(face.Surface, 'Radius'):
                continue
            
            radius = face.Surface.Radius
            face_center = face.CenterOfMass
            face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + i % 26)}")
            
            # Skip if already matched
            if face_id in excluded_faces:
                continue
            
            # [FIX] Radius check with adaptive tolerance (0.5mm base, or 30% of radius for larger oblongs)
            radius_tolerance_tier2 = max(0.5, expected_radius * 0.3)  # At least 0.5mm, or 30% of radius
            if abs(radius - expected_radius) > radius_tolerance_tier2:
                continue
            print(f"[TIER 2] Cylinder {face_id}: radius={radius:.2f}mm (expected={expected_radius:.2f}mm, tolerance={radius_tolerance_tier2:.2f}mm) - MATCHED")
            
            # Get arc length
            max_edge_length = max((edge.Length for edge in face.Edges), default=0)
            
            all_cylinders.append({
                'face': face,
                'face_id': face_id,
                'center': {'x': face_center.x, 'y': face_center.y, 'z': face_center.z},
                'radius': radius,
                'arc_length': max_edge_length,
                'center_vec': face_center  # Keep FreeCAD Vector for compatibility
            })
        
            print(f"[TIER 2] Found {len(all_cylinders)} cylinders with R~{expected_radius:.2f}mm")
        
        if len(all_cylinders) < 4:
            print(f"[TIER 2] [ERROR] Not enough cylinders (need 4, found {len(all_cylinders)})")
            return None
        
        # STEP 2: Pre-filter by radius bucket (IMPORTANT: prevents wrong grouping)
        # [FIX] Use adaptive tolerance for grouping
        bucket_tolerance = max(0.5, expected_radius * 0.3)
        radius_buckets = group_cylinders_by_radius(all_cylinders, tolerance=bucket_tolerance)
        matching_bucket = None
        for bucket_radius, cylinders in radius_buckets.items():
            # [FIX] Use same adaptive tolerance for matching
            if abs(bucket_radius - expected_radius) < bucket_tolerance:
                matching_bucket = cylinders
                break
        
        if not matching_bucket:
            print(f"[TIER 2] [ERROR] No matching radius bucket for {expected_radius:.2f}mm")
            return None
        
        print(f"[TIER 2] Pre-filtered to {len(matching_bucket)} cylinders in radius bucket")
        
        # STEP 3: DBSCAN Clustering
        if len(matching_bucket) <= 4:
            # Only 1 oblong possible, skip clustering
            clusters = [list(range(len(matching_bucket)))]
            print(f"[TIER 2] Only {len(matching_bucket)} cylinders, skipping clustering")
        else:
            # Calculate adaptive epsilon
            eps = calculate_adaptive_epsilon(total_length, width, max_eps=50.0)
            print(f"[TIER 2] DBSCAN clustering: eps={eps:.2f}mm, min_samples=4")
            
            # Extract points for clustering
            points = [c['center'] for c in matching_bucket]
            cluster_indices = dbscan_3d(points, eps, min_samples=4)
            
            # Filter clusters with exactly 4 cylinders (perfect oblongs)
            clusters = [c for c in cluster_indices if len(c) == 4]
            
            if not clusters:
                # Try clusters with >= 4 cylinders (might have multiple oblongs)
                clusters = [c for c in cluster_indices if len(c) >= 4]
                print(f"[TIER 2] Found {len(clusters)} cluster(s) with >= 4 cylinders")
            else:
                print(f"[TIER 2] Found {len(clusters)} perfect cluster(s) with 4 cylinders")
        
        if not clusters:
            print(f"[TIER 2] [ERROR] No valid clusters found")
            return None
        
        # STEP 4: Validate rectangle pattern for each cluster
        MAX_COMBINATIONS_PER_CLUSTER = 100  # Early termination
        
        best_pattern = None
        best_score = 0.0
        
        for cluster_idx, cluster_indices in enumerate(clusters):
            cluster_cylinders = [matching_bucket[i] for i in cluster_indices]
            
            if len(cluster_cylinders) == 4:
                # Perfect cluster - validate directly
                pattern = FeatureExtractor._validate_rectangle_pattern_3d(
                    cluster_cylinders, total_length, width, depth
                )
                if pattern and pattern.get('score', 0) > best_score:
                    best_pattern = pattern
                    best_score = pattern['score']
                    best_pattern['cylinders'] = cluster_cylinders
            elif len(cluster_cylinders) > 4:
                # Multiple oblongs in cluster - try combinations
                print(f"[TIER 2] Cluster {cluster_idx}: {len(cluster_cylinders)} cylinders, trying combinations...")
                combinations = itertools.combinations(cluster_cylinders, 4)
                
                for combo in itertools.islice(combinations, MAX_COMBINATIONS_PER_CLUSTER):
                    pattern = FeatureExtractor._validate_rectangle_pattern_3d(
                        list(combo), total_length, width, depth
                    )
                    if pattern and pattern.get('score', 0) > best_score:
                        best_pattern = pattern
                        best_score = pattern['score']
                        best_pattern['cylinders'] = list(combo)
                        break  # Found valid rectangle, stop searching
        
        if not best_pattern or best_score < 0.7:
            print(f"[TIER 2] [ERROR] No valid rectangle pattern found (best score: {best_score:.2f})")
            return None
        
        print(f"[TIER 2] [OK] Found valid rectangle pattern (score: {best_score:.2f})")
        selected_cylinders = best_pattern['cylinders']
        
        # STEP 5: Find planar faces using 3D sphere search
        pattern_center = best_pattern.get('center', {})
        search_radius = max(total_length, width) / 2.0 + 5.0
        
        planar_faces = []
        for i, face in enumerate(shape.Faces):
            if 'Plane' not in face.Surface.TypeId:
                continue
            
            face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + i % 26)}")
            if face_id in excluded_faces:
                continue
            
            # Check if face center is within search sphere
            face_center = face.CenterOfMass
            face_center_dict = {'x': face_center.x, 'y': face_center.y, 'z': face_center.z}
            dist = euclidean_distance_3d(pattern_center, face_center_dict)
            
            if dist > search_radius:
                continue
            
            # Validate normal vector
            direction = oblong_metadata.get('direction', {'x': 0, 'y': 0, 'z': 1})
            try:
                normal = face.normalAt(0, 0)
                normal_dict = {'x': normal.x, 'y': normal.y, 'z': normal.z}
                dot = abs(dot_product_3d(normal_dict, direction))
                
                if dot > 0.98:  # Parallel to direction (top/bottom)
                    planar_faces.append({
                        'face': face,
                        'face_id': face_id,
                        'normal': normal_dict
                    })
            except:
                pass
        
        # Select 2 best planar faces (opposite normals, closest to expected area)
        selected_planar = []
        if len(planar_faces) >= 2:
            # Sort by area (closest to expected)
            expected_area = (total_length - width) * width + math.pi * (expected_radius ** 2)
            planar_faces.sort(key=lambda f: abs(f['face'].Area - expected_area))
            
            # Take 2 with opposite normals
            for face_info in planar_faces:
                if len(selected_planar) >= 2:
                    break
                
                # Check if normal is opposite to already selected
                if len(selected_planar) == 0:
                    selected_planar.append(face_info)
                else:
                    # Check if opposite normal
                    existing_normal = selected_planar[0]['normal']
                    current_normal = face_info['normal']
                    dot = dot_product_3d(existing_normal, current_normal)
                    if dot < -0.9:  # Opposite directions
                        selected_planar.append(face_info)
        
        if len(selected_planar) < 1:
            print(f"[TIER 2] [WARNING] No planar faces found, returning cylinders only")
            if len(selected_cylinders) == 4:
                return {
                    'face_ids': [c['face_id'] for c in selected_cylinders],
                    'planar': 0,
                    'cylindrical': 4,
                    'total': 4,
                    'bbox_3d': best_pattern.get('bbox_3d')
                }
            return None
        
        # Combine results
        all_faces = selected_cylinders + selected_planar
        face_ids = [f['face_id'] for f in all_faces]
        
        # Calculate final bbox 3D
        all_centers = [c['center'] for c in selected_cylinders]
        for p in selected_planar:
            center = p['face'].CenterOfMass
            all_centers.append({'x': center.x, 'y': center.y, 'z': center.z})
        
        if all_centers:
            final_bbox_3d = {
                'min': {
                    'x': min(c['x'] for c in all_centers),
                    'y': min(c['y'] for c in all_centers),
                    'z': min(c['z'] for c in all_centers)
                },
                'max': {
                    'x': max(c['x'] for c in all_centers),
                    'y': max(c['y'] for c in all_centers),
                    'z': max(c['z'] for c in all_centers)
                }
            }
        else:
            final_bbox_3d = best_pattern.get('bbox_3d')
        
        print(f"[TIER 2] [OK] Success: {len(selected_cylinders)} cylinders + {len(selected_planar)} planar = {len(all_faces)} faces")
        
        return {
            'face_ids': face_ids,
            'planar': len(selected_planar),
            'cylindrical': len(selected_cylinders),
            'total': len(all_faces),
            'bbox_3d': final_bbox_3d,
            'confidence': best_score
        }
    
    @staticmethod
    def _validate_rectangle_pattern_3d(cylinders: List[Dict], expected_length: float, 
                                        expected_width: float, expected_depth: float) -> Optional[Dict]:
        """
        Validate that 4 cylinders form a valid rectangle pattern in 3D.
        
        Returns:
            Dict with 'score' (0-1), 'center', 'bbox_3d', or None if invalid
        """
        if len(cylinders) != 4:
            return None
        
        # Calculate 6 pairwise distances
        centers = [c['center'] for c in cylinders]
        distances = []
        for i in range(4):
            for j in range(i+1, 4):
                dist = euclidean_distance_3d(centers[i], centers[j])
                distances.append(dist)
        
        distances.sort()
        
        # Expected pattern: 2×width, 2×length, 2×diagonal
        expected_diagonal = math.sqrt(expected_length**2 + expected_width**2)
        
        # Validate distances
        width_tolerance = 2.0  # mm
        length_tolerance = 3.0  # mm
        diagonal_tolerance = 3.0  # mm
        
        width_match = (
            abs(distances[0] - expected_width) < width_tolerance and
            abs(distances[1] - expected_width) < width_tolerance
        )
        length_match = (
            abs(distances[2] - expected_length) < length_tolerance and
            abs(distances[3] - expected_length) < length_tolerance
        )
        diagonal_match = (
            abs(distances[4] - expected_diagonal) < diagonal_tolerance and
            abs(distances[5] - expected_diagonal) < diagonal_tolerance
        )
        
        if not (width_match and length_match and diagonal_match):
            return None
        
        # Check coplanarity
        max_distance = 0.0
        try:
            plane = fit_plane_3d(centers)
            for center in centers:
                dist = distance_to_plane_3d(center, plane)
                max_distance = max(max_distance, dist)
            
            if max_distance > 2.0:  # Not coplanar enough
                return None
        except:
            max_distance = 1.0  # Assume good coplanarity if fit fails
        
        # Calculate center and bbox
        center = {
            'x': sum(c['x'] for c in centers) / 4.0,
            'y': sum(c['y'] for c in centers) / 4.0,
            'z': sum(c['z'] for c in centers) / 4.0
        }
        
        bbox_3d = {
            'min': {
                'x': min(c['x'] for c in centers),
                'y': min(c['y'] for c in centers),
                'z': min(c['z'] for c in centers)
            },
            'max': {
                'x': max(c['x'] for c in centers),
                'y': max(c['y'] for c in centers),
                'z': max(c['z'] for c in centers)
            }
        }
        
        # Calculate score (0-1)
        score = 0.0
        if width_match:
            score += 0.3
        if length_match:
            score += 0.3
        if diagonal_match:
            score += 0.2
        if max_distance < 1.0:  # Good coplanarity
            score += 0.2
        
        return {
            'score': score,
            'center': center,
            'bbox_3d': bbox_3d
        }

    @staticmethod
    def _add_oblongs_from_metadata(shape, face_id_map: Dict, oblong_metadata: List[Dict]) -> List[Dict[str, Any]]:
        """
        [OK] UPGRADED: Progressive matching with multi-strategy fallback.
        
        Add oblong features from metadata using progressive relaxation:
        
        STRATEGY 1: STRICT BBOX (tolerance=0.5mm, confidence=0.95)
          - Tight bbox matching
          - Best quality, but may miss faces due to tessellation
        
        STRATEGY 2: EXPANDED BBOX (adaptive tolerance, confidence=0.85)
          - Adaptive tolerance based on oblong size
          - Expands bbox for through-cuts (negative Z)
          - Handles most real-world cases
        
        STRATEGY 3: CENTER-BASED (radius=15mm, confidence=0.75)
          - Match faces near oblong center
          - Fallback for misaligned geometry
        
        STRATEGY 4: GEOMETRIC DETECTION (confidence=0.60)
          - Detect filleted rectangles from geometry alone
          - Last resort when metadata fails
        
        Validation Layers (for each strategy):
        1. Bbox filtering (with strategy-specific tolerance)
        2. Surface type classification (planar vs cylindrical)
        3. Normal vector validation (top/bottom vs walls)
        4. Radius validation (10% tolerance)
        5. Area validation (50% tolerance)
        6. Confidence scoring
        
        Args:
            shape: FreeCAD Shape object
            face_id_map: Mapping of face hash to face ID
            oblong_metadata: List of oblong dictionaries from metadata
            
        Returns:
            List of oblong feature dictionaries with validated faces
        """
        print(f"[DEBUG] _add_oblongs_from_metadata called with {len(oblong_metadata)} oblongs")
        print(f"[DEBUG] Face ID map size: {len(face_id_map)}")
        import FreeCAD
        
        # [NEW] PREPROCESSING STAGE: 3D Spatial Indexing and Conflict Detection
        print("\n" + "="*70)
        print("PREPROCESSING: 3D Spatial Indexing")
        print("="*70)
        
        # Step 1: Extract ALL cylindrical faces and group by radius
        print(f"\n[PREPROCESS] Step 1: Extracting all cylindrical faces...")
        all_cylinders = []
        for i, face in enumerate(shape.Faces):
            if 'Cylinder' not in face.Surface.TypeId:
                continue
            
            if not hasattr(face.Surface, 'Radius'):
                continue
            
            radius = face.Surface.Radius
            face_center = face.CenterOfMass
            face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + i % 26)}")
            
            all_cylinders.append({
                'face': face,
                'face_id': face_id,
                'center': {'x': face_center.x, 'y': face_center.y, 'z': face_center.z},
                'radius': radius
            })
        
        print(f"[PREPROCESS] Found {len(all_cylinders)} total cylindrical faces")
        
        # [DEBUG] Show radius distribution
        if all_cylinders:
            radius_dist = {}
            for cyl in all_cylinders:
                r = round(cyl['radius'], 2)
                if r not in radius_dist:
                    radius_dist[r] = []
                radius_dist[r].append(cyl)
            print(f"[PREPROCESS] Radius distribution:")
            for r, cyl_list in sorted(radius_dist.items())[:10]:  # Show first 10
                print(f"  {r:.2f}mm: {len(cyl_list)} cylinders")
        
        # Group by radius buckets
        if HAS_GEOMETRY_UTILS:
            # [FIX] Use adaptive tolerance for preprocessing grouping
            # Get expected radius from first oblong (if available)
            if oblong_metadata:
                first_oblong = oblong_metadata[0]
                first_radius = first_oblong.get('width', 0) / 2.0
                bucket_tolerance = max(0.5, first_radius * 0.3)
                print(f"[PREPROCESS] Expected radius: {first_radius:.2f}mm, bucket tolerance: {bucket_tolerance:.2f}mm")
            else:
                bucket_tolerance = 0.5
            radius_buckets = group_cylinders_by_radius(all_cylinders, tolerance=bucket_tolerance)
            print(f"[PREPROCESS] Grouped into {len(radius_buckets)} radius buckets (tolerance: {bucket_tolerance:.2f}mm)")
            
            # [DEBUG] Show buckets near expected radius
            if oblong_metadata:
                expected_radius = oblong_metadata[0].get('width', 0) / 2.0
                print(f"[PREPROCESS] Buckets near expected radius {expected_radius:.2f}mm:")
                for bucket_r, cyl_list in radius_buckets.items():
                    diff = abs(bucket_r - expected_radius)
                    if diff <= bucket_tolerance * 2:  # Show within 2x tolerance
                        print(f"  Bucket {bucket_r:.2f}mm: {len(cyl_list)} cylinders (diff: {diff:.2f}mm)")
        else:
            radius_buckets = {}
        
        # Step 2: Sort metadata by confidence
        print(f"\n[PREPROCESS] Step 2: Sorting metadata by confidence...")
        def get_confidence(oblong):
            source = oblong.get('source', 'unknown')
            if 'makeOblong' in source or 'call' in source:
                return 0.95
            elif 'llm' in source.lower():
                return 0.85
            elif 'regex' in source.lower():
                return 0.75
            else:
                return 0.70
        
        sorted_oblongs = sorted(
            oblong_metadata,
            key=lambda o: (
                -get_confidence(o),  # Higher confidence first
                o.get('center', {}).get('z', 0),  # Then by Z
                o.get('center', {}).get('y', 0),  # Then by Y
                o.get('center', {}).get('x', 0)   # Then by X
            )
        )
        
        print(f"[PREPROCESS] Sorted {len(sorted_oblongs)} oblongs by confidence and position")
        for i, o in enumerate(sorted_oblongs[:5]):  # Show first 5
            center = o.get('center', {})
            conf = get_confidence(o)
            print(f"  Oblong {i+1}: confidence={conf:.2f}, center=({center.get('x', 0):.1f}, {center.get('y', 0):.1f}, {center.get('z', 0):.1f})")
        
        # Step 3: Detect conflicts (bbox overlap)
        print(f"\n[PREPROCESS] Step 3: Detecting bbox conflicts...")
        conflict_groups = []
        if HAS_GEOMETRY_UTILS:
            for i, oblong1 in enumerate(sorted_oblongs):
                bbox1 = oblong1.get('bounding_box')
                if not bbox1:
                    continue
                
                for j, oblong2 in enumerate(sorted_oblongs[i+1:], start=i+1):
                    bbox2 = oblong2.get('bounding_box')
                    if not bbox2:
                        continue
                    
                    # Check intersection
                    if bbox_3d_intersection(bbox1, bbox2):
                        # Calculate overlap percentage
                        overlap_volume = (
                            min(bbox1['max']['x'], bbox2['max']['x']) - max(bbox1['min']['x'], bbox2['min']['x'])
                        ) * (
                            min(bbox1['max']['y'], bbox2['max']['y']) - max(bbox1['min']['y'], bbox2['min']['y'])
                        ) * (
                            min(bbox1['max']['z'], bbox2['max']['z']) - max(bbox1['min']['z'], bbox2['min']['z'])
                        )
                        
                        volume1 = (
                            (bbox1['max']['x'] - bbox1['min']['x']) *
                            (bbox1['max']['y'] - bbox1['min']['y']) *
                            (bbox1['max']['z'] - bbox1['min']['z'])
                        )
                        
                        overlap_pct = (overlap_volume / volume1 * 100) if volume1 > 0 else 0
                        
                        if overlap_pct > 50:  # More than 50% overlap
                            conflict_groups.append([i, j])
                            print(f"[PREPROCESS] Conflict detected: Oblong {i+1} <-> Oblong {j+1} ({overlap_pct:.1f}% overlap)")
        
        print(f"[PREPROCESS] Found {len(conflict_groups)} conflict group(s)")
        
        # Mark conflicted oblongs
        conflicted_indices = set()
        for group in conflict_groups:
            conflicted_indices.update(group)
        
        print(f"[PREPROCESS] {len(conflicted_indices)} oblong(s) in conflicts (will use Tier 2)")
        
        oblong_features = []
        already_matched_faces = set()  # [OK] Track faces already assigned to oblongs
        
        for oblong_idx, oblong in enumerate(sorted_oblongs):
            print(f"\n" + "="*70)
            print(f"[INFO] Processing oblong {oblong_idx + 1}/{len(oblong_metadata)}")
            print("="*70)
            
            # Show oblong parameters
            print(f"[INFO] Oblong params: length={oblong.get('total_length', 0):.2f}, width={oblong.get('width', 0):.2f}, depth={oblong.get('depth', 0):.2f}")
            print(f"[INFO] Expected: 2 planar, 4 cylindrical (radius={oblong.get('width', 0)/2.0:.2f})")
            
            # Check if this oblong is in conflict group
            is_conflicted = oblong_idx in conflicted_indices
            if is_conflicted:
                print(f"[INFO] Oblong {oblong_idx + 1} is in conflict group -> will skip Tier 1, use Tier 2")
            
            # Get or calculate bounding box
            bbox = oblong.get('bounding_box')
            
            if not bbox:
                # Calculate bbox from oblong parameters
                print(f"[INFO] Oblong missing bounding_box, calculating from parameters...")
                
                corner = oblong.get('corner', {})
                total_length = oblong.get('total_length', oblong.get('straight_length', 0) + oblong.get('width', 0))
                width = oblong.get('width', 0)
                depth = oblong.get('depth', oblong.get('height', 0))
                direction = oblong.get('direction', {'x': 0, 'y': 0, 'z': 1})
                
                if not corner or total_length == 0 or width == 0:
                    print(f"[WARNING] Oblong missing required parameters, skipping")
                    continue
                
                # Calculate bbox based on direction
                end_radius = width / 2.0
                
                if abs(direction.get('z', 0)) > 0.9:  # Z-up extrusion
                    bbox = {
                        "min": {
                            "x": corner.get('x', 0) - end_radius,
                            "y": corner.get('y', 0),
                            "z": max(corner.get('z', 0), 0.0)
                        },
                        "max": {
                            "x": corner.get('x', 0) + total_length + end_radius,
                            "y": corner.get('y', 0) + width,
                            "z": max(corner.get('z', 0) + depth, 5.0)
                        }
                    }
                else:
                    bbox = {
                        "min": {
                            "x": corner.get('x', 0),
                            "y": corner.get('y', 0),
                            "z": corner.get('z', 0)
                        },
                        "max": {
                            "x": corner.get('x', 0) + total_length,
                            "y": corner.get('y', 0) + width,
                            "z": corner.get('z', 0) + depth
                        }
                    }
                
                print(f"[OK] Calculated bbox: {bbox}")
            
            # Calculate center if missing
            center = oblong.get('center')
            if not center and bbox:
                center = {
                    "x": (bbox['min']['x'] + bbox['max']['x']) / 2.0,
                    "y": (bbox['min']['y'] + bbox['max']['y']) / 2.0,
                    "z": (bbox['min']['z'] + bbox['max']['z']) / 2.0
                }
            
            # Extract oblong parameters
            oblong_total_length = oblong.get('total_length', oblong.get('straight_length', 0) + oblong.get('width', 0))
            oblong_width = oblong.get('width', 0)
            oblong_depth = oblong.get('depth', oblong.get('height', 0))
            expected_radius = oblong_width / 2.0
            direction_dict = oblong.get('direction', {'x': 0, 'y': 0, 'z': 1})
            direction_vec = FreeCAD.Vector(direction_dict['x'], direction_dict['y'], direction_dict['z'])
            
            # Expected counts
            expected_faces = oblong.get('expected_faces', {'planar': 2, 'cylindrical': 4, 'total': 6})
            expected_planar = expected_faces.get('planar', 2)
            expected_cylindrical = expected_faces.get('cylindrical', 4)
            
            print(f"[INFO] Oblong params: length={oblong_total_length}, width={oblong_width}, depth={oblong_depth}")
            print(f"[INFO] Expected: {expected_planar} planar, {expected_cylindrical} cylindrical (radius={expected_radius:.2f})")
            
            # [NEW] 3-TIER DETECTION FLOW
            # Tier 1: Try dual-orientation geometric detection (existing)
            # Tier 2: Cylinder-first with DBSCAN (for nested oblongs)
            # Tier 3: Fallback to bbox matching (existing)
            
            tier1_result = None
            if not is_conflicted:
                # Only try Tier 1 if not in conflict
                print(f"\n[TIER 1] Attempting dual-orientation geometric detection...")
                tier1_result = FeatureExtractor._detect_oblongs_with_dual_orientation(
                    shape, face_id_map, expected_radius, oblong, already_matched_faces
                )
            else:
                print(f"\n[TIER 1] Skipped (conflict detected, using Tier 2 directly)")
            
            # [✅ OPTIMIZED] FLEXIBLE FACE COUNT VALIDATION
            # Standard oblongs: 6 faces (80% of cases)
            # - 2 planar caps (top/bottom filleted rectangles)
            # - 4 cylindrical corners (rounded ends)
            # 
            # Edge cases (20%):
            # - 7 faces: One wall split (4 cylinders + 3 planar)
            # - 5 faces: Minimal (4 cylinders + 1 merged planar)
            # - 8+ faces: Complex topology (reserved for future)
            
            MIN_FACES_STRICT = 6    # Standard oblong
            MIN_FACES_RELAXED = 5   # Allow minimal case
            MAX_FACES = 8           # Upper limit (future: complex cases)
            
            total_matched = tier1_result.get('total', 0) if tier1_result else 0
            geometric_result = None
            
            if total_matched >= MIN_FACES_RELAXED and total_matched <= MAX_FACES:
                # Tier 1 success
                geometric_result = tier1_result
                print(f"[TIER 1] [OK] Success: {total_matched} faces matched")
            else:
                # Tier 1 failed, try Tier 2
                print(f"[TIER 1] [FAILED] Failed ({total_matched} faces), trying Tier 2...")
                tier2_result = FeatureExtractor._detect_oblong_by_geometry_tier2(
                    shape, face_id_map, oblong, already_matched_faces
                )
                
                if tier2_result and tier2_result.get('total', 0) >= MIN_FACES_RELAXED:
                    # Tier 2 success
                    geometric_result = tier2_result
                    print(f"[TIER 2] ✅ Success: {tier2_result.get('total', 0)} faces matched")
                else:
                    # Both Tier 1 and Tier 2 failed, will fallback to bbox matching
                    print(f"[TIER 2] [FAILED] Failed, will fallback to bbox matching")
                    geometric_result = None
            
            if geometric_result and geometric_result.get('total', 0) >= MIN_FACES_RELAXED:
                # [OK] SUCCESS: Found sufficient faces geometrically!
                matching_face_ids = geometric_result['face_ids']
                planar_count = geometric_result['planar']
                cylindrical_count = geometric_result['cylindrical']
                bbox_3d_from_faces = geometric_result.get('bbox_3d')  # [NEW] Bbox 3D từ actual faces
                
                # ✅ CHECK: Standard 6-face pattern (2P + 4C)
                is_standard = (planar_count == 2 and cylindrical_count == 4)
                
                if is_standard:
                    print(f"[GEOMETRIC] [OK] PERFECT: Standard 6-face oblong (2 planar caps + 4 cylindrical corners)")
                    confidence = 0.95
                    match_quality = 'PERFECT'
                elif total_matched == 6:
                    print(f"[GEOMETRIC] [OK] GOOD: 6 faces but non-standard split ({planar_count}P + {cylindrical_count}C)")
                    confidence = 0.90
                    match_quality = 'GOOD'
                elif total_matched == 5:
                    print(f"[GEOMETRIC] ⚠️  ACCEPTABLE: Minimal 5-face oblong ({planar_count}P + {cylindrical_count}C)")
                    confidence = 0.80
                    match_quality = 'GOOD'
                elif total_matched >= 7:
                    print(f"[GEOMETRIC] ⚠️  ACCEPTABLE: {total_matched} faces (complex case) ({planar_count}P + {cylindrical_count}C)")
                    confidence = 0.85
                    match_quality = 'GOOD'
                
                # [IMPROVED] CLAIM all faces to prevent conflicts
                already_matched_faces.update(matching_face_ids)
                
                # [NEW] Detailed logging for claimed faces
                claimed_cylinders = [fid for fid in matching_face_ids if 'cylinder' in str(type(fid)).lower() or cylindrical_count > 0]
                print(f"[GEOMETRIC] Claimed {len(matching_face_ids)} faces -> Prevents conflicts with holes/bending")
                
                # [NEW] Sử dụng bbox 3D từ actual faces (chính xác hơn bbox từ metadata)
                # Bbox 3D từ faces = hình hộp chữ nhật bao quanh toàn bộ oblong
                final_bbox = bbox_3d_from_faces if bbox_3d_from_faces else bbox
                
                if bbox_3d_from_faces:
                    print(f"[GEOMETRIC] Using bbox 3D from {total_matched} actual faces (more accurate than metadata bbox)")
                
                # [FIX] Calculate center from corner if center is invalid
                calculated_center = center
                if not center or (center.get('x', 0) == 0 and center.get('y', 0) == 0 and center.get('z', 0) == 0):
                    # Calculate from corner + dimensions
                    corner = oblong.get('corner', {})
                    if corner:
                        calculated_center = {
                            'x': corner.get('x', 0) + oblong_total_length / 2.0,
                            'y': corner.get('y', 0) + oblong_width / 2.0,
                            'z': corner.get('z', 0) + oblong_depth / 2.0
                        }
                        print(f"[FIX] Calculated center from corner: {calculated_center}")
                
                # Create oblong feature with validated faces
                oblong_feature = {
                    "type": "oblong",
                    "id": oblong.get('id', f"oblong_{len(oblong_features) + 1:03d}"),
                    "straight_length": round(oblong.get('straight_length', 0), 3),
                    "width": round(oblong_width, 3),
                    "total_length": round(oblong_total_length, 3),
                    "depth": round(oblong_depth, 3),
                    "fillet_radius": round(expected_radius, 3),
                    "position": calculated_center,  # [FIX] Use calculated center
                    "center": calculated_center,  # [FIX] Also add center field for compatibility
                    "corner": oblong.get('corner', {}),
                    "direction": direction_dict,
                    "bounding_box": final_bbox,  # [NEW] Sử dụng bbox 3D từ faces
                    "face_ids": matching_face_ids,
                    "face_breakdown": {
                        "planar": planar_count,
                        "cylindrical": cylindrical_count,
                        "other": 0,
                        "total": total_matched
                    },
                    "geometry_type": oblong.get('geometry_type', 'filleted_rectangle'),
                    "from_metadata": True,
                    "confidence": confidence,
                    "match_quality": match_quality,
                    "source": oblong.get('source', 'unknown'),
                    "units": oblong.get('units', 'mm')
                }
                try:
                    oblong_features.append(oblong_feature)
                    print(f"[OK] Added oblong {oblong_feature.get('id')}: {len(matching_face_ids)} faces matched ({match_quality})\n")
                except Exception as e:
                    pass
            
            else:
                # [WARNING] Geometric detection failed or incomplete, fallback to bbox matching
                if geometric_result:
                    print(f"[GEOMETRIC] [WARNING]  Incomplete: {geometric_result.get('total', 0)} faces, falling back to bbox")
                else:
                    print(f"[GEOMETRIC] [ERROR] Failed, falling back to bbox matching")
            
                # === LAYER 1: ADAPTIVE BBOX VALIDATION ===
                # [OK] OPTIMIZED: Adaptive tolerance based on oblong size
                base_tolerance = max(
                    oblong_total_length * 0.1,  # 10% of length
                    oblong_width * 0.1,          # 10% of width
                    2.0                          # Minimum 2mm
                )
                
                # [OK] OPTIMIZED: Auto-expand for through-cut oblongs
                is_through_cut = oblong.get('is_through_cut', False)
                if is_through_cut:
                    tolerance = base_tolerance * 1.5  # 50% more for through-cuts
                    print(f"[Layer 1] Through-cut: tolerance={tolerance:.2f}mm (expanded)")
                else:
                    tolerance = base_tolerance
                    print(f"[Layer 1] Adaptive tolerance={tolerance:.2f}mm")
                candidate_faces = []
                
                for i, face in enumerate(shape.Faces):
                    # FreeCAD cung cấp bbox cho từng face riêng lẻ (2 điểm: min, max)
                    face_bbox = face.BoundBox
                    
                    # [IMPROVED] Sử dụng bbox để xác định mặt phẳng và match với oblong
                    # Bbox 2 điểm cho biết:
                    # - Mặt nằm trên mặt phẳng nào (XY, XZ, YZ)
                    # - Tọa độ của mặt phẳng
                    # - Kích thước trên mặt phẳng
                    
                    # Match face với oblong dựa trên mặt phẳng từ bbox
                    is_matched = FeatureExtractor._match_oblong_face_by_plane(
                        face_bbox, oblong, direction_vec, tolerance
                    )
                    
                    if is_matched:
                        # [OK] FILTER: Reject large box walls (area > 3x expected oblong area)
                        face_area = face.Area
                        max_expected_area = (oblong_total_length * oblong_width) * 3.0  # 3x max
                        
                        if face_area > max_expected_area:
                            # This is likely a box wall, not an oblong face
                            continue
                        
                        face_id = face_id_map.get(
                            face.hashCode(),
                            f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}"
                        )
                        candidate_faces.append({
                            'face': face,
                            'face_id': face_id,
                            'index': i
                        })
                
                print(f"[Layer 1] Strict bbox filter: {len(candidate_faces)} candidates")
                
                # === LAYER 2: FACE TYPE CLASSIFICATION ===
                planar_candidates = []
                cylindrical_candidates = []
                
                for face_info in candidate_faces:
                    face = face_info['face']
                    surface_type = face.Surface.TypeId
                    
                    if 'Plane' in surface_type:
                        planar_candidates.append(face_info)
                    elif 'Cylinder' in surface_type:
                        cylindrical_candidates.append(face_info)
                
                print(f"[Layer 2] Type classification: {len(planar_candidates)} planar, {len(cylindrical_candidates)} cylindrical")
                
                # === LAYER 3: NORMAL VECTOR VALIDATION (for planar faces) ===
                # For through-cut oblongs, planar faces can be:
                # - Parallel to direction (dot > 0.9): top/bottom faces
                # - Perpendicular to direction (dot < 0.1): wall faces (common for through-cuts)
                top_bottom_faces = []
                wall_faces = []
                
                for face_info in planar_candidates:
                    face = face_info['face']
                    try:
                        normal = face.normalAt(0, 0)
                        dot = abs(normal.dot(direction_vec))
                        
                        if dot > 0.9:  # Parallel to direction -> top/bottom
                            top_bottom_faces.append(face_info)
                            print(f"[Layer 3] Face {face_info['face_id']}: top/bottom (dot={dot:.3f})")
                        elif dot < 0.1:  # Perpendicular -> wall (valid for through-cuts!)
                            wall_faces.append(face_info)
                            print(f"[Layer 3] Face {face_info['face_id']}: wall (dot={dot:.3f}) - valid for through-cut")
                    except Exception as e:
                        pass
                        print(f"[Layer 3] Failed to check normal for face {face_info['face_id']}: {e}")
                
                # Combine both types for through-cut oblongs
                all_planar_faces = top_bottom_faces + wall_faces
                print(f"[Layer 3] Normal validation: {len(top_bottom_faces)} top/bottom, {len(wall_faces)} walls, {len(all_planar_faces)} total planar")
                
                # === LAYER 4: RADIUS VALIDATION (for cylindrical faces) ===
                # Cylindrical faces must have radius = width/2
                valid_cylindrical = []
                # [FIX] Adaptive tolerance: use relative tolerance (30% of radius) or minimum 1.5mm
                radius_tolerance = max(1.5, expected_radius * 0.3)  # At least 1.5mm, or 30% of radius
                print(f"[Layer 4] Radius validation: expected={expected_radius:.2f}mm, tolerance={radius_tolerance:.2f}mm")
                
                for face_info in cylindrical_candidates:
                    face = face_info['face']
                    try:
                        surface = face.Surface
                        if hasattr(surface, 'Radius'):
                            radius = surface.Radius
                            
                            if abs(radius - expected_radius) < radius_tolerance:
                                valid_cylindrical.append(face_info)
                                print(f"[Layer 4] Face {face_info['face_id']}: valid cylindrical (radius={radius:.2f}mm, expected={expected_radius:.2f}mm)")
                            else:
                                print(f"[Layer 4] Face {face_info['face_id']}: rejected (radius={radius:.2f}mm, expected={expected_radius:.2f}mm, diff={abs(radius - expected_radius):.2f}mm)")
                    except Exception as e:
                        pass
                        print(f"[Layer 4] Failed to check radius for face {face_info['face_id']}: {e}")
                
                print(f"[Layer 4] Radius validation: {len(valid_cylindrical)} valid cylindrical")
                
                # === LAYER 5: AREA VALIDATION (additional safety) ===
                # CORRECTED FORMULAS:
                # Top/bottom area: Filleted rectangle = straight section + rounded ends
                #   = (straight_length × width) + (π × radius²)
                # Cylindrical area: Quarter cylinder (90° arc) = (π × radius × depth) / 4
                
                straight_length = oblong.get('straight_length', oblong_total_length - oblong_width)
                
                # Filleted rectangle area (top/bottom faces)
                straight_area = straight_length * oblong_width
                rounded_area = 3.14159 * (expected_radius ** 2)
                expected_top_bottom_area = straight_area + rounded_area
                
                # Quarter cylinder area (each of 4 cylindrical faces)
                expected_cyl_area = (3.14159 * expected_radius * oblong_depth) / 4.0
                
                print(f"[Layer 5] Expected areas: planar={expected_top_bottom_area:.2f} mm², cylindrical={expected_cyl_area:.2f} mm²")
                
                # [OK] SKIP area validation for planar faces (wall faces have different/zero area)
                # Just accept all planar faces that passed normal validation
                area_validated_planar = all_planar_faces
                print(f"[Layer 5] Planar faces: {len(area_validated_planar)} (area validation skipped for walls)")
                
                area_validated_cyl = []
                for face_info in valid_cylindrical:
                    area = face_info['face'].Area
                    # Allow 50% tolerance for area matching
                    if expected_cyl_area * 0.5 <= area <= expected_cyl_area * 1.5:
                        area_validated_cyl.append(face_info)
                        print(f"[Layer 5] Face {face_info['face_id']}: area OK ({area:.2f} mm²)")
                    else:
                        print(f"[Layer 5] Face {face_info['face_id']}: area mismatch ({area:.2f} mm², expected ~{expected_cyl_area:.2f})")
                
                print(f"[Layer 5] Area validation: {len(area_validated_planar)} planar, {len(area_validated_cyl)} cylindrical")
                
                # === LAYER 6: CONFIDENCE SCORING ===
                planar_count = len(area_validated_planar)
                cylindrical_count = len(area_validated_cyl)
                total_matched = planar_count + cylindrical_count
                
                # Calculate confidence based on match quality
                if planar_count == expected_planar and cylindrical_count == expected_cylindrical:
                    confidence = 0.95  # Perfect match
                    match_quality = "PERFECT"
                elif planar_count >= 1 and cylindrical_count >= 2:
                    confidence = 0.75  # Partial match
                    match_quality = "PARTIAL"
                else:
                    confidence = 0.5  # Poor match
                    match_quality = "POOR"
                
                print(f"[Layer 6] Match quality: {match_quality} (confidence={confidence:.2f})")
                print(f"          Found: {planar_count}/{expected_planar} planar, {cylindrical_count}/{expected_cylindrical} cylindrical")
                
                # Collect all validated face IDs (only for bbox matching path)
                matched_faces = area_validated_planar + area_validated_cyl
                matching_face_ids = [f['face_id'] for f in matched_faces]
                # [FIX] Calculate center from corner if center is invalid
                calculated_center = center
                if not center or (center.get('x', 0) == 0 and center.get('y', 0) == 0 and center.get('z', 0) == 0):
                    # Calculate from corner + dimensions
                    corner = oblong.get('corner', {})
                    if corner:
                        calculated_center = {
                            'x': corner.get('x', 0) + oblong_total_length / 2.0,
                            'y': corner.get('y', 0) + oblong_width / 2.0,
                            'z': corner.get('z', 0) + oblong_depth / 2.0
                        }
                        print(f"[FIX] Calculated center from corner: {calculated_center}")
                
                # Create oblong feature with validated faces
                oblong_feature = {
                    "type": "oblong",
                    "id": oblong.get('id', f"oblong_{len(oblong_features) + 1:03d}"),
                    "straight_length": round(oblong.get('straight_length', 0), 3),
                    "width": round(oblong_width, 3),
                    "total_length": round(oblong_total_length, 3),
                    "depth": round(oblong_depth, 3),
                    "fillet_radius": round(expected_radius, 3),
                    "position": calculated_center,  # [FIX] Use calculated center
                    "center": calculated_center,  # [FIX] Also add center field for compatibility
                    "corner": oblong.get('corner', {}),
                    "direction": direction_dict,
                    "bounding_box": bbox,
                    "face_ids": matching_face_ids,
                    "face_breakdown": {
                        "planar": planar_count,
                        "cylindrical": cylindrical_count,
                        "other": 0,
                        "total": total_matched
                    },
                    "geometry_type": oblong.get('geometry_type', 'filleted_rectangle'),
                    "from_metadata": True,
                    "confidence": confidence,
                    "match_quality": match_quality,
                    "source": oblong.get('source', 'unknown'),
                    "units": oblong.get('units', 'mm')
                }
                try:
                    oblong_features.append(oblong_feature)
                    print(f"[OK] Added oblong {oblong_feature.get('id')}: {len(matching_face_ids)} faces matched ({match_quality})\n")
                except Exception as e:
                    pass
            traceback.print_exc()
        
        # [OK] POST-PROCESSING: Optimize oblong detection
        print("\n" + "="*70)
        print("POST-PROCESSING: Oblong Optimization")
        print("="*70)
        
        # STEP 0: Pre-filter - Remove obviously incomplete oblongs BEFORE deduplication
        # This prevents fake/incomplete oblongs from stealing faces during dedup
        print(f"\n[STEP 0] Pre-filtering incomplete oblongs...")
        print(f"  Before filter: {len(oblong_features)} oblongs")
        
        pre_filtered = []
        for oblong in oblong_features:
            total_faces = len(oblong.get('face_ids', []))
            if total_faces >= 6:
                pre_filtered.append(oblong)
            else:
                print(f"  [ERROR] Rejected {oblong.get('id')}: only {total_faces} faces (need >= 6)")
        
        oblong_features = pre_filtered
        print(f"  After filter: {len(oblong_features)} oblongs")
        
        # STEP 1: Deduplicate shared faces (only among valid oblongs)
        print(f"\n[STEP 1] Deduplicating shared faces...")
        oblong_features = FeatureExtractor._deduplicate_oblong_faces(
            oblong_features, shape, face_id_map
        )
        
        # STEP 2: Validate completeness ([OK] Require 6 faces minimum quality)
        print(f"\n[STEP 2] Validating oblong completeness...")
        oblong_features = FeatureExtractor._validate_oblong_completeness(
            oblong_features, min_faces=6  # [OK] Require 6 faces for PERFECT/GOOD
        )
        
        # STEP 3: Recalculate bbox 3D từ actual faces (sau deduplication)
        # Mục đích: Gộp tất cả bbox của 6-8 faces lại để có bbox tổng hợp chính xác
        # Bbox 3D = min(min) và max(max) của tất cả faces
        print(f"\n[STEP 3] Recalculating bbox 3D from actual faces...")
        for oblong in oblong_features:
            face_ids = oblong.get('face_ids', [])
            if not face_ids:
                continue
            
            # Build reverse map: face_id -> face hash
            id_to_hash = {v: k for k, v in face_id_map.items()}
            
            # Get all face bboxes (mỗi face có bbox riêng - 2 điểm: min, max)
            face_bboxes = []
            face_centers = []
            for face_id in face_ids:
                face_hash = id_to_hash.get(face_id)
                if not face_hash:
                    continue
                
                for face in shape.Faces:
                    if face.hashCode() == face_hash:
                        face_bboxes.append(face.BoundBox)  # FreeCAD BoundBox (2 điểm)
                        face_centers.append(face.CenterOfMass)
                        break
            
            if face_bboxes:
                # [BBOX 3D] Gộp tất cả bbox của 6-8 faces lại
                # Bbox tổng hợp = min của tất cả min, max của tất cả max
                combined_bbox_3d = {
                    'min': {
                        'x': min(b.XMin for b in face_bboxes),  # Min X của tất cả faces
                        'y': min(b.YMin for b in face_bboxes),  # Min Y của tất cả faces
                        'z': min(b.ZMin for b in face_bboxes)   # Min Z của tất cả faces
                    },
                    'max': {
                        'x': max(b.XMax for b in face_bboxes),  # Max X của tất cả faces
                        'y': max(b.YMax for b in face_bboxes),  # Max Y của tất cả faces
                        'z': max(b.ZMax for b in face_bboxes)   # Max Z của tất cả faces
                    }
                }
                
                # Recalculate center từ actual faces
                avg_center = FreeCAD.Vector(
                    sum(c.x for c in face_centers) / len(face_centers),
                    sum(c.y for c in face_centers) / len(face_centers),
                    sum(c.z for c in face_centers) / len(face_centers)
                )
                
                # So sánh với bbox từ metadata (từ pnt)
                metadata_bbox = oblong.get('bounding_box', {})
                if metadata_bbox:
                    meta_min = metadata_bbox.get('min', {})
                    meta_max = metadata_bbox.get('max', {})
                    
                    x_diff = abs(combined_bbox_3d['min']['x'] - meta_min.get('x', 0)) + \
                             abs(combined_bbox_3d['max']['x'] - meta_max.get('x', 0))
                    y_diff = abs(combined_bbox_3d['min']['y'] - meta_min.get('y', 0)) + \
                             abs(combined_bbox_3d['max']['y'] - meta_max.get('y', 0))
                    z_diff = abs(combined_bbox_3d['min']['z'] - meta_min.get('z', 0)) + \
                             abs(combined_bbox_3d['max']['z'] - meta_max.get('z', 0))
                    
                    print(f"  [BBOX] Oblong {oblong.get('id')}:")
                    print(f"    Metadata bbox: X=[{meta_min.get('x', 0):.1f}, {meta_max.get('x', 0):.1f}], "
                          f"Y=[{meta_min.get('y', 0):.1f}, {meta_max.get('y', 0):.1f}], "
                          f"Z=[{meta_min.get('z', 0):.1f}, {meta_max.get('z', 0):.1f}]")
                    print(f"    Combined from {len(face_bboxes)} faces: "
                          f"X=[{combined_bbox_3d['min']['x']:.1f}, {combined_bbox_3d['max']['x']:.1f}], "
                          f"Y=[{combined_bbox_3d['min']['y']:.1f}, {combined_bbox_3d['max']['y']:.1f}], "
                          f"Z=[{combined_bbox_3d['min']['z']:.1f}, {combined_bbox_3d['max']['z']:.1f}]")
                    
                    if x_diff > 10.0 or y_diff > 10.0 or z_diff > 10.0:
                        print(f"    [WARNING] Bbox mismatch > 10mm! (diff: X={x_diff:.1f}, Y={y_diff:.1f}, Z={z_diff:.1f}mm)")
                    else:
                        print(f"    [OK] Bbox matches metadata (diff: X={x_diff:.1f}, Y={y_diff:.1f}, Z={z_diff:.1f}mm)")
                
                # Sử dụng bbox 3D từ faces (chính xác hơn)
                oblong['bounding_box'] = combined_bbox_3d
                oblong['position'] = {
                    'x': round(avg_center.x, 3),
                    'y': round(avg_center.y, 3),
                    'z': round(avg_center.z, 3)
                }
                print(f"  [OK] Recalculated {oblong.get('id')}: center=({avg_center.x:.1f}, {avg_center.y:.1f}, {avg_center.z:.1f}), "
                      f"bbox 3D from {len(face_bboxes)} faces")
        
        # STEP 4: Log summary
        print(f"\n[STEP 4] Final Summary:")
        FeatureExtractor._log_oblong_summary(oblong_features)
        
        print("="*70 + "\n")
        
        return oblong_features
    
    @staticmethod
    def _bboxes_intersect(bbox1, bbox2: Dict) -> bool:
        """
        Check if FreeCAD BoundBox intersects with metadata bounding box.
        
        Args:
            bbox1: FreeCAD BoundBox object
            bbox2: Dictionary with 'min' and 'max' keys
            
        Returns:
            True if bounding boxes intersect
        """
        min2_x = bbox2['min']['x']
        min2_y = bbox2['min']['y']
        min2_z = bbox2['min']['z']
        max2_x = bbox2['max']['x']
        max2_y = bbox2['max']['y']
        max2_z = bbox2['max']['z']
        
        # Check for intersection in all 3 dimensions
        x_overlap = not (bbox1.XMax < min2_x or bbox1.XMin > max2_x)
        y_overlap = not (bbox1.YMax < min2_y or bbox1.YMin > max2_y)
        z_overlap = not (bbox1.ZMax < min2_z or bbox1.ZMin > max2_z)
        
        return x_overlap and y_overlap and z_overlap

    @staticmethod
    def _add_box_holes_from_metadata(shape, face_id_map: Dict, box_hole_metadata: List[Dict]) -> List[Dict[str, Any]]:
        """
        Add closed square/rectangular hole features from metadata.

        Closed box holes must have four internal vertical planar wall faces.
        Cutters that touch the sheet boundary become open edge cutouts in STEP
        topology and are intentionally skipped here so square_hole/rectangular_hole
        always means a true 4-face hole.
        """
        features = []
        if not box_hole_metadata:
            return features

        shape_bbox = shape.BoundBox
        actual_z_min = shape_bbox.ZMin
        actual_z_max = shape_bbox.ZMax
        thickness = shape_bbox.ZLength
        used_face_ids = set()

        print(f"[BOX_HOLE] Processing {len(box_hole_metadata)} square/rectangular hole metadata entries")

        for idx, meta in enumerate(box_hole_metadata, start=1):
            if meta.get('is_open_cutout', False):
                center = meta.get('center', {})
                print(f"[BOX_HOLE] Skipping open cutout #{idx} at ({center.get('x')}, {center.get('y')}) - not a closed 4-face hole")
                continue

            bbox = meta.get('bounding_box', {})
            if not bbox:
                continue
            bmin = bbox.get('min', {})
            bmax = bbox.get('max', {})
            min_x = bmin.get('x', 0.0)
            max_x = bmax.get('x', 0.0)
            min_y = bmin.get('y', 0.0)
            max_y = bmax.get('y', 0.0)

            z_min = max(bmin.get('z', actual_z_min), actual_z_min)
            z_max = min(bmax.get('z', actual_z_max), actual_z_max)
            if z_max <= z_min:
                z_min = actual_z_min
                z_max = actual_z_max

            width = abs(max_x - min_x)
            length = abs(max_y - min_y)
            hole_type = meta.get('type') or ('square_hole' if abs(width - length) < 0.1 else 'rectangular_hole')

            tolerance = max(0.05, min(width, length, thickness) * 0.08)
            tolerance = min(max(tolerance, 0.05), 1.0)

            matched_faces = []
            matched_face_bounds = []
            x_wall_positions = set()
            y_wall_positions = set()
            is_through_cut = meta.get('is_through_cut', True)

            for i, face in enumerate(shape.Faces):
                if 'Plane' not in face.Surface.TypeId:
                    continue

                face_id = face_id_map.get(face.hashCode(), f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}")
                if face_id in used_face_ids:
                    continue

                fb = face.BoundBox
                if is_through_cut:
                    # Through cutters are often intentionally deeper than the sheet
                    # (for example thickness + 2mm). Match the actual STEP wall
                    # span by overlap, then derive final depth from matched faces.
                    z_match = (
                        fb.ZMax >= z_min - tolerance and
                        fb.ZMin <= z_max + tolerance and
                        max(fb.ZLength, fb.XLength, fb.YLength) > tolerance
                    )
                else:
                    z_match = (
                        abs(fb.ZMin - z_min) <= tolerance and
                        abs(fb.ZMax - z_max) <= tolerance
                    )
                if not z_match:
                    continue

                is_x_wall = (
                    fb.XLength <= tolerance and
                    (abs(fb.XMin - min_x) <= tolerance or abs(fb.XMin - max_x) <= tolerance) and
                    abs(fb.YMin - min_y) <= tolerance and
                    abs(fb.YMax - max_y) <= tolerance
                )
                is_y_wall = (
                    fb.YLength <= tolerance and
                    (abs(fb.YMin - min_y) <= tolerance or abs(fb.YMin - max_y) <= tolerance) and
                    abs(fb.XMin - min_x) <= tolerance and
                    abs(fb.XMax - max_x) <= tolerance
                )

                if is_x_wall:
                    matched_faces.append(face_id)
                    matched_face_bounds.append(fb)
                    x_wall_positions.add(round(fb.XMin, 3))
                elif is_y_wall:
                    matched_faces.append(face_id)
                    matched_face_bounds.append(fb)
                    y_wall_positions.add(round(fb.YMin, 3))

            has_closed_wall_set = (
                len(matched_faces) == 4 and
                len(x_wall_positions) == 2 and
                len(y_wall_positions) == 2
            )
            if not has_closed_wall_set:
                center = meta.get('center', {})
                print(f"[BOX_HOLE] Skipping {hole_type} #{idx} at ({center.get('x')}, {center.get('y')}): matched {len(matched_faces)}/4 wall faces")
                continue

            if is_through_cut and matched_face_bounds:
                z_min = min(fb.ZMin for fb in matched_face_bounds)
                z_max = max(fb.ZMax for fb in matched_face_bounds)

            used_face_ids.update(matched_faces)
            center = {
                'x': round((min_x + max_x) / 2.0, 3),
                'y': round((min_y + max_y) / 2.0, 3),
                'z': round((z_min + z_max) / 2.0, 3)
            }
            feature = {
                'type': hole_type,
                'subtype': 'through' if meta.get('is_through_cut', True) else 'blind',
                'id': meta.get('id', f"{hole_type}_{len(features) + 1:03d}"),
                'width': round(width, 3),
                'length': round(length, 3),
                'depth': round(z_max - z_min, 3),
                'position': center,
                'center': center,
                'corner': {'x': round(min_x, 3), 'y': round(min_y, 3), 'z': round(z_min, 3)},
                'bounding_box': {
                    'min': {'x': round(min_x, 3), 'y': round(min_y, 3), 'z': round(z_min, 3)},
                    'max': {'x': round(max_x, 3), 'y': round(max_y, 3), 'z': round(z_max, 3)}
                },
                'face_ids': matched_faces,
                'face_breakdown': {
                    'planar_walls': 4,
                    'x_walls': 2,
                    'y_walls': 2,
                    'total': 4
                },
                'from_metadata': True,
                'is_open_cutout': False,
                'confidence': 0.95,
                'source': meta.get('source', 'Part.makeBox_cut'),
                'units': meta.get('units', 'mm')
            }
            if hole_type == 'square_hole':
                feature['side'] = round(width, 3)

            features.append(feature)
            print(f"[BOX_HOLE] Added {hole_type} #{idx}: 4 wall faces at ({center['x']}, {center['y']}, {center['z']})")

        return features


# --- Utility Classes ---
class FreeCADUtils:
    """Utility class for FreeCAD operations"""
    @staticmethod
    def import_step_file(step_path: str, doc_name: str = "TempDoc"):
        # NOTE: tried bypassing Import.insert() with a direct Part.Shape.read()
        # of the SAME .step file to skip its colors/link-group bookkeeping.
        # Cross-process timing made it look ~24% faster (334s vs 441s on a
        # 9990-hole sheet), but a same-process A/B test (both paths run back-
        # to-back, same process, same system state) showed NO real difference
        # (~23-27s either way on a smaller file) -- the apparent win was
        # environment/measurement noise, not a real effect. Reverted.
        #
        # A DIFFERENT and genuinely huge lever, verified this session: OCC's
        # native .brep format reads ~120-430x faster than the equivalent
        # .step (0.88s vs 379.57s on the 9990-hole sheet, identical resulting
        # geometry -- same face area to the decimal). STEP is slow because
        # OCC has to re-resolve/translate every entity; .brep is a direct
        # dump of OCC's own in-memory structures, so reading it back is
        # nearly free. This can ONLY help if a sibling .brep already exists
        # next to the .step (e.g. written by whatever process created the
        # shape in the first place, before it ever exported STEP -- for a
        # generated/parametric part this costs that process about 1s extra).
        # It does NOT help for a .step received with no such sibling, since
        # producing the .brep still requires reading the .step once.
        brep_path = os.path.splitext(step_path)[0] + ".brep"
        if os.path.exists(brep_path):
            try:
                doc = FreeCAD.newDocument(doc_name)
                shape = Part.Shape()
                shape.importBrep(brep_path)
                obj = doc.addObject("Part::Feature", "ImportedShape")
                obj.Shape = shape
                doc.recompute()
                print(f"[OK] Imported via sibling .brep (fast path): {brep_path}")
                return doc
            except Exception as e:
                print(f"[WARNING] Sibling .brep import failed ({e}), falling back to STEP")

        try:
            doc = FreeCAD.newDocument(doc_name)
            Import.insert(step_path, doc.Name)
            return doc
        except Exception as e:
            pass
            print(f"[ERROR] Failed to import STEP file: {e}")
            return None

    @staticmethod
    def create_mesh_from_shape(shape, linear_deflection: float = None, angular_deflection: float = None):
        try:
            mesh = MeshPart.meshFromShape(
                Shape=shape,
                LinearDeflection=linear_deflection or FREECAD_MESH_CONFIG["LINEAR_DEFLECTION"],
                AngularDeflection=angular_deflection or FREECAD_MESH_CONFIG["ANGULAR_DEFLECTION"],
                Relative=FREECAD_MESH_CONFIG["RELATIVE"]
            )
            return mesh
        except Exception as e:
            pass
            print(f"[ERROR] Failed to create mesh: {e}")
            return None

    @staticmethod
    def triangulate_planar_face_with_holes(face, hole_centers_radii, segments: int = PERFORATED_HOLE_POLYGON_SEGMENTS):
        """
        Fast path for a planar face whose inner wires are all simple circular holes
        (the top/bottom face of a perforated sheet). OCC's general-purpose
        BRepMesh_IncrementalMesh scales worse than linear with hole count on this
        specific topology (measured ~6.6x slower than this at 4428 holes). Instead,
        build the outer boundary + every hole polygon directly and triangulate the
        whole polygon-with-holes in a single earcut call.

        hole_centers_radii: pre-collected [(FreeCAD.Vector center, radius), ...] for
        ALL small cylindrical hole faces on the shape (computed once per shape, via
        the cheap GeomCylinder.Center/.Radius properties -- NOT by walking this
        face's own inner Wires/Edges/Curves, which is ~1000x slower in practice for
        thousands of holes: measured 12.5s of pure wire traversal overhead on a
        2125-hole face vs <1s for the equivalent cylindrical-face property reads).

        Bails out to None (caller falls back to the proven OCC path) whenever the
        inner-wire count on this face doesn't match the supplied hole count -- this
        is the correctness guard against silently mismatched geometry.

        Returns (points3d, triangles) as (list[(x,y,z)], list[(i,j,k)]), or None.
        """
        if not (HAS_NUMPY and HAS_EARCUT) or not hole_centers_radii:
            return None
        try:
            surf = face.Surface
            if surf.TypeId != 'Part::GeomPlane':
                return None

            outer_wire = face.OuterWire
            inner_wire_count = len(face.Wires) - 1
            if inner_wire_count != len(hole_centers_radii):
                return None  # topology doesn't match what we were given; play safe

            # Project via the plane's OWN native (u,v) parametrization (surf.parameter
            # / surf.value), not a hand-rolled cross-product basis. A hand-rolled
            # basis is mathematically equivalent but was measured to make earcut's
            # hole-bridging ~8x slower (7.5s vs 0.9s on 2125 holes) for reasons tied
            # to the specific offset/rotation it produced -- never fully root-caused,
            # but the native OCC parametrization reliably avoids it and is simpler.
            def to_2d(pt):
                return surf.parameter(pt)

            # Outer boundary: Wire.discretize() walks the wire in true geometric
            # order (unlike wire.Edges, which is NOT guaranteed to be in traversal
            # order -- using it directly produced a self-crossing polygon that made
            # earcut's hole-bridging pathologically slow too). Not a bottleneck
            # either way -- there is exactly one of these per face.
            outer_pts_3d = outer_wire.discretize(Deflection=0.5)
            outer_pts_2d = [to_2d(p) for p in outer_pts_3d]
            if len(outer_pts_2d) < 3:
                return None

            hole_centers_2d = [to_2d(c) for c, r in hole_centers_radii]
            hole_radii = [r for c, r in hole_centers_radii]

            # Batch-construct ALL hole polygons at once via numpy broadcasting --
            # no per-hole Python loop, no per-point OCC curve evaluation.
            centers = np.array(hole_centers_2d, dtype=np.float64)          # (H, 2)
            radii = np.array(hole_radii, dtype=np.float64)                 # (H,)
            angles = np.linspace(0, 2 * np.pi, segments, endpoint=False)   # (N,)
            circle = np.stack([np.cos(angles), np.sin(angles)], axis=-1)   # (N, 2)
            hole_pts = centers[:, None, :] + radii[:, None, None] * circle[None, :, :]
            hole_pts_flat = hole_pts.reshape(-1, 2)

            outer_arr = np.array(outer_pts_2d, dtype=np.float64)
            all_vertices_2d = np.vstack([outer_arr, hole_pts_flat])

            ring_ends = [len(outer_arr)]
            for _ in hole_radii:
                ring_ends.append(ring_ends[-1] + segments)
            rings = np.array(ring_ends, dtype=np.uint32)

            tri_idx = _earcut.triangulate_float64(all_vertices_2d, rings)
            if len(tri_idx) == 0:
                return None

            # Reconstruct 3D points via the plane's own origin/axes (pulled once,
            # not per-point) instead of calling surf.value() per vertex -- avoids
            # ~68000 individual OCC calls for large hole counts.
            position = surf.Position
            u_dir = surf.Rotation.multVec(FreeCAD.Vector(1, 0, 0))
            v_dir = surf.Rotation.multVec(FreeCAD.Vector(0, 1, 0))
            pos_arr = np.array([position.x, position.y, position.z])
            u_arr = np.array([u_dir.x, u_dir.y, u_dir.z])
            v_arr = np.array([v_dir.x, v_dir.y, v_dir.z])
            pts3d_np = pos_arr + all_vertices_2d[:, 0:1] * u_arr + all_vertices_2d[:, 1:2] * v_arr
            points3d = [(float(x), float(y), float(z)) for x, y, z in pts3d_np]
            triangles = [tuple(int(x) for x in tri_idx[i:i + 3]) for i in range(0, len(tri_idx), 3)]
            return points3d, triangles
        except Exception:
            return None

    _cdt_python_path = None

    @staticmethod
    def _find_cdt_python() -> Optional[str]:
        """Locate a plain python3 interpreter (with numpy+triangle) to run
        cdt_worker.py in -- NOT sys.executable, which inside freecadcmd
        resolves to the freecadcmd binary itself, not a usable interpreter."""
        if FreeCADUtils._cdt_python_path is not None:
            return FreeCADUtils._cdt_python_path or None
        candidates = ["/opt/conda/bin/python3", "/opt/conda/bin/python", "python3"]
        for candidate in candidates:
            try:
                r = subprocess.run([candidate, "-c", "import triangle, numpy"],
                                    capture_output=True, timeout=10)
                if r.returncode == 0:
                    FreeCADUtils._cdt_python_path = candidate
                    return candidate
            except Exception:
                continue
        FreeCADUtils._cdt_python_path = ""
        return None

    @staticmethod
    def _run_cdt_subprocess(all_vertices_2d, all_segments, centers):
        """Run triangle.triangulate() in a separate subprocess (cdt_worker.py)
        so a segfault there can't take down the calling freecadcmd process.
        Returns (tri_idx, out_verts) as numpy arrays, or (None, None) on any
        failure (missing interpreter, subprocess crash/timeout, no result)."""
        python_path = FreeCADUtils._find_cdt_python()
        if not python_path:
            return None, None
        worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdt_worker.py")
        if not os.path.exists(worker_path):
            return None, None

        tmp_dir = tempfile.mkdtemp(prefix="cdt_")
        input_path = os.path.join(tmp_dir, "in.npz")
        output_path = os.path.join(tmp_dir, "out.npz")
        try:
            np.savez(input_path, vertices=all_vertices_2d, segments=all_segments, holes=centers)
            result = subprocess.run(
                [python_path, worker_path, input_path, output_path],
                capture_output=True, timeout=60
            )
            if result.returncode != 0 or not os.path.exists(output_path):
                return None, None
            out = np.load(output_path)
            return out["triangles"], out["out_verts"]
        except Exception:
            return None, None
        finally:
            for p in (input_path, output_path):
                try:
                    os.remove(p)
                except Exception:
                    pass
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass

    @staticmethod
    def triangulate_planar_face_with_holes_cdt(face, hole_centers_radii, segments: int = PERFORATED_HOLE_POLYGON_SEGMENTS):
        """
        Same fast path as triangulate_planar_face_with_holes, but using the
        `triangle` package (Shewchuk's constrained Delaunay triangulation,
        Triangle.c) instead of mapbox_earcut for the actual triangulation call.

        earcut's ear-clipping hole-bridging was found to genuinely fail at high
        hole counts (thousands of spurious triangles on a 2125-hole sheet,
        verified via hole-center-containment checks). `triangle` is a much more
        mature, widely-used CDT implementation (standard in FEM/CFD meshing)
        purpose-built for exactly this "polygon with many holes" case. Verified
        on real extracted data: 2125 holes -> 0.15s with 0/131 flagged
        triangles containing a hole center (all legitimate hole-free margin
        coverage); 4428 holes -> 0.34s with 0/191. Both ~100x+ faster than
        OCC's BRepMesh_IncrementalMesh on the same face.

        Returns (points3d, triangles) as (list[(x,y,z)], list[(i,j,k)]), or
        None if triangle isn't available, topology doesn't match, or the
        result fails the area sanity check (see below) -- caller falls back
        to the proven OCC MeshPart path in all of these cases.
        """
        if not (HAS_NUMPY and HAS_TRIANGLE) or not hole_centers_radii:
            return None
        try:
            surf = face.Surface
            if surf.TypeId != 'Part::GeomPlane':
                return None

            outer_wire = face.OuterWire
            inner_wire_count = len(face.Wires) - 1
            if inner_wire_count != len(hole_centers_radii):
                return None  # topology doesn't match what we were given; play safe

            def to_2d(pt):
                return surf.parameter(pt)

            outer_pts_3d = outer_wire.discretize(Deflection=0.5)
            outer_pts_2d = [to_2d(p) for p in outer_pts_3d]
            if len(outer_pts_2d) < 3:
                return None

            hole_centers_2d = [to_2d(c) for c, r in hole_centers_radii]
            hole_radii = [r for c, r in hole_centers_radii]

            centers = np.array(hole_centers_2d, dtype=np.float64)          # (H, 2)
            radii = np.array(hole_radii, dtype=np.float64)                 # (H,)
            angles = np.linspace(0, 2 * np.pi, segments, endpoint=False)   # (N,)
            circle = np.stack([np.cos(angles), np.sin(angles)], axis=-1)   # (N, 2)
            hole_pts = centers[:, None, :] + radii[:, None, None] * circle[None, :, :]
            hole_pts_flat = hole_pts.reshape(-1, 2)                        # (H*N, 2)

            outer_arr = np.array(outer_pts_2d, dtype=np.float64)
            n_outer = len(outer_arr)
            all_vertices_2d = np.vstack([outer_arr, hole_pts_flat])

            # PSLG segments: consecutive-point boundary edges for the outer
            # ring, then for each hole ring -- vectorized, no per-hole Python loop.
            outer_seg = np.stack([np.arange(n_outer), (np.arange(n_outer) + 1) % n_outer], axis=1)
            n_holes = len(hole_radii)
            hole_idx = np.arange(segments)
            hole_seg_local = np.stack([hole_idx, (hole_idx + 1) % segments], axis=1)  # (N, 2)
            hole_offsets = n_outer + np.arange(n_holes)[:, None] * segments             # (H, 1)
            hole_segs = (hole_seg_local[None, :, :] + hole_offsets[:, :, None]).reshape(-1, 2)
            all_segments = np.vstack([outer_seg, hole_segs]).astype(np.int32)

            # Run the actual triangle.triangulate() call in a SEPARATE
            # subprocess (cdt_worker.py). Verified this session: `triangle`'s
            # C code can segfault on PSLG input that was independently
            # confirmed valid (clean, no duplicate points, correct hole
            # positions) -- a heap-corruption-sensitive bug in the library
            # itself, not something a Python-side data check can catch. A
            # SIGSEGV can't be caught by try/except, so isolating the call
            # means a crash there just fails this helper process; we fall
            # back to the proven OCC MeshPart path below, same as any other
            # CDT failure mode.
            tri_idx, out_verts = FreeCADUtils._run_cdt_subprocess(all_vertices_2d, all_segments, centers)
            if tri_idx is None or len(tri_idx) == 0 or out_verts is None:
                return None
            # `triangle` with the bare 'p' switch (no 'q'/'a' refinement) does
            # not introduce Steiner points -- if it ever did on some edge case,
            # our input-vertex-indexed 3D reconstruction below would silently
            # produce wrong coordinates. Guard against that explicitly.
            if len(out_verts) != len(all_vertices_2d):
                return None

            # Area sanity check: mesh area should equal outer polygon area
            # minus hole areas, within discretization tolerance (~2-3% is
            # normal from approximating circles with `segments`-gons).
            a = out_verts[tri_idx[:, 0]]
            b = out_verts[tri_idx[:, 1]]
            c = out_verts[tri_idx[:, 2]]
            mesh_area = 0.5 * np.abs(
                (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
            ).sum()

            def polygon_area(pts):
                x, y = pts[:, 0], pts[:, 1]
                return 0.5 * abs(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
            outer_area = polygon_area(outer_arr)
            holes_area = float(np.sum(np.pi * radii ** 2))
            expected_area = outer_area - holes_area
            if expected_area <= 0 or abs(mesh_area - expected_area) / expected_area > 0.05:
                return None  # something's off -- play safe, fall back to OCC

            position = surf.Position
            u_dir = surf.Rotation.multVec(FreeCAD.Vector(1, 0, 0))
            v_dir = surf.Rotation.multVec(FreeCAD.Vector(0, 1, 0))
            pos_arr = np.array([position.x, position.y, position.z])
            u_arr = np.array([u_dir.x, u_dir.y, u_dir.z])
            v_arr = np.array([v_dir.x, v_dir.y, v_dir.z])
            pts3d_np = pos_arr + all_vertices_2d[:, 0:1] * u_arr + all_vertices_2d[:, 1:2] * v_arr
            points3d = [(float(x), float(y), float(z)) for x, y, z in pts3d_np]
            triangles = [tuple(int(x) for x in tri) for tri in tri_idx]
            return points3d, triangles
        except Exception:
            return None

    @staticmethod
    def count_small_cylindrical_faces(shape, radius_threshold: float = PERFORATED_SMALL_HOLE_RADIUS_MM) -> int:
        count = 0
        for face in shape.Faces:
            try:
                surf = face.Surface
                if surf.TypeId == 'Part::GeomCylinder' and surf.Radius <= radius_threshold:
                    count += 1
            except Exception:
                continue
        return count

    @staticmethod
    def collect_small_cylindrical_hole_centers(shape, radius_threshold: float = PERFORATED_SMALL_HOLE_RADIUS_MM):
        """(center, radius) for every small cylindrical hole face, read via the
        cheap GeomCylinder properties. Feeds triangulate_planar_face_with_holes."""
        holes = []
        for face in shape.Faces:
            try:
                surf = face.Surface
                if surf.TypeId == 'Part::GeomCylinder' and surf.Radius <= radius_threshold:
                    holes.append((surf.Center, surf.Radius))
            except Exception:
                continue
        return holes

    @staticmethod
    def extract_individual_faces(shape) -> List:
        try:
            return list(shape.Faces)
        except Exception as e:
            pass
            print(f"[ERROR] Failed to extract individual faces: {e}")
            return []

    @staticmethod
    def extract_edges_data(shape, edge_id_prefix: str = "Edge") -> List[Dict[str, Any]]:
        edges_data = []
        try:
            n_points = EDGE_PROCESSING_CONFIG["POINTS_PER_EDGE"] + 1
            for i, edge in enumerate(shape.Edges):
                vertices = []
                try:
                    # edge.discretize() samples the whole edge in a single
                    # C++ call (same uniform-parameter distribution as the
                    # manual per-point curve.value() loop this replaces),
                    # instead of POINTS_PER_EDGE separate Python<->C++
                    # round-trips per edge. Matters at scale: a 4428-hole
                    # perforated sheet has ~4428 hole-boundary edges alone.
                    points = edge.discretize(Number=n_points)
                    vertices = [{
                        "btType": "BTVector3d-389",
                        "x": float(p.x), "y": float(p.y), "z": float(p.z)
                    } for p in points]
                except Exception:
                    start_point = edge.firstVertex().Point
                    end_point = edge.lastVertex().Point
                    vertices = [
                        {"btType": "BTVector3d-389", "x": float(start_point.x), "y": float(start_point.y), "z": float(start_point.z)},
                        {"btType": "BTVector3d-389", "x": float(end_point.x), "y": float(end_point.y), "z": float(end_point.z)}
                    ]
                
                edges_data.append({
                    "btType": "BTExportTessellatedEdgesEdge-1364",
                    "id": f"{edge_id_prefix}_{i}",
                    "vertices": vertices
                })
            return edges_data
        except Exception as e:
            pass
            print(f"[ERROR] Failed to extract edges data: {e}")
            return []

    @staticmethod
    def cleanup_document(doc):
        if doc:
            try:
                FreeCAD.closeDocument(doc.Name)
            except Exception as e:
                pass
                print(f"[WARNING] Failed to cleanup document: {e}")

    @staticmethod
    def get_shape_objects(doc) -> List:
        try:
            return [obj for obj in doc.Objects if hasattr(obj, 'Shape') and not obj.Shape.isNull()]
        except Exception as e:
            pass
            print(f"[ERROR] Failed to get shape objects: {e}")
            return []


class OnShapeJSONConverter:
    """Converter for OnShape JSON format with feature extraction and threaded holes metadata support"""

    @staticmethod
    def _build_facet_face(face_id, pts, tris, attr_style: bool):
        """Original non-indexed encoding: every triangle repeats its own 3 full
        vertex objects. Used for all non-perforated shapes (unchanged behavior).

        JSON output is byte-for-byte the same shape as before (still one full
        {btType,x,y,z} object per triangle corner) -- only how it's *built* in
        Python changed: each unique point is rounded and wrapped in a dict
        exactly ONCE (vectorized via numpy) and that same dict object is then
        referenced by every triangle corner that uses it, instead of
        re-rounding + re-allocating a fresh dict per (triangle, corner)
        occurrence. Meshes reuse points heavily (a planar CDT mesh has roughly
        2 triangles per interior point), so this cuts the Python-side
        round()+dict-alloc work by roughly that reuse factor.
        """
        if HAS_NUMPY:
            if attr_style:
                arr = np.fromiter(
                    (c for p in pts for c in (p.x, p.y, p.z)),
                    dtype=np.float64, count=len(pts) * 3
                ).reshape(-1, 3)
            else:
                arr = np.asarray(pts, dtype=np.float64)
            arr = np.round(arr, 4)
            vertex_objs = [
                {"btType": "BTVector3d-389", "x": x, "y": y, "z": z}
                for x, y, z in arr.tolist()
            ]
        else:
            if attr_style:
                get = lambda p: (round(p.x, 4), round(p.y, 4), round(p.z, 4))
            else:
                get = lambda p: (round(p[0], 4), round(p[1], 4), round(p[2], 4))
            vertex_objs = [
                {"btType": "BTVector3d-389", "x": x, "y": y, "z": z}
                for (x, y, z) in (get(p) for p in pts)
            ]

        facets = [
            {
                "btType": "BTExportTessellatedFacesFacet-1417",
                "vertices": [vertex_objs[vi] for vi in tri],
                "indices": [],
                "normals": [],
                "textureCoordinates": []
            }
            for tri in tris
        ]
        return {"btType": "BTExportTessellatedFacesFace-1192", "id": face_id, "facets": facets}

    @staticmethod
    def _build_indexed_face(face_id, pts, tris, attr_style: bool):
        """Indexed encoding: a shared FLAT vertex array [x,y,z,x,y,z,...] + a
        flat triangle-index array, instead of duplicating full vertex objects
        3x per triangle (the non-indexed encoding) or even repeating a
        {"btType":..., "x":, "y":, "z":} object per vertex (the first indexed
        encoding tried this session). That per-vertex object boilerplate
        ("btType": "BTVector3d-389" plus 3 key names) is identical on every
        vertex and, for a perforated sheet with tens of thousands of shared
        vertices, was measured to cost far more bytes than the actual
        coordinate data. A flat number array carries the same 3 numbers per
        vertex with none of that repetition. "vertexFormat": "flat" marks this
        so json_viewer.js can distinguish it from the older object-array
        encoding (still emitted nowhere now, but kept parseable for any
        already-delivered files). Only used for is_perforated shapes.
        """
        if attr_style:
            vertices = [round(v, 4) for p in pts for v in (p.x, p.y, p.z)]
        else:
            vertices = [round(v, 4) for p in pts for v in (p[0], p[1], p[2])]
        indices = [int(vi) for tri in tris for vi in tri]
        return {
            "btType": "BTExportTessellatedFacesFace-1192",
            "id": face_id,
            "vertexFormat": "flat",
            "vertices": vertices,
            "indices": indices,
        }

    def convert(self, step_path: str, json_path: str, metadata_path: Optional[str] = None) -> bool:
        if not os.path.exists(step_path):
            print(f"[ERROR] STEP file not found: {step_path}")
            return False

        # Coarse phase timing -- printed at the end of convert(). Lets us see
        # from real pipeline logs (not isolated benchmarks) exactly which
        # phase dominates wall time, instead of guessing.
        _t_start = time.perf_counter()
        _timing = {"import": 0.0, "cached_tessellate": 0.0, "cdt": 0.0,
                   "per_face_fallback": 0.0, "facet_build": 0.0,
                   "edges": 0.0, "features": 0.0, "json_write": 0.0}
        _counts = {"cached_tessellate": 0, "cdt": 0, "per_face_fallback": 0}

        doc = FreeCADUtils.import_step_file(step_path)
        _timing["import"] = time.perf_counter() - _t_start
        if not doc:
            return False

        # Load feature metadata if provided (threaded holes + oblongs + bending + box holes + shape_type)
        feature_metadata = {}
        if metadata_path:
            feature_metadata = self._load_metadata(metadata_path)
            threaded_count = len(feature_metadata.get('threaded_holes', []))
            oblong_count = len(feature_metadata.get('oblongs', []))
            bending_count = len(feature_metadata.get('bending_features', []))
            box_hole_count = len(feature_metadata.get('box_holes', []))
            shape_type = feature_metadata.get('shape_type', None)  # 🔥 NEW: Check shape_type
            
            # 🔥 UPDATED: Create metadata if ANY of these exist (features OR shape_type)
            if threaded_count > 0 or oblong_count > 0 or bending_count > 0 or box_hole_count > 0 or shape_type:
                if shape_type:
                    print(f"[OK] Loaded metadata: shape_type={shape_type}, {threaded_count} threaded hole(s), {oblong_count} oblong(s), {bending_count} bending feature(s), {box_hole_count} box hole(s)")
                else:
                    print(f"[OK] Loaded metadata: {threaded_count} threaded hole(s), {oblong_count} oblong(s), {bending_count} bending feature(s), {box_hole_count} box hole(s)")
            else:
                print(f"[WARNING] Metadata file provided but no features or shape_type found")

        try:
            shape_objects = FreeCADUtils.get_shape_objects(doc)
            if not shape_objects:
                print(f"[ERROR] No shape objects found in STEP file")
                return False
            print(f"[OK] Found {len(shape_objects)} shape objects")

            all_faces = []
            all_edges = []
            all_features = []
            combined_bbox = None
            any_perforated = False

            for obj in shape_objects:
                shape = obj.Shape

                # Update combined bounding box
                if combined_bbox is None:
                    combined_bbox = shape.BoundBox
                else:
                    combined_bbox.add(shape.BoundBox)

                # Build face ID map for feature extraction
                face_id_map = {}
                individual_faces = FreeCADUtils.extract_individual_faces(shape)

                # Shapes with many small holes (e.g. perforated sheets) get a coarser
                # mesh: the default LINEAR_DEFLECTION is absolute, so small holes are
                # heavily over-tessellated relative to their size.
                small_hole_data = FreeCADUtils.collect_small_cylindrical_hole_centers(shape)
                small_hole_count = len(small_hole_data)
                is_perforated = small_hole_count >= PERFORATED_HOLE_COUNT_THRESHOLD
                any_perforated = any_perforated or is_perforated
                mesh_linear_deflection = PERFORATED_MESH_CONFIG["LINEAR_DEFLECTION"] if is_perforated else None
                mesh_angular_deflection = PERFORATED_MESH_CONFIG["ANGULAR_DEFLECTION"] if is_perforated else None
                active_linear_deflection = mesh_linear_deflection or FREECAD_MESH_CONFIG["LINEAR_DEFLECTION"]
                if is_perforated:
                    print(f"[OK] {obj.Name}: {small_hole_count} small holes detected -> using coarser mesh deflection")
                    # Cache a .brep sibling next to the .step so any future
                    # re-conversion of this exact file hits FreeCADUtils.
                    # import_step_file's fast path (verified ~120-430x faster
                    # import, identical geometry). Scoped to perforated shapes
                    # only, since that's where the slow STEP import + this
                    # session's other perforated-specific changes already
                    # live; doesn't touch behavior for any other shape type.
                    # Skipped if a .brep is already there (e.g. one the
                    # upstream generator provided itself).
                    brep_cache_path = os.path.splitext(step_path)[0] + ".brep"
                    if not os.path.exists(brep_cache_path):
                        try:
                            shape.exportBrep(brep_cache_path)
                            print(f"[OK] Cached .brep for faster re-conversion next time: {brep_cache_path}")
                        except Exception as e:
                            print(f"[WARNING] Failed to write .brep cache: {e}")

                # ⚡ Mesh the WHOLE shape once via BRepMesh_IncrementalMesh (through
                # Shape.tessellate). OCC caches the triangulation on each TopoDS_Face,
                # so the per-face Face.tessellate() calls below just retrieve the cached
                # result instead of re-triangulating shared edges/faces from scratch.
                # ~6x faster than calling MeshPart.meshFromShape once per face -- BUT
                # measured to blow up in time/memory (>1h, >8GB RAM) on shapes with
                # thousands of faces (e.g. 4434-face perforated sheet), likely due to
                # non-linear vertex-welding cost in OCC's whole-shape incremental mesh.
                # Only use it below a safe face-count threshold; large shapes fall back
                # to the proven per-face MeshPart path below.
                use_cached_tessellation = False
                if len(individual_faces) <= WHOLE_SHAPE_TESSELLATE_MAX_FACES:
                    try:
                        shape.tessellate(active_linear_deflection)
                        use_cached_tessellation = True
                    except Exception:
                        use_cached_tessellation = False

                for i, face_shape in enumerate(individual_faces):
                    face_id = f"Jf{chr(65 + (i % 26))}{'' if i < 26 else str(i // 26)}"
                    face_id_map[face_shape.hashCode()] = face_id

                    pts = tris = None
                    if use_cached_tessellation:
                        _t0 = time.perf_counter()
                        try:
                            pts, tris = face_shape.tessellate(active_linear_deflection)
                        except Exception:
                            pts = tris = None
                        _timing["cached_tessellate"] += time.perf_counter() - _t0

                    if pts is not None:
                        # NOTE: was _build_indexed_face() for is_perforated (flat vertex
                        # array + vertexFormat:"flat") -- reverted to always emitting the
                        # legacy per-facet object format. The speed win (CDT + .brep cache
                        # + coarser deflection, all below) is independent of output
                        # encoding; keeping the legacy format avoids touching
                        # json_viewer.js/enrich_features.py or any downstream consumer.
                        _t0 = time.perf_counter()
                        all_faces.append(OnShapeJSONConverter._build_facet_face(face_id, pts, tris, attr_style=True))
                        _timing["facet_build"] += time.perf_counter() - _t0
                        _counts["cached_tessellate"] += 1
                        continue

                    # ⚡ CDT fast path for perforated-plate faces: `triangle`
                    # (Shewchuk CDT) instead of the earlier-tried earcut, which
                    # genuinely produced bad triangles at scale. `triangle` was
                    # verified correct (0 real defects out of every flagged
                    # "long" triangle, checked via hole-center-containment) at
                    # 2125 and 4428 holes, and is ~100x+ faster than OCC here.
                    # triangulate_planar_face_with_holes_cdt self-guards on
                    # topology mismatch AND an area sanity check; both fall
                    # through to the proven OCC path below on any doubt.
                    if is_perforated and small_hole_data:
                        _t0 = time.perf_counter()
                        cdt_result = FreeCADUtils.triangulate_planar_face_with_holes_cdt(face_shape, small_hole_data)
                        _timing["cdt"] += time.perf_counter() - _t0
                        if cdt_result is not None:
                            pts, tris = cdt_result
                            # Legacy per-facet format -- see NOTE above.
                            _t0 = time.perf_counter()
                            all_faces.append(OnShapeJSONConverter._build_facet_face(face_id, pts, tris, attr_style=False))
                            _timing["facet_build"] += time.perf_counter() - _t0
                            _counts["cdt"] += 1
                            continue

                    # Fallback: per-face MeshPart.meshFromShape (only used if the
                    # shared tessellation cache above wasn't available). This is
                    # the suspected hot spot for shapes with thousands of small
                    # non-planar faces (e.g. every cylindrical hole side-wall on
                    # a perforated sheet) -- each call is a separate C++
                    # round-trip through FreeCAD's incremental mesher.
                    _t0 = time.perf_counter()
                    mesh = FreeCADUtils.create_mesh_from_shape(
                        face_shape,
                        linear_deflection=mesh_linear_deflection,
                        angular_deflection=mesh_angular_deflection
                    )
                    _timing["per_face_fallback"] += time.perf_counter() - _t0
                    _counts["per_face_fallback"] += 1
                    if not mesh:
                        continue

                    # ⚡ FAST PATH: mesh.Topology returns ALL points + triangles
                    # in a SINGLE C++ call as Python tuples.
                    try:
                        pts, tris = mesh.Topology  # 1 C++ round-trip for everything
                        # Legacy per-facet format -- see NOTE above.
                        _t0 = time.perf_counter()
                        all_faces.append(OnShapeJSONConverter._build_facet_face(face_id, pts, tris, attr_style=False))
                        _timing["facet_build"] += time.perf_counter() - _t0
                        continue
                    except Exception:
                        # Fallback: original loop nếu mesh.Topology không khả dụng
                        facets = []
                        for facet in mesh.Facets:
                            vertices = [{
                                "btType": "BTVector3d-389",
                                "x": p.x, "y": p.y, "z": p.z
                            } for p in (mesh.Points[idx] for idx in facet.PointIndices)]
                            facets.append({
                                "btType": "BTExportTessellatedFacesFacet-1417",
                                "vertices": vertices,
                                "indices": [],
                                "normals": [],
                                "textureCoordinates": []
                            })

                    all_faces.append({
                        "btType": "BTExportTessellatedFacesFace-1192",
                        "id": face_id,
                        "facets": facets
                    })
                
                # Extract edges
                _t0 = time.perf_counter()
                all_edges.extend(FreeCADUtils.extract_edges_data(shape, obj.Name))
                _timing["edges"] += time.perf_counter() - _t0
                
                # Extract semantic features with metadata (threaded + oblongs)
                _t0 = time.perf_counter()
                try:
                    print(f"[DEBUG] Extracting features for {obj.Name}...")
                    print(f"[DEBUG] Face ID map size: {len(face_id_map)}")
                    print(f"[DEBUG] Metadata oblongs: {len(feature_metadata.get('oblongs', []))}")
                    print(f"[DEBUG] Metadata box holes: {len(feature_metadata.get('box_holes', []))}")
                    features = FeatureExtractor.extract_features(shape, face_id_map, feature_metadata)
                    print(f"[DEBUG] Extracted {len(features)} features")
                    all_features.extend(features)
                    _timing["features"] += time.perf_counter() - _t0
                except Exception as e:
                    _timing["features"] += time.perf_counter() - _t0
                    import traceback
                    print(f"[ERROR] Feature extraction failed for {obj.Name}: {e}")
                    traceback.print_exc()
            
            # 🔥 CRITICAL: Remove duplicate features caused by oblong faces
            # Oblong cylindrical faces are often detected as individual holes
            # We need to remove these hole/slot features if they're part of an oblong
            all_features = self._deduplicate_oblong_features(all_features)

            # Build metadata
            metadata = {}
            if combined_bbox:
                metadata = {
                    "bounding_box": {
                        "min": {
                            "x": round(combined_bbox.XMin, 3),
                            "y": round(combined_bbox.YMin, 3),
                            "z": round(combined_bbox.ZMin, 3)
                        },
                        "max": {
                            "x": round(combined_bbox.XMax, 3),
                            "y": round(combined_bbox.YMax, 3),
                            "z": round(combined_bbox.ZMax, 3)
                        }
                    },
                    "dimensions": {
                        "length": round(combined_bbox.XLength, 3),
                        "width": round(combined_bbox.YLength, 3),
                        "height": round(combined_bbox.ZLength, 3)
                    }
                }

            # Load geometry data from geometry.json file (created by script.py)
            geometry_data = self._load_geometry_json(step_path)
            
            # Extract area and volume/mass information from geometry data
            geometry_info = {}
            if geometry_data:
                # Extract surface area (diện tích)
                geometry_info["surface_area"] = {
                    "mm2": geometry_data.get("surface_area_mm2", 0),
                    "cm2": geometry_data.get("surface_area_cm2", 0)
                }
                
                # Extract volume (thể tích)
                geometry_info["volume"] = {
                    "mm3": geometry_data.get("volume_mm3", 0),
                    "cm3": geometry_data.get("volume_cm3", 0)
                }
                
                # Extract mass (khối lượng)
                geometry_info["mass"] = {
                    "grams": geometry_data.get("mass_grams", 0),
                    "kg": geometry_data.get("mass_kg", 0)
                }
                
                # Additional geometry information
                if "material" in geometry_data:
                    geometry_info["material"] = geometry_data.get("material")
                if "density_g_per_mm3" in geometry_data:
                    geometry_info["density"] = geometry_data.get("density_g_per_mm3")
                if "face_count" in geometry_data:
                    geometry_info["face_count"] = geometry_data.get("face_count")
                
                print(f"[OK] Geometry info added: Area={geometry_info['surface_area']['cm2']:.2f}cm², Volume={geometry_info['volume']['cm3']:.2f}cm³, Mass={geometry_info['mass']['grams']:.2f}g")
            else:
                print(f"[WARNING] No geometry data available - area and volume will not be included")

            # Build final JSON structure
            json_data = {
                "faces": {
                    "btType": "BTExportTessellatedFacesResponse-898",
                    "bodies": [{
                        "btType": "BTExportTessellatedFacesBody-1321",
                        "facetPoints": [],
                        "faces": all_faces
                    }]
                },
                "edges": {
                    "btType": "BTExportTessellatedEdgesResponse-327",
                    "bodies": [{
                        "btType": "BTExportTessellatedEdgesBody-1324",
                        "edges": all_edges
                    }] if all_edges else []
                },
                "shape_type": feature_metadata.get('shape_type', None),  # 🔥 NEW: Shape type for ViewCube
                "features": all_features,
                "metadata": metadata,
                "geometry": geometry_info if geometry_info else {}
            }

            # Write JSON output atomically to prevent partial writes.
            # Perforated sheets have hundreds of thousands of small vertex/index
            # objects -- pretty-print indentation on that many objects nearly
            # triples file size for zero benefit (nobody reads this file by eye).
            # Compact output only for perforated sheets; other shapes keep the
            # existing indented format unchanged.
            write_indent = 0 if any_perforated else 2
            if any_perforated:
                print(f"[OK] Perforated sheet detected -> writing compact JSON (no indent) to reduce file size")
            _t0 = time.perf_counter()
            if HAS_FILE_UTILS:
                # Use atomic write (recommended)
                success, error = safe_write_json_atomic(
                    json_path,
                    json_data,
                    indent=write_indent,
                    ensure_ascii=False
                )

                if not success:
                    print(f"[ERROR] Failed to write JSON atomically: {error}")
                    return False

                print(f"[OK] OnShape JSON exported atomically to: {json_path}")
            else:
                # Fallback to standard write (not atomic)
                print(f"[WARNING] Using non-atomic JSON write (file_utils not available)")
                with open(json_path, 'w') as f:
                    json.dump(json_data, f, indent=(write_indent or None))
                print(f"[OK] OnShape JSON exported to: {json_path}")
            _timing["json_write"] = time.perf_counter() - _t0

            print(f"[OK] Total features extracted: {len(all_features)}")
            _timing["total"] = time.perf_counter() - _t_start
            print(
                "[TIMING] " + " | ".join(f"{k}={v:.2f}s" for k, v in _timing.items())
                + " | counts: " + " | ".join(f"{k}={v}" for k, v in _counts.items())
            )
            return True

        except Exception as e:
            pass
            print(f"[ERROR] OnShape JSON conversion failed: {e}")
            traceback.print_exc()
            return False
        finally:
            FreeCADUtils.cleanup_document(doc)

    def _deduplicate_oblong_features(self, features: List[Dict]) -> List[Dict]:
        """
        Remove duplicate hole/slot features that are actually part of oblongs.
        
        Oblong cylindrical faces are often detected as individual holes.
        This method removes these conflicting features to avoid confusion.
        
        Args:
            features: List of all extracted features
            
        Returns:
            Deduplicated list of features
        """
        # Find all oblong features and their face IDs
        oblong_face_ids = set()
        oblongs = []
        
        for feature in features:
            if feature.get('type') == 'oblong':
                oblongs.append(feature)
                face_ids = feature.get('face_ids', [])
                oblong_face_ids.update(face_ids)
        
        if not oblongs:
            # No oblongs, no deduplication needed
            return features
        
        print(f"[INFO] Deduplicating features: Found {len(oblongs)} oblong(s) with {len(oblong_face_ids)} faces")
        
        # Remove hole/slot features that share faces with oblongs
        deduplicated = []
        removed_count = 0
        
        for feature in features:
            feature_type = feature.get('type', '')
            face_ids = set(feature.get('face_ids', []))
            
            # Keep oblongs and non-conflicting features
            if feature_type == 'oblong':
                deduplicated.append(feature)
            elif feature_type in ['hole', 'slot']:
                # Check if this hole/slot shares faces with any oblong
                if face_ids.intersection(oblong_face_ids):
                    removed_count += 1
                else:
                    deduplicated.append(feature)
            else:
                # Keep all other features (threaded_hole, countersink, fillet, etc.)
                deduplicated.append(feature)
        
        if removed_count > 0:
            print(f"[OK] Removed {removed_count} duplicate hole/slot feature(s) that are part of oblongs")
        
        return deduplicated

    def _load_metadata(self, metadata_path: str) -> Dict:
        """
        Load feature metadata from JSON file (threaded holes + oblongs).
        
        Args:
            metadata_path: Path to metadata JSON file
            
        Returns:
            Dictionary with feature metadata:
            {
                'threaded': {'3d': {...}, 'xy': {...}},
                'oblongs': [...]
            }
        """
        try:
            if not os.path.exists(metadata_path):
                print(f"[WARNING] Metadata file not found: {metadata_path}")
                return {'threaded': {}, 'oblongs': []}
            
            with open(metadata_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            return self._build_metadata_lookup(metadata)
            
        except Exception as e:
            pass
            print(f"[WARNING] Failed to load metadata: {e}")
            return {'threaded': {}, 'oblongs': []}
    
    def _build_metadata_lookup(self, metadata: Dict) -> Dict:
        """
        Build metadata in format expected by FeatureExtractor.extract_features.
        
        Args:
            metadata: Metadata dictionary with 'threaded_holes', 'oblongs', 'bending_features' arrays
            
        Returns:
            Dictionary with:
            {
                'shape_type': str | None,
                'threaded_holes': [...],  # Array format (not lookup!)
                'oblongs': [...],
                'bending_features': [...]
            }
        """
        # [OK] FIX: Return metadata in the format expected by FeatureExtractor
        # It expects arrays, not lookup dictionaries!
        return {
            'shape_type': metadata.get('shape_type', None),  # 🔥 NEW: Preserve shape_type
            'threaded_holes': metadata.get('threaded_holes', []),
            'oblongs': metadata.get('oblongs', []),
            'bending_features': metadata.get('bending_features', []),
            'box_holes': metadata.get('box_holes', [])
        }
    
    def _load_geometry_json(self, step_path: str) -> Optional[Dict]:
        """
        Load geometry data from geometry.json file created by script.py.
        
        The geometry file is expected to be in the same directory as the STEP file,
        with naming pattern: {base_name}_geometry.json
        
        Args:
            step_path: Path to STEP file (e.g., /app/storage/user_123/output/user_123.step)
            
        Returns:
            Dictionary with geometry data (volume, surface_area, mass, etc.) or None if not found
        """
        try:
            # Get directory and base filename
            step_file = Path(step_path)
            step_dir = step_file.parent
            base_name = step_file.stem  # e.g., "user_123" from "user_123.step"
            
            # Look for geometry.json file
            geometry_filename = f"{base_name}_geometry.json"
            geometry_path = step_dir / geometry_filename
            
            # DEBUG: Print detailed information
            print(f"[DEBUG] Looking for geometry file:")
            print(f"  - STEP path: {step_path}")
            print(f"  - STEP directory: {step_dir}")
            print(f"  - STEP base name: {base_name}")
            print(f"  - Expected geometry file: {geometry_filename}")
            print(f"  - Full geometry path: {geometry_path}")
            print(f"  - Geometry path exists: {geometry_path.exists()}")
            
            # Also try alternative naming patterns (in case of naming inconsistencies)
            alternative_patterns = [
                geometry_filename,  # {base_name}_geometry.json
                "geometry.json",    # Just geometry.json
                f"{base_name}.geometry.json",  # {base_name}.geometry.json
            ]
            
            found_path = None
            for pattern in alternative_patterns:
                alt_path = step_dir / pattern
                if alt_path.exists():
                    found_path = alt_path
                    print(f"[DEBUG] Found geometry file with alternative pattern: {pattern}")
                    break
            
            if not found_path:
                if not geometry_path.exists():
                    # List files in directory for debugging
                    print(f"[DEBUG] Files in directory {step_dir}:")
                    try:
                        for f in step_dir.iterdir():
                            if f.is_file():
                                print(f"  - {f.name}")
                    except Exception as e:
                        print(f"  - Could not list directory: {e}")
                    
                    print(f"[INFO] Geometry file not found: {geometry_path}")
                    print(f"[INFO] Tried patterns: {alternative_patterns}")
                    print(f"[INFO] Geometry data will not be included in JSON output")
                    return None
                found_path = geometry_path
            
            # Load and parse JSON
            with open(found_path, 'r', encoding='utf-8') as f:
                geometry_data = json.load(f)
            
            print(f"[OK] Loaded geometry data from: {found_path.name}")
            print(f"[OK] Geometry: V={geometry_data.get('volume_cm3', 0)}cm³, M={geometry_data.get('mass_grams', 0)}g")
            
            return geometry_data
            
        except json.JSONDecodeError as e:
            print(f"[ERROR] Invalid JSON in geometry file: {e}")
            import traceback
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            return None
        except Exception as e:
            print(f"[WARNING] Failed to load geometry data: {e}")
            import traceback
            print(f"[WARNING] Traceback: {traceback.format_exc()}")
            return None


def main():
    """Main execution function"""
    # We expect sys.argv to be [script_path, input_step, output_json] or [script_path, input_step, output_json, metadata_json]
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print(f"[ERROR] Invalid arguments.")
        print(f"Usage: <script> <input_step> <output_json> [metadata_json]")
        print(f"  input_step: Path to STEP file")
        print(f"  output_json: Path to output JSON file")
        print(f"  metadata_json: (Optional) Path to threaded holes metadata JSON")
        sys.exit(1)

    input_step = sys.argv[1]
    output_json = sys.argv[2]
    metadata_json = sys.argv[3] if len(sys.argv) == 4 else None

    print("=" * 80)
    print("STEP to OnShape JSON Conversion with Threaded Holes Metadata Support")
    print("=" * 80)
    print(f"Input STEP:  {input_step}")
    print(f"Output JSON: {output_json}")
    if metadata_json:
        print(f"Metadata:    {metadata_json}")
    else:
        print(f"Metadata:    None (no threaded holes metadata)")
    print("=" * 80)

    converter = OnShapeJSONConverter()
    success = converter.convert(input_step, output_json, metadata_json)

    if success:
        print("=" * 80)
        print("[OK] Conversion successful")
        print("=" * 80)
        sys.exit(0)
    else:
        print("=" * 80)
        print("[ERROR] Conversion failed")
        print("=" * 80)
        sys.exit(1)


if __name__ == "__main__":
    main()  
