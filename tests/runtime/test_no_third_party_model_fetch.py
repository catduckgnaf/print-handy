"""#1844, #1899: detector model pinning, bundling, and zero Obico fetches.

In #1844 Print Handy stopped pulling Obico weights from obico.io; in #1899
Obico was replaced completely by PrintGuard Standard (oliverbravery/PrintGuard),
pinned by full 40-hex commit SHA and SHA-256 and bundled into the Docker image
at build time. These checks pin that:

* the only fetch URL is the pinned PrintGuard commit on raw.githubusercontent.com
  (never a moving branch like ``main``), and the Dockerfile bundles that exact
  file with the matching SHA-256;
* ``obico.io`` never appears anywhere in runtime code under ``print_handy/``;
* a bundled copy is used as is, with no download started.

The suite-wide guard in tests/conftest.py (``_no_public_internet``) is the
other half: no test may reach a public address at all.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from print_handy.runtime import local_vision as lv

ROOT = Path(__file__).resolve().parents[2]
PINNED_PATH_RE = re.compile(
    r"^/oliverbravery/PrintGuard/[0-9a-f]{40}/models/standard/model\.onnx$"
)


def test_model_url_is_pinned_to_an_immutable_printguard_commit():
    url = urllib.parse.urlsplit(lv.MODEL_URL)
    assert url.scheme == "https" and url.hostname == "raw.githubusercontent.com", lv.MODEL_URL
    assert PINNED_PATH_RE.fullmatch(url.path), lv.MODEL_URL
    assert lv.MODEL_REVISION in url.path
    assert re.fullmatch(r"[0-9a-f]{40}", lv.MODEL_REVISION)
    assert re.fullmatch(r"[0-9a-f]{64}", lv.MODEL_SHA256)


def test_dockerfile_bundles_the_pinned_model():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    add = re.search(
        r"ADD --checksum=sha256:([0-9a-f]{64})\s*\\\s*\n\s*(\S+)\s*\\\s*\n\s*(\S+)",
        dockerfile,
    )
    assert add, "the Dockerfile must ADD the detector model with a pinned --checksum"
    checksum, source, _dest = add.groups()
    assert checksum == lv.MODEL_SHA256
    assert source == lv.MODEL_URL
    assert f"/opt/print-handy/models/{lv.MODEL_FILENAME}" in dockerfile
    assert lv.DEFAULT_BUNDLED_MODEL_PATH == f"/opt/print-handy/models/{lv.MODEL_FILENAME}"


def test_runtime_code_never_references_obico():
    offenders = []
    for path in sorted((ROOT / "print_handy").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lower = line.lower()
            if "obico.io" in lower or "obico-failure-detector" in lower or "print-handy-vision-model" in lower:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, f"retired Obico model reference in runtime code: {offenders}"


def test_bundled_model_is_used_without_any_download(monkeypatch, tmp_path):
    bundled = tmp_path / "bundled" / lv.MODEL_FILENAME
    bundled.parent.mkdir()
    bundled.write_bytes(b"bundled weights")
    monkeypatch.setenv("PRINT_HANDY_BUNDLED_MODEL", str(bundled))
    monkeypatch.setattr(lv, "_download_model",
                        lambda path: (_ for _ in ()).throw(AssertionError("downloaded")))
    cfg = {"paths": {"state_home": str(tmp_path / "state")},
           "vision": {"local_detector_enabled": True}}
    assert lv.model_path(cfg) == bundled
    assert lv.prefetch_model(cfg, wait=True) is False
    assert not (tmp_path / "state" / "models").exists()


def test_operator_model_path_still_wins_over_the_bundled_copy(monkeypatch, tmp_path):
    bundled = tmp_path / lv.MODEL_FILENAME
    bundled.write_bytes(b"bundled weights")
    monkeypatch.setenv("PRINT_HANDY_BUNDLED_MODEL", str(bundled))
    own = tmp_path / "mine.onnx"
    cfg = {"paths": {"state_home": str(tmp_path)}, "vision": {"local_model_path": str(own)}}
    assert lv.model_path(cfg) == own


def test_without_a_bundled_copy_the_model_goes_to_the_state_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("PRINT_HANDY_BUNDLED_MODEL", str(tmp_path / "absent" / lv.MODEL_FILENAME))
    cfg = {"paths": {"state_home": str(tmp_path)}, "vision": {}}
    assert lv.model_path(cfg) == tmp_path / "models" / lv.MODEL_FILENAME
