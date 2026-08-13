#!/usr/bin/env python3
"""
Standalone CDT triangulation worker, run as a SEPARATE subprocess by
triangulate_planar_face_with_holes_cdt() in step_converter.py.

Why a subprocess: the `triangle` package (Shewchuk's C triangulator, ported
via Cython) has been observed to segfault on certain PSLG inputs, even when
the input data itself is independently verified valid (no duplicate points,
correct hole positions/radii) -- pointing to a heap-corruption-sensitive bug
in the C library itself rather than bad input. A SIGSEGV cannot be caught by
Python's try/except, so an in-process crash would kill the whole freecadcmd
job. Running it here means a crash only kills this short-lived helper
process; the caller sees a non-zero/failed exit and falls back to the proven
OCC MeshPart path, exactly as it already does for any other CDT failure mode
(topology mismatch, area sanity check, etc).

Usage: python3 cdt_worker.py <input.npz> <output.npz>
input.npz  : vertices (V,2) float64, segments (S,2) int32, holes (H,2) float64
output.npz : triangles (M,3) int32, out_verts (K,2) float64
Exit code 0 + output.npz written = success. Any other outcome = caller falls back.
"""
import sys
import numpy as np


def main():
    if len(sys.argv) != 3:
        print("Usage: cdt_worker.py <input.npz> <output.npz>", file=sys.stderr)
        return 2

    input_path, output_path = sys.argv[1], sys.argv[2]
    data = np.load(input_path)

    import triangle as _triangle_lib

    pslg = {
        "vertices": data["vertices"],
        "segments": data["segments"],
        "holes": data["holes"],
    }
    result = _triangle_lib.triangulate(pslg, 'p')
    tri_idx = result.get('triangles')
    out_verts = result.get('vertices')
    if tri_idx is None or len(tri_idx) == 0 or out_verts is None:
        return 1

    np.savez(output_path, triangles=np.asarray(tri_idx, dtype=np.int32), out_verts=np.asarray(out_verts, dtype=np.float64))
    return 0


if __name__ == "__main__":
    sys.exit(main())
