"""#1844: Print Handy never pulls the detector model from a third party.

Obico paid for about 1,100 downloads a day of its 202 MB model from
obico.io, driven by Print Handy. The fix bundles the weights in the
Docker image and hosts the one download source (for source installs) on
the project's model repository, catduckgnaf/print-handy-vision-model. These checks pin that:

* the only fetch URL is this project's release asset, and the Dockerfile
  bundles exactly that file with the pinned SHA-256;
* obico.io appears in the runtime code only as the recorded provenance;
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
RELEASE_PREFIX = "/catduckgnaf/print-handy-vision-model/releases/download/"


def test_model_url_is_the_model_repos_release_asset():
    url = urllib.parse.urlsplit(lv.MODEL_URL)
    assert url.scheme == "https" and url.hostname == "github.com", lv.MODEL_URL
    assert url.path.startswith(RELEASE_PREFIX), lv.MODEL_URL
    assert url.path.endswith("/" + lv.MODEL_FILENAME), lv.MODEL_URL


def test_dockerfile_bundles_the_pinned_model():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    add = re.search(r"ADD --checksum=sha256:([0-9a-f]{64})\s*\\\s*\n\s*(\S+)\s*\\\s*\n\s*(\S+)", dockerfile)
    assert add, "the Dockerfile must ADD the detector model with a pinned --checksum"
    checksum, source, _dest = add.groups()
    assert checksum == lv.MODEL_SHA256
    assert source == lv.MODEL_URL
    assert f"/opt/print-handy/models/{lv.MODEL_FILENAME}" in dockerfile
    assert lv.DEFAULT_BUNDLED_MODEL_PATH == f"/opt/print-handy/models/{lv.MODEL_FILENAME}"


def test_runtime_code_never_names_obico_io_as_a_source():
    offenders = []
    for path in sorted((ROOT / "print_handy").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "obico.io/" in line and "MODEL_UPSTREAM_URL =" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, f"obico.io URL outside the provenance constant: {offenders}"
    used = [p for p in (ROOT / "print_handy").rglob("*.py")
            if "MODEL_UPSTREAM_URL" in p.read_text(encoding="utf-8")]
    assert used == [ROOT / "print_handy" / "runtime" / "local_vision.py"]
    source = (ROOT / "print_handy" / "runtime" / "local_vision.py").read_text(encoding="utf-8")
    assert source.count("MODEL_UPSTREAM_URL") == 2  # the docstring mention and the definition


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
