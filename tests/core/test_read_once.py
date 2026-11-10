"""print_now reads its G-code once: the decoded text must equal what Path.read_text gave."""
import hashlib

import pytest

from print_handy.core import model_job as mj

SAMPLES = [
    b"; plain\nG1 X1 Y2\n",
    b"; windows\r\nG1 X1\r\nM104 S200\r\n",
    b"; old mac\rG1 X1\r",
    b"; mixed\r\nG1\nG2\rG3",
    "; ünïcode name ✓\nG1 X1\n".encode("utf-8"),
    b"",
]


@pytest.mark.parametrize("raw", SAMPLES)
def test_decoding_the_bytes_matches_read_text(tmp_path, raw):
    path = tmp_path / "plate.gcode"
    path.write_bytes(raw)
    assert mj._text_like_read_text(raw) == path.read_text()
    assert mj._text_like_read_text(raw, "ignore") == path.read_text(errors="ignore")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == mj.file_sha256(path)


def test_undecodable_bytes_still_fail_like_read_text(tmp_path):
    path = tmp_path / "bad.gcode"
    raw = b"G1 X1\n\xff\xfe\xfa bad\n"
    path.write_bytes(raw)
    try:
        expected = path.read_text()
    except UnicodeDecodeError:
        with pytest.raises(UnicodeDecodeError):
            mj._text_like_read_text(raw)
    else:  # a locale codec that maps every byte
        assert mj._text_like_read_text(raw) == expected
    assert mj._text_like_read_text(raw, "ignore") == path.read_text(errors="ignore")
