import zipfile

from print_handy.intake.fit_flow import large_model_notice


def _3mf(path, mesh_bytes):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('3D/Objects/object_1.model', b' ' * mesh_bytes)
    return path


def test_small_model_gets_no_notice(tmp_path):
    assert large_model_notice(_3mf(tmp_path / 'a.3mf', 1_000_000)) is None


def test_large_model_says_it_is_not_stuck(tmp_path):
    text = large_model_notice(_3mf(tmp_path / 'b.3mf', 90_000_000))
    assert 'about 2 minutes' in text and 'not stuck' in text


def test_unreadable_file_gets_no_notice(tmp_path):
    bad = tmp_path / 'c.3mf'
    bad.write_bytes(b'not a zip')
    assert large_model_notice(bad) is None
