#!/usr/bin/env python3
"""
3D Geometry Utilities for Oblong Detection

Provides utility functions for 3D geometry operations:
- Distance calculations
- DBSCAN clustering
- Plane fitting
- Bbox operations
- Vector operations
"""

import math
from typing import List, Dict, Tuple, Optional, Any
import numpy as np

try:
    from sklearn.cluster import DBSCAN
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    print("[WARNING] sklearn not available. DBSCAN will use fallback implementation.")


def euclidean_distance_3d(point1: Dict[str, float], point2: Dict[str, float]) -> float:
    """
    Calculate Euclidean distance between two 3D points.
    
    Args:
        point1: Dict with 'x', 'y', 'z' keys
        point2: Dict with 'x', 'y', 'z' keys
    
    Returns:
        Distance in mm
    """
    dx = point1['x'] - point2['x']
    dy = point1['y'] - point2['y']
    dz = point1['z'] - point2['z']
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def distance_matrix_3d(points: List[Dict[str, float]]) -> List[List[float]]:
    """
    Calculate distance matrix for a list of 3D points.
    
    Args:
        points: List of dicts with 'x', 'y', 'z' keys
    
    Returns:
        n×n matrix of distances
    """
    n = len(points)
    matrix = [[0.0] * n for _ in range(n)]
    
    for i in range(n):
        for j in range(i+1, n):
            dist = euclidean_distance_3d(points[i], points[j])
            matrix[i][j] = dist
            matrix[j][i] = dist
    
    return matrix


def dbscan_3d(points: List[Dict[str, float]], eps: float, min_samples: int = 4) -> List[List[int]]:
    """
    Apply DBSCAN clustering to 3D points.
    
    Args:
        points: List of dicts with 'x', 'y', 'z' keys
        eps: Maximum distance between points in same cluster
        min_samples: Minimum points required to form cluster
    
    Returns:
        List of clusters, each cluster is a list of point indices
    """
    if not points:
        return []
    
    if HAS_SKLEARN:
        # Use sklearn DBSCAN
        coords = np.array([[p['x'], p['y'], p['z']] for p in points])
        clustering = DBSCAN(eps=eps, min_samples=min_samples, metric='euclidean')
        labels = clustering.fit_predict(coords)
        
        # Group points by cluster label
        clusters = {}
        for idx, label in enumerate(labels):
            if label >= 0:  # Ignore noise points (label = -1)
                if label not in clusters:
                    clusters[label] = []
                clusters[label].append(idx)
        
        return list(clusters.values())
    else:
        # Fallback: Simple distance-based clustering
        return _dbscan_fallback(points, eps, min_samples)


def _dbscan_fallback(points: List[Dict[str, float]], eps: float, min_samples: int) -> List[List[int]]:
    """
    Simple DBSCAN fallback implementation.
    """
    n = len(points)
    visited = [False] * n
    clusters = []
    
    for i in range(n):
        if visited[i]:
            continue
        
        # Find neighbors
        neighbors = []
        for j in range(n):
            if i != j and euclidean_distance_3d(points[i], points[j]) <= eps:
                neighbors.append(j)
        
        if len(neighbors) < min_samples - 1:  # -1 because we don't count point i
            continue  # Noise point
        
        # Start new cluster
        cluster = [i]
        visited[i] = True
        
        # Expand cluster
        seed_set = neighbors[:]
        while seed_set:
            q = seed_set.pop(0)
            if visited[q]:
                continue
            
            visited[q] = True
            cluster.append(q)
            
            # Find neighbors of q
            q_neighbors = []
            for j in range(n):
                if j != q and euclidean_distance_3d(points[q], points[j]) <= eps:
                    q_neighbors.append(j)
            
            if len(q_neighbors) >= min_samples - 1:
                seed_set.extend([n for n in q_neighbors if not visited[n]])
        
        if len(cluster) >= min_samples:
            clusters.append(cluster)
    
    return clusters


def fit_plane_3d(points: List[Dict[str, float]]) -> Tuple[float, float, float, float]:
    """
    Fit a plane to 3D points using least squares.
    
    Args:
        points: List of dicts with 'x', 'y', 'z' keys (at least 3 points)
    
    Returns:
        Tuple (a, b, c, d) where ax + by + cz + d = 0
    """
    if len(points) < 3:
        raise ValueError("Need at least 3 points to fit a plane")
    
    # Convert to numpy array
    coords = np.array([[p['x'], p['y'], p['z']] for p in points])
    
    # Calculate centroid
    centroid = np.mean(coords, axis=0)
    
    # Center points
    centered = coords - centroid
    
    # SVD to find normal vector
    U, S, Vt = np.linalg.svd(centered)
    normal = Vt[-1]  # Last row is normal vector
    
    # Normalize
    normal = normal / np.linalg.norm(normal)
    
    # Calculate d: d = -normal · centroid
    d = -np.dot(normal, centroid)
    
    return (normal[0], normal[1], normal[2], d)


def distance_to_plane_3d(point: Dict[str, float], plane: Tuple[float, float, float, float]) -> float:
    """
    Calculate perpendicular distance from point to plane.
    
    Args:
        point: Dict with 'x', 'y', 'z' keys
        plane: Tuple (a, b, c, d) where ax + by + cz + d = 0
    
    Returns:
        Distance in mm
    """
    a, b, c, d = plane
    x, y, z = point['x'], point['y'], point['z']
    
    # Distance = |ax + by + cz + d| / sqrt(a² + b² + c²)
    numerator = abs(a*x + b*y + c*z + d)
    denominator = math.sqrt(a*a + b*b + c*c)
    
    return numerator / denominator if denominator > 0 else 0.0


def bbox_3d_intersection(bbox1: Dict[str, Dict[str, float]], bbox2: Dict[str, Dict[str, float]]) -> bool:
    """
    Check if two 3D bounding boxes intersect.
    
    Args:
        bbox1: Dict with 'min' and 'max' keys, each with 'x', 'y', 'z'
        bbox2: Dict with 'min' and 'max' keys, each with 'x', 'y', 'z'
    
    Returns:
        True if bboxes intersect
    """
    # Check if bboxes overlap in all 3 dimensions
    overlap_x = (bbox1['min']['x'] <= bbox2['max']['x'] and 
                 bbox1['max']['x'] >= bbox2['min']['x'])
    overlap_y = (bbox1['min']['y'] <= bbox2['max']['y'] and 
                 bbox1['max']['y'] >= bbox2['min']['y'])
    overlap_z = (bbox1['min']['z'] <= bbox2['max']['z'] and 
                 bbox1['max']['z'] >= bbox2['min']['z'])
    
    return overlap_x and overlap_y and overlap_z


def bbox_3d_union(bbox1: Dict[str, Dict[str, float]], bbox2: Dict[str, Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """
    Calculate union of two 3D bounding boxes.
    
    Args:
        bbox1: Dict with 'min' and 'max' keys
        bbox2: Dict with 'min' and 'max' keys
    
    Returns:
        Union bbox
    """
    return {
        'min': {
            'x': min(bbox1['min']['x'], bbox2['min']['x']),
            'y': min(bbox1['min']['y'], bbox2['min']['y']),
            'z': min(bbox1['min']['z'], bbox2['min']['z'])
        },
        'max': {
            'x': max(bbox1['max']['x'], bbox2['max']['x']),
            'y': max(bbox1['max']['y'], bbox2['max']['y']),
            'z': max(bbox1['max']['z'], bbox2['max']['z'])
        }
    }


def expand_bbox_3d(bbox: Dict[str, Dict[str, float]], tolerance: float) -> Dict[str, Dict[str, float]]:
    """
    Expand bounding box by tolerance in all 6 directions.
    
    Args:
        bbox: Dict with 'min' and 'max' keys
        tolerance: Expansion amount in mm
    
    Returns:
        Expanded bbox
    """
    return {
        'min': {
            'x': bbox['min']['x'] - tolerance,
            'y': bbox['min']['y'] - tolerance,
            'z': bbox['min']['z'] - tolerance
        },
        'max': {
            'x': bbox['max']['x'] + tolerance,
            'y': bbox['max']['y'] + tolerance,
            'z': bbox['max']['z'] + tolerance
        }
    }


def dot_product_3d(v1: Dict[str, float], v2: Dict[str, float]) -> float:
    """
    Calculate dot product of two 3D vectors.
    
    Args:
        v1: Dict with 'x', 'y', 'z' keys
        v2: Dict with 'x', 'y', 'z' keys
    
    Returns:
        Dot product
    """
    return v1['x'] * v2['x'] + v1['y'] * v2['y'] + v1['z'] * v2['z']


def angle_between_vectors_3d(v1: Dict[str, float], v2: Dict[str, float]) -> float:
    """
    Calculate angle between two 3D vectors in degrees.
    
    Args:
        v1: Dict with 'x', 'y', 'z' keys
        v2: Dict with 'x', 'y', 'z' keys
    
    Returns:
        Angle in degrees (0-180)
    """
    dot = dot_product_3d(v1, v2)
    mag1 = math.sqrt(v1['x']**2 + v1['y']**2 + v1['z']**2)
    mag2 = math.sqrt(v2['x']**2 + v2['y']**2 + v2['z']**2)
    
    if mag1 == 0 or mag2 == 0:
        return 0.0
    
    cos_angle = dot / (mag1 * mag2)
    cos_angle = max(-1.0, min(1.0, cos_angle))  # Clamp to [-1, 1]
    
    return math.degrees(math.acos(cos_angle))


def normalize_vector_3d(v: Dict[str, float]) -> Dict[str, float]:
    """
    Normalize 3D vector to unit length.
    
    Args:
        v: Dict with 'x', 'y', 'z' keys
    
    Returns:
        Normalized vector
    """
    mag = math.sqrt(v['x']**2 + v['y']**2 + v['z']**2)
    if mag == 0:
        return {'x': 0.0, 'y': 0.0, 'z': 0.0}
    
    return {
        'x': v['x'] / mag,
        'y': v['y'] / mag,
        'z': v['z'] / mag
    }


def group_cylinders_by_radius(cylinders: List[Dict[str, Any]], tolerance: float = 0.5) -> Dict[float, List[Dict[str, Any]]]:
    """
    Group cylinders by radius into buckets.
    
    Args:
        cylinders: List of cylinder dicts with 'radius' key
        tolerance: Radius tolerance for grouping (mm)
    
    Returns:
        Dict mapping radius (rounded) to list of cylinders
    """
    buckets = {}
    
    for cyl in cylinders:
        radius = cyl.get('radius', 0.0)
        
        # Round to nearest bucket
        bucket_key = round(radius / tolerance) * tolerance
        
        if bucket_key not in buckets:
            buckets[bucket_key] = []
        buckets[bucket_key].append(cyl)
    
    return buckets


def calculate_adaptive_epsilon(total_length: float, width: float, 
                                nearest_oblong: Optional[Dict] = None,
                                min_eps: Optional[float] = None,
                                max_eps: float = 50.0) -> float:
    """
    Calculate adaptive DBSCAN epsilon based on oblong size and gap.
    
    Args:
        total_length: Oblong total length (mm)
        width: Oblong width (mm)
        nearest_oblong: Optional nearest oblong metadata (for gap calculation)
        min_eps: Minimum epsilon (default: 2× width)
        max_eps: Maximum epsilon (default: 50mm)
    
    Returns:
        Adaptive epsilon value
    """
    # Base epsilon from total length
    base_eps = total_length * 1.5
    
    # Minimum epsilon
    if min_eps is None:
        min_eps = width * 2.0
    
    # Consider gap to nearest oblong
    if nearest_oblong:
        center = {'x': 0, 'y': 0, 'z': 0}  # Placeholder, should use actual center
        nearest_center = nearest_oblong.get('center', {})
        gap = euclidean_distance_3d(center, nearest_center)
        base_eps = min(base_eps, gap * 0.4)  # 40% of gap
    
    # Clamp to [min_eps, max_eps]
    eps = max(min_eps, min(base_eps, max_eps))
    
    return eps
