"""Dispatch to the native mesh kernel for large mesh intake (#1790).

The Rust kernel (``native/bounds/src/mesh.rs`` in the bounds extension,
``print_handy.core._bounds_native``) replaces the per-triangle Python loops
that made a 56 MB STL pair take minutes before its first question: mesh
topology, bounds with area and volume for a transformed copy, the 3MF
``<vertices>``/``<triangles>`` text, and STL parsing. The Python code it
replaces stays forever as the fallback and the test oracle; every function
here returns None when the native path does not apply, and the caller then
runs its own Python code unchanged.

The kernel is used under the bounds extension's rules
(``print_handy.gcode.bounds``): only when the extension imports, reports the
expected ``MESH_KERNEL_VERSION`` and passes a golden self-check, and never
when ``PRINT_HANDY_BOUNDS_IMPL=python``. It is used only for input it can
take exactly: typed float64 storage (``CompactTriangles``,
``IndexedTriangles``, and ``TransformedTriangles`` chains over them whose
transforms carry a native recipe), with every coordinate finite. Anything
else, and any input the kernel declines (``MeshDecline``: a non-finite
coordinate after a transform, ASCII STL text outside the plain decimal
grammar), takes the Python code under every setting, ``native`` included,
the way non-ASCII G-code takes the Python bounds parser. A kernel fault
(any other exception) is logged and disables the kernel for the rest of the
process; ``PRINT_HANDY_BOUNDS_IMPL=native`` turns a missing, mismatched or
failing kernel into an error instead.
"""
import logging
import os
from array import array

import numpy as np

logger = logging.getLogger(__name__)

# The mesh kernel's semantics version; bump with any change to a contract.
MESH_KERNEL_VERSION = 1
_NEVER_SWALLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)
# Lists of triangles are converted only where the Python topology itself
# switches to its compact path (jobs/audit.mesh_topology), which reads every
# value through float() just as the conversion does.
LIST_TOPOLOGY_MIN = 100_000

_module = None
_broken = False


def _setting():
    return os.environ.get("PRINT_HANDY_BOUNDS_IMPL", "auto").strip().lower()


# Golden self-check: a tetrahedron with one face missing, one face listed
# twice (a non-manifold edge set) and a needle triangle that welds
# degenerate, moved by a transform chain. Expected values
# are the Python oracle's (compact_topology, the scalar loops in
# mesh_fast/geometry_layout, write_geometry_only_3mf), recorded once.
_SELF_SOUP = (
    0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0,
    1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0,
    1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0,
    0.0, 0.0, 0.0, 0.0, 0.0, 0.0000001, 1.0, 0.0, 0.0,
)
_SELF_OPS = [("rotz", [0.6, 0.8]), ("translate", [2.0, -1.0, 0.5])]
_SELF_TOPOLOGY = {'triangles': 5, 'shells': 1, 'boundary_edges': 2, 'non_manifold_edges': 2,
                  'singular_vertices': 4, 'winding_inconsistencies': 1, 'degenerate_triangles': 1}
_SELF_MEASURE = ((1.2, -1.0, 0.5), (2.6, -0.19999999999999996, 1.5), 2.7320508575688778, 0.18333329666666662)
_SELF_VERTICES = (
    '          <vertex x="2.000000" y="-1.000000" z="0.500000"/>\n'
    '          <vertex x="1.200000" y="-0.400000" z="0.500000"/>\n'
    '          <vertex x="2.600000" y="-0.200000" z="0.500000"/>\n'
    '          <vertex x="2.000000" y="-1.000000" z="1.500000"/>')
_SELF_TRIANGLES = (
    '          <triangle v1="0" v2="1" v3="2"/>\n'
    '          <triangle v1="0" v2="2" v3="3"/>\n'
    '          <triangle v1="2" v2="1" v3="3"/>\n'
    '          <triangle v1="2" v2="1" v3="3"/>\n'
    '          <triangle v1="0" v2="0" v3="2"/>')


def _self_check(module):
    soup = array('d', _SELF_SOUP)
    topo = module.mesh_topology(soup, None, _SELF_OPS)
    if dict(topo) != _SELF_TOPOLOGY:
        raise ValueError(f"native mesh topology self-check mismatch: {topo!r}")
    measured = module.mesh_measure(soup, None, _SELF_OPS)
    lo, hi, area, volume = measured
    if (tuple(lo), tuple(hi)) != _SELF_MEASURE[:2] or (area, volume) != _SELF_MEASURE[2:]:
        raise ValueError(f"native mesh measure self-check mismatch: {measured!r}")
    if tuple(module.threemf_mesh_text(soup, None, _SELF_OPS)) != (_SELF_VERTICES, _SELF_TRIANGLES):
        raise ValueError("native 3MF mesh text self-check mismatch")


def _load():
    """The verified native module, or None. Cached; a failure is not retried."""
    global _module, _broken
    if _module is not None or _broken:
        return _module
    try:
        from . import _bounds_native as module
        version = getattr(module, "MESH_KERNEL_VERSION", None)
        if version != MESH_KERNEL_VERSION:
            raise ValueError(f"native mesh kernel version {version!r} != expected {MESH_KERNEL_VERSION}")
        _self_check(module)
    except BaseException as exc:
        if isinstance(exc, _NEVER_SWALLOW):
            raise
        _broken = True
        if _setting() == "native":
            raise RuntimeError(f"native mesh kernel required but unavailable: {exc}") from exc
        logger.debug("native mesh kernel unavailable, using Python: %s", exc)
        return None
    _module = module
    return module


def _reset_cache():
    """Forget the cached module verdict (tests only)."""
    global _module, _broken
    _module = None
    _broken = False


def available():
    """True when the native kernel will be used for eligible input."""
    return _setting() != "python" and _load() is not None


def _call(name, *args):
    """Run one kernel function and return its result, or None for "use Python".

    None means the caller runs its own Python code: under the ``python``
    setting, when the kernel is unavailable or disabled (``auto``), when the
    kernel declines the input (``MeshDecline``, every setting), and after a
    kernel fault (``auto``). Under ``native`` an unavailable, disabled or
    faulting kernel raises RuntimeError instead of returning None; a decline
    still returns None, because it is the contract, not a fault.
    """
    global _broken
    setting = _setting()
    if setting == "python":
        return None
    required = setting == "native"
    if _broken:
        if required:
            raise RuntimeError("native mesh kernel required but unavailable: "
                               "disabled after an earlier failure in this process")
        return None
    module = _load()
    if module is None:
        return None
    try:
        return getattr(module, name)(*args)
    except BaseException as exc:
        if isinstance(exc, _NEVER_SWALLOW):
            raise
        if isinstance(exc, module.MeshDecline):
            logger.debug("native mesh kernel declined %s (%s); using Python", name, exc)
            return None
        _broken = True
        if required:
            raise RuntimeError(f"native mesh kernel required but failed: {exc!r}") from exc
        logger.warning("native mesh kernel failed in %s (%r); using Python", name, exc)
        return None


def mesh_args(triangles, allow_lists=False):
    """``(vertices, faces, ops)`` for the kernel, or None when not typed storage.

    ``vertices`` is a float64 buffer (a soup of nine values per triangle when
    ``faces`` is None), ``faces`` an int64 array, and ``ops`` the transform
    chain innermost first. ``allow_lists`` also converts a plain triangle
    list (topology only: see LIST_TOPOLOGY_MIN).
    """
    from .compact_geometry import CompactTriangles, IndexedTriangles, TransformedTriangles
    ops = []
    base = triangles
    while isinstance(base, TransformedTriangles):
        if base.ops is None:
            return None
        ops[:0] = base.ops
        base = base.source
    if isinstance(base, CompactTriangles):
        return base.coordinates, None, ops
    if isinstance(base, IndexedTriangles):
        vertices = base.vertices
        if vertices.dtype != np.float64:
            return None
        return (np.ascontiguousarray(vertices).reshape(-1),
                np.ascontiguousarray(base.faces, dtype=np.int64).reshape(-1), ops)
    if allow_lists and not ops and isinstance(base, list):
        try:
            soup = np.asarray(base, dtype=np.float64)
        except (TypeError, ValueError):
            return None
        if soup.shape != (len(base), 3, 3):
            return None
        return soup.reshape(-1), None, ops
    return None


def _wire_ops(ops):
    return [(kind, [float(v) for v in params]) for kind, params in ops]


def topology(triangles):
    """``mesh_topology_compact``'s dict for ``triangles``, or None."""
    args = mesh_args(triangles, allow_lists=hasattr(triangles, '__len__')
                     and len(triangles) > LIST_TOPOLOGY_MIN)
    if args is None:
        return None
    vertices, faces, ops = args
    counts = _call("mesh_topology", vertices, faces, _wire_ops(ops))
    if counts is None:
        return None
    count = int(counts["triangles"])
    shells = int(counts["shells"])
    boundary = int(counts["boundary_edges"])
    nonmanifold = int(counts["non_manifold_edges"])
    singular = int(counts["singular_vertices"])
    winding = int(counts["winding_inconsistencies"])
    degenerate = int(counts["degenerate_triangles"])
    return {'format': 'triangle-mesh', 'triangles': count, 'shells': shells,
            'triangle_count': count, 'connected_shells': shells,
            'boundary_edges': boundary, 'non_manifold_edges': nonmanifold,
            'non_manifold_vertices': singular, 'singular_vertices': singular,
            'winding_inconsistencies': winding, 'degenerate_triangles': degenerate,
            'watertight': boundary == 0 and nonmanifold == 0,
            'manifold': not (boundary or nonmanifold or singular or degenerate or winding)}


def measure(triangles):
    """``(lo, hi, area, signed_volume)`` as the scalar Python loops compute them, or None.

    Bounds start at the first point and move on strictly smaller or larger
    values; area and signed volume add one triangle at a time from 0.0.
    None for an empty mesh (the Python loops raise their own error).
    """
    args = mesh_args(triangles)
    if args is None:
        return None
    vertices, faces, ops = args
    result = _call("mesh_measure", vertices, faces, _wire_ops(ops))
    if result is None:
        return None
    lo, hi, area, volume = result
    return [float(v) for v in lo], [float(v) for v in hi], float(area), float(volume)


def threemf_text(triangles):
    """``(vertex_lines, triangle_lines)`` of one ``write_geometry_only_3mf`` object, or None."""
    args = mesh_args(triangles)
    if args is None:
        return None
    vertices, faces, ops = args
    return _call("threemf_mesh_text", vertices, faces, _wire_ops(ops))


def weld(triangles, round6=True):
    """An indexed mesh ``(vertices (N, 3) float64, faces (M, 3) int64)`` in first-occurrence order, or None.

    ``round6`` welds on Python ``round(v, 6)`` keys and stores the first
    corner's rounded values (the 3MF writer's welding); otherwise corners
    weld on exact equality (``-0.0`` equals ``0.0``).
    """
    args = mesh_args(triangles)
    if args is None:
        return None
    vertices, faces, ops = args
    result = _call("mesh_weld", vertices, faces, _wire_ops(ops), bool(round6))
    if result is None:
        return None
    welded, ids = result
    return (np.frombuffer(welded, dtype='<f8').reshape((-1, 3)),
            np.frombuffer(ids, dtype='<i8').reshape((-1, 3)))


def stl_parse(data):
    """``('binary' | 'ascii', coordinates)`` for STL bytes, or None.

    ``coordinates`` holds nine float64 values per triangle, exactly what
    ``orca_adapter.read_stl_triangles`` reads. The native ASCII number
    parser accepts only a subset of Python's float(): pure-ASCII text with
    every token in the plain decimal grammar
    ``[+-]?(digits[.digits]|.digits)([eE][+-]?digits)?``. Anything float()
    also accepts (``inf``, ``nan``, underscores, non-ASCII digits) or
    rejects makes the kernel raise MeshDecline, and this returns None so the
    Python regex and float() decide the whole file.
    """
    result = _call("stl_parse", data)
    if result is None:
        return None
    kind, coords = result
    return kind, coords
