"""The selected ZIP member cannot fill the filesystem before intake validation (#1833)."""
import shutil
import zipfile

import pytest

from print_handy.core import intake_resources
from print_handy.jobs import sources


def _mock_zip_download(monkeypatch, tmp_path, contents):
    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("folder/part.stl", contents)
    monkeypatch.setattr(sources, "download",
                        lambda _url, target, _headers=None: shutil.copyfile(archive, target))


def test_zip_extraction_refuses_when_disk_reserve_would_be_crossed(tmp_path, monkeypatch):
    _mock_zip_download(monkeypatch, tmp_path, b"A" * 4096)
    free = intake_resources.MIN_FREE_BYTES
    monkeypatch.setattr(sources.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": free})())
    folder = tmp_path / "extracted"
    with pytest.raises(intake_resources.IntakeLimitExceeded, match="free disk space"):
        sources._fetch_chosen("https://example.invalid/download", folder, "part.stl", "test", exact=False)
    assert not (folder / "part.stl").exists()
    assert not (folder / "part.stl.download").exists()


def test_zip_extraction_keeps_any_size_that_preserves_disk_reserve(tmp_path, monkeypatch):
    data = b"B" * (intake_resources.CHUNK * 3)
    _mock_zip_download(monkeypatch, tmp_path, data)
    free = intake_resources.MIN_FREE_BYTES + len(data) + intake_resources.CHUNK
    monkeypatch.setattr(sources.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": free})())
    result = sources._fetch_chosen("https://example.invalid/download",
                                   tmp_path / "extracted", "part.stl", "test", exact=False)
    assert result.read_bytes() == data
