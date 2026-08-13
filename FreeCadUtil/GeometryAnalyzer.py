"""
GeometryAnalyzer.py - Utility functions for analyzing FreeCAD geometry

This module provides functions to:
- Calculate volume and surface area
- Count and analyze individual faces
- Calculate mass for different materials
- Export geometry metadata to JSON

Author: Tolery API AI
"""

import json
import os
import re
from typing import Dict, List, Optional, Tuple

try:
    import FreeCAD as App
    FREECAD_AVAILABLE = True
except ImportError:
    FREECAD_AVAILABLE = False

# Mapping of material names to FreeCAD .FCMat file paths
MATERIAL_FCMAT_MAP = {
    "steel": "Standard/Metal/Steel/Steel-Generic.FCMat",
    "stainless_steel": "Standard/Metal/Steel/Steel-1C22.FCMat",
    "aluminum": "Standard/Metal/Aluminum/Aluminum-Generic.FCMat",
}


def _load_density_from_fcmat(material_file_path: str) -> Optional[float]:
    """
    Load density from FreeCAD .FCMat file.
    
    Args:
        material_file_path: Absolute path to .FCMat file
    
    Returns:
        Density in g/mm³ or None if not found
    """
    if not os.path.exists(material_file_path):
        return None
    
    try:
        with open(material_file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Look for Density line in YAML format
        # Format: Density: "7900 kg/m^3" or Density: "2.7 g/cm^3"
        density_match = re.search(
            r'Density:\s*["\']?([0-9.]+)\s*(kg/m\^?3|g/cm\^?3|kg/m³|g/cm³)',
            content,
            re.IGNORECASE
        )
        
        if density_match:
            value = float(density_match.group(1))
            unit = density_match.group(2).lower()
            
            # Convert to g/mm³
            if 'kg/m' in unit:
                # kg/m³ to g/mm³: divide by 1,000,000
                return value / 1000000.0
            elif 'g/cm' in unit:
                # g/cm³ to g/mm³: divide by 1,000
                return value / 1000.0
        
        return None
    except Exception:
        return None


def get_material_density(material_name: str) -> Tuple[float, str]:
    """
    Get material density from FreeCAD .FCMat files.
    
    Args:
        material_name: Name of material (e.g., "steel", "aluminum")
    
    Returns:
        Tuple of (density in g/mm³, source)
        Source is either "freecad_material" or "default_steel"
    """
    material_key = material_name.lower().replace(' ', '_').replace('-', '_')
    
    # Try to load from FreeCAD .FCMat file
    if FREECAD_AVAILABLE and material_key in MATERIAL_FCMAT_MAP:
        try:
            material_base = os.path.join(
                App.getResourceDir(),
                "Mod",
                "Material",
                "Resources",
                "Materials"
            )
            mat_file = MATERIAL_FCMAT_MAP[material_key]
            mat_path = os.path.join(material_base, mat_file)
            
            density = _load_density_from_fcmat(mat_path)
            if density is not None:
                return (density, "freecad_material")
        except Exception:
            pass
    
    # Default to steel if material not found (7900 kg/m³ = 0.00790 g/mm³)
    return (0.00790, "default_steel")



def analyze_geometry(shape, material: str = "steel") -> Dict:
    """
    Analyze a FreeCAD shape and return comprehensive geometry information.
    
    Args:
        shape: FreeCAD Shape object
        material: Material name (default: "steel")
    
    Returns:
        Dictionary containing volume, surface area, face count, and mass calculations
    
    Example:
        >>> from FreeCadUtil.GeometryAnalyzer import analyze_geometry
        >>> result = analyze_geometry(my_shape, material="aluminum")
        >>> print(f"Volume: {result['volume_mm3']:.2f} mm³")
    """
    # Calculate basic geometry
    volume = shape.Volume  # mm³
    surface_area = shape.Area  # mm²
    face_count = len(shape.Faces)
    
    # Get material density from FreeCAD or fallback
    density, density_source = get_material_density(material)
    
    # Calculate mass
    mass_grams = volume * density
    mass_kg = mass_grams / 1000.0
    
    # Analyze individual faces
    faces_info = []
    for i, face in enumerate(shape.Faces):
        face_area = face.Area
        face_type = face.Surface.__class__.__name__  # e.g., "Plane", "Cylinder", "Cone"
        
        faces_info.append({
            "face_id": i + 1,
            "area_mm2": round(face_area, 2),
            "area_cm2": round(face_area / 100.0, 2),
            "type": face_type,
            "percentage_of_total": round((face_area / surface_area) * 100, 2) if surface_area > 0 else 0
        })
    
    return {
        "volume_mm3": round(volume, 2),
        "volume_cm3": round(volume / 1000.0, 2),
        "surface_area_mm2": round(surface_area, 2),
        "surface_area_cm2": round(surface_area / 100.0, 2),
        "face_count": face_count,
        "mass_grams": round(mass_grams, 2),
        "mass_kg": round(mass_kg, 4),
        "material": material,
        "density_g_per_mm3": density,
        "density_source": density_source,
        "faces": faces_info
    }


def print_geometry_report(shape, material: str = "steel", show_faces: bool = True):
    """
    Print a formatted geometry analysis report to console.
    
    Args:
        shape: FreeCAD Shape object
        material: Material name (default: "steel")
        show_faces: Whether to show individual face details (default: True)
    
    Example:
        >>> from FreeCadUtil.GeometryAnalyzer import print_geometry_report
        >>> print_geometry_report(my_shape, material="aluminum", show_faces=True)
    """
    result = analyze_geometry(shape, material)
    
    print("\n" + "="*70)
    print("GEOMETRY ANALYSIS REPORT")
    print("="*70)
    
    print(f"\n📐 DIMENSIONS:")
    print(f"   Volume:       {result['volume_mm3']:>12.2f} mm³  ({result['volume_cm3']:.2f} cm³)")
    print(f"   Surface Area: {result['surface_area_mm2']:>12.2f} mm²  ({result['surface_area_cm2']:.2f} cm²)")
    print(f"   Face Count:   {result['face_count']:>12d} faces")
    
    print(f"\n⚖️  MASS CALCULATION ({result['material'].upper()}):")
    print(f"   Density:      {result['density_g_per_mm3']:>12.5f} g/mm³")
    density_source_label = {
        "freecad_material": "(from FreeCAD .FCMat)",
        "default_steel": "(default: steel 7900 kg/m³)"
    }.get(result.get('density_source', 'default_steel'), "")
    if density_source_label:
        print(f"   Source:       {density_source_label}")
    print(f"   Mass:         {result['mass_grams']:>12.2f} g  ({result['mass_kg']:.4f} kg)")
    
    if show_faces and result['faces']:
        print(f"\n🔍 FACE ANALYSIS:")
        print(f"   {'Face ID':<10} {'Type':<20} {'Area (mm²)':<15} {'Area (cm²)':<15} {'% of Total':<12}")
        print(f"   {'-'*10} {'-'*20} {'-'*15} {'-'*15} {'-'*12}")
        
        for face in result['faces']:
            print(f"   {face['face_id']:<10} {face['type']:<20} {face['area_mm2']:<15.2f} "
                  f"{face['area_cm2']:<15.2f} {face['percentage_of_total']:<12.2f}%")
    
    print("\n" + "="*70 + "\n")


def calculate_mass_for_materials(volume: float, materials: Optional[List[str]] = None) -> Dict:
    """
    Calculate mass for multiple materials given a volume.
    
    Args:
        volume: Volume in mm³
        materials: List of material names (if None, uses all available materials)
    
    Returns:
        Dictionary with material names as keys and mass info as values
    
    Example:
        >>> from FreeCadUtil.GeometryAnalyzer import calculate_mass_for_materials
        >>> masses = calculate_mass_for_materials(10000, ["steel", "aluminum"])
        >>> print(masses["steel"]["mass_grams"])
    """
    if materials is None:
        materials = list(MATERIAL_FCMAT_MAP.keys())
    
    results = {}
    for material in materials:
        density, density_source = get_material_density(material)
        mass_grams = volume * density
        mass_kg = mass_grams / 1000.0
        
        results[material] = {
            "density_g_per_mm3": density,
            "density_source": density_source,
            "mass_grams": round(mass_grams, 2),
            "mass_kg": round(mass_kg, 4)
        }
    
    return results


def export_geometry_to_json(shape, output_path: str, material: str = "steel", 
                            additional_info: Optional[Dict] = None):
    """
    Export geometry analysis to a JSON file.
    
    Args:
        shape: FreeCAD Shape object
        output_path: Path to save JSON file
        material: Material name (default: "steel")
        additional_info: Optional dictionary with additional metadata to include
    
    Example:
        >>> from FreeCadUtil.GeometryAnalyzer import export_geometry_to_json
        >>> export_geometry_to_json(my_shape, "output/geometry.json", 
        ...                         material="aluminum",
        ...                         additional_info={"part_name": "Bracket_01"})
    """
    result = analyze_geometry(shape, material)
    
    # Add additional info if provided
    if additional_info:
        result.update(additional_info)
    
    # Write to JSON file
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    
    print(f"[SUCCESS] Geometry data exported to: {output_path}")


def get_face_details(shape, face_index: int) -> Dict:
    """
    Get detailed information about a specific face.
    
    Args:
        shape: FreeCAD Shape object
        face_index: Index of the face (0-based)
    
    Returns:
        Dictionary with detailed face information
    
    Example:
        >>> from FreeCadUtil.GeometryAnalyzer import get_face_details
        >>> face_info = get_face_details(my_shape, 0)
        >>> print(f"Face type: {face_info['type']}")
    """
    if face_index < 0 or face_index >= len(shape.Faces):
        raise IndexError(f"Face index {face_index} out of range. Shape has {len(shape.Faces)} faces.")
    
    face = shape.Faces[face_index]
    
    return {
        "face_id": face_index + 1,
        "area_mm2": round(face.Area, 2),
        "area_cm2": round(face.Area / 100.0, 2),
        "type": face.Surface.__class__.__name__,
        "edge_count": len(face.Edges),
        "vertex_count": len(face.Vertexes),
        "center_of_mass": {
            "x": round(face.CenterOfMass.x, 2),
            "y": round(face.CenterOfMass.y, 2),
            "z": round(face.CenterOfMass.z, 2)
        }
    }


# Convenience function for quick analysis
def quick_analysis(shape, material: str = "steel") -> Tuple[float, float, int, float]:
    """
    Quick analysis returning only the most essential values.
    
    Args:
        shape: FreeCAD Shape object
        material: Material name (default: "steel")
    
    Returns:
        Tuple of (volume_mm3, surface_area_mm2, face_count, mass_grams)
    
    Example:
        >>> from FreeCadUtil.GeometryAnalyzer import quick_analysis
        >>> vol, area, faces, mass = quick_analysis(my_shape, "aluminum")
        >>> print(f"Volume: {vol:.2f} mm³, Faces: {faces}")
    """
    volume = shape.Volume
    surface_area = shape.Area
    face_count = len(shape.Faces)
    density, _ = get_material_density(material)
    mass_grams = volume * density
    
    return (
        round(volume, 2),
        round(surface_area, 2),
        face_count,
        round(mass_grams, 2)
    )
