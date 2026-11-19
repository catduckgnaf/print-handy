"""The bundled Wrapped track carries its third-party credits (catduck, 2026-11-18).

The track is Preston Brown's, from HelixScreen, with a CC BY-SA 3.0
engine sample; it must never be described as catduck's original music.
"""
from pathlib import Path

from print_handy.webui import wrapped_video

ROOT = Path(__file__).resolve().parents[1]
CREDITS = ROOT / "print_handy" / "data" / "audio" / "CREDITS.md"


def test_credits_ship_beside_the_track_and_name_every_source():
    text = CREDITS.read_text()
    assert wrapped_video.BUNDLED_MUSIC.parent == CREDITS.parent
    assert "Preston Brown" in text and "CC BY 4.0" in text
    assert "Edvvc" in text and "CC BY-SA 3.0" in text
    assert "creativecommons.org/licenses/by-sa/3.0" in text


def test_no_surface_claims_the_track_as_original_music_by_catduck():
    for rel in ("NOTICE", "docs/COMPONENTS.md", "docs/WEBUI.md"):
        text = (ROOT / rel).read_text()
        assert "original music by catduck" not in text.lower(), rel
        assert "Preston Brown" in text, rel


def test_the_package_data_glob_ships_the_credits_file():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"data/*/*.md"' in pyproject
