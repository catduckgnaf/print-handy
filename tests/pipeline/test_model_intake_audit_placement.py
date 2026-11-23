"""File-to-audit-to-placement integration using production geometry code offline."""
import hashlib
import struct

from print_handy.jobs import audit as jobs_audit


def _write_cube_stl(path):
    points = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0),
              (0, 0, 20), (10, 0, 20), (10, 10, 20), (0, 10, 20)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    with path.open('wb') as stream:
        stream.write(b'integration cube'.ljust(80, b' '))
        stream.write(struct.pack('<I', len(faces)))
        for face in faces:
            stream.write(struct.pack('<12fH', 0, 0, 1, *(value for vertex in face for value in points[vertex]), 0))
    return path


def test_stl_file_audit_carries_real_geometry_into_ranked_printer_placement(tmp_path):
    source = _write_cube_stl(tmp_path / 'part.stl')
    report = jobs_audit.audit(source, printer_model='offline-printer')

    assert report['path'] == 'part.stl'
    assert report['kind'] == '.stl'
    assert report['bytes'] == source.stat().st_size
    assert report['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report['mesh_audit']['dimensions_mm'] == [10.0, 10.0, 20.0]
    assert report['mesh_audit']['manifold'] is True
    candidates = report['mesh_audit']['placement_candidates']
    assert candidates
    assert all(row['printer_model'] == 'offline-printer' for row in candidates)
    assert sorted(row['rank'] for row in candidates) == list(range(1, len(candidates) + 1))
    assert any(row['fits_bed'] and row['footprint_mm'][2] == 20.0 for row in candidates)
