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


def test_source_archive_extract_refuses_when_disk_reserve_would_be_crossed(tmp_path, monkeypatch):
    from print_handy.core import source_archive
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("part.stl", b"C" * 4096)
    free = intake_resources.MIN_FREE_BYTES
    monkeypatch.setattr(source_archive.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": free})())
    dest = tmp_path / "out"
    with pytest.raises(intake_resources.IntakeLimitExceeded, match="free disk space"):
        source_archive.extract(archive, dest)
    assert not list(dest.rglob("part.stl"))


def test_source_archive_extract_keeps_member_when_reserve_preserved(tmp_path, monkeypatch):
    from print_handy.core import source_archive
    data = b"D" * (intake_resources.CHUNK * 3)
    archive = tmp_path / "upload.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("part.stl", data)
    free = intake_resources.MIN_FREE_BYTES + len(data) + intake_resources.CHUNK
    monkeypatch.setattr(source_archive.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": free})())
    paths, _report = source_archive.extract(archive, tmp_path / "out")
    assert paths[0].read_bytes() == data


def test_zip_download_refuses_a_zip_bomb_member(tmp_path, monkeypatch):
    """The chosen member keeps a compression-ratio guard; free space alone is not enough."""
    from print_handy.core import source_archive
    _mock_zip_download(monkeypatch, tmp_path, b"\0" * (4 * 1024 * 1024))
    folder = tmp_path / "extracted"
    with pytest.raises(intake_resources.IntakeLimitExceeded, match="zip-bomb") as refused:
        sources._fetch_chosen("https://example.invalid/download", folder, "part.stl", "test", exact=False)
    assert f"{source_archive.MAX_RATIO}:1" in str(refused.value)
    assert not (folder / "part.stl").exists()
    assert not (folder / "part.stl.download").exists()


def test_zip_download_disk_refusal_names_the_server_not_the_mesh(tmp_path, monkeypatch):
    _mock_zip_download(monkeypatch, tmp_path, b"A" * 4096)
    free = intake_resources.MIN_FREE_BYTES
    monkeypatch.setattr(sources.shutil, "disk_usage",
                        lambda _: type("Usage", (), {"free": free})())
    with pytest.raises(intake_resources.IntakeLimitExceeded) as refused:
        sources._fetch_chosen("https://example.invalid/download", tmp_path / "x", "part.stl",
                              "test", exact=False)
    assert "low on disk space" in str(refused.value)
    assert "Simplify" not in str(refused.value)
