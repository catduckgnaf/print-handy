"""Exact comparison against the established topology contract."""
import random
import pytest
from print_handy.jobs.audit import mesh_topology
from print_handy.core.compact_topology import mesh_topology_compact


@pytest.mark.parametrize('seed', range(20))
def test_compact_topology_matches_legacy(seed):
    rng = random.Random(seed)
    vertices = [(float(rng.randrange(4)), float(rng.randrange(4)), float(rng.randrange(4))) for _ in range(18)]
    faces = [[rng.choice(vertices) for _ in range(3)] for _ in range(40)]
    assert mesh_topology_compact(faces) == mesh_topology(faces)


def test_compact_topology_closed_touching_and_empty():
    tetra = [[(0.,0.,0.),(0.,1.,0.),(1.,0.,0.)],
             [(0.,0.,0.),(1.,0.,0.),(0.,0.,1.)],
             [(0.,0.,0.),(0.,0.,1.),(0.,1.,0.)],
             [(1.,0.,0.),(0.,1.,0.),(0.,0.,1.)]]
    for faces in ([], tetra, tetra + [[(-x,-y,-z) for x,y,z in t] for t in tetra]):
        assert mesh_topology_compact(faces) == mesh_topology(faces)
