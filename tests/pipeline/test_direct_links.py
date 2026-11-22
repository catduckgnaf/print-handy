"""Direct file links (#607): any public link straight at a .stl/.3mf file.

Fully offline with a fake transport (model_job._open_once) and fake DNS
(model_job._resolve_host_ips) — the guarded fetch, its redirect handling and
its destination checks are the code under test, never the network.
"""
import asyncio
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import intake_resources, link_intake, prepared_intake
from print_handy.jobs import sources as jobs_sources
from .test_color_intake import state  # noqa: F401  (used by the e2e flow)
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is the shared harness fixture)

PUBLIC = {"93.184.216.34"}
STL = b"solid direct\nendsolid direct\n"
TMF = b"PK\x03\x04" + b"\x00" * 64
DIRECT_URL = "https://files.example/models/benchy.stl"
DROPBOX_URL = "https://www.dropbox.com/s/abc123/benchy.stl?dl=0"


class FakeResponse:
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self._pos = 0
        self.headers = headers if headers is not None else {}
        self.status = status

    def read(self, n=-1):
        if self._pos >= len(self._body):
            return b""
        step = n if n and n > 0 else len(self._body)
        chunk = self._body[self._pos:self._pos + step]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(jobs_sources, "_resolve_host_ips", lambda host: set(PUBLIC))
    return PUBLIC


def serve(monkeypatch, handler):
    calls = []

    def fake_open(url):
        calls.append(url)
        return handler(url)

    monkeypatch.setattr(jobs_sources, "_open_once", fake_open)
    return calls


def redirect(location, code=302):
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError("https://files.example/x", code, "redirect", headers, io.BytesIO(b""))


def job_and_source(url=DIRECT_URL):
    job = jobs_sources.inventory_for_url(url)
    return job, job["sources"][0]


# --------------------------------------------------------------------- link recognition

@pytest.mark.parametrize("text", [
    DIRECT_URL,
    "https://cdn.example.org/a/b/MODEL.3MF?token=xyz",
    "please print this https://files.example/benchy.stl for me",
    DROPBOX_URL,
    "https://www.dropbox.com/scl/fi/xyz/part.3mf?rlkey=abc&dl=0",
    "https://dl.dropboxusercontent.com/s/abc123/benchy.stl",
])
def test_model_link_accepts_direct_file_links(text):
    assert link_intake.model_link(text)


@pytest.mark.parametrize("text", [
    "https://example.com/about",
    "https://example.com/model.zip",
    "https://example.com/model.stl.exe",
    "no link at all",
    f"{DIRECT_URL} and https://files.example/other.stl",
])
def test_model_link_rejects_non_file_links(text):
    assert link_intake.model_link(text) is None


# --------------------------------------------------------------------- inventory / registry

def test_inventory_routes_direct_url():
    job, source = job_and_source()
    assert job["site"] == "direct"
    assert job["title"] == "benchy"
    assert source["name"] == "benchy.stl"
    assert source["download_url"] == DIRECT_URL


def test_inventory_unknown_non_file_link_names_direct_links():
    with pytest.raises(ValueError) as caught:
        jobs_sources.inventory_for_url("https://example.com/model/123")
    assert "direct link to a .stl/.3mf file" in str(caught.value)


def test_dropbox_share_link_rewritten_to_download_form():
    job, source = job_and_source(DROPBOX_URL)
    assert job["site"] == "direct"
    assert source["name"] == "benchy.stl"
    assert "dl=1" in source["download_url"] and "dl=0" not in source["download_url"]


def test_dropbox_scl_link_keeps_rlkey_and_sets_dl1():
    fetch = jobs_sources.dropbox_fetch_url(
        "https://www.dropbox.com/scl/fi/xyz/part.3mf?rlkey=abc&dl=0")
    assert "rlkey=abc" in fetch and "dl=1" in fetch


def test_dropboxusercontent_link_used_as_is():
    url = "https://dl.dropboxusercontent.com/s/abc123/benchy.stl"
    assert jobs_sources.dropbox_fetch_url(url) == url


# --------------------------------------------------------------------- guarded download

def test_download_direct_stl(public_dns, monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: FakeResponse(STL, {"Content-Type": "application/octet-stream"}))
    job, source = job_and_source()
    dest = jobs_sources.download_for_site(job, source, tmp_path / "source")
    assert dest.name == "benchy.stl" and dest.read_bytes() == STL


def test_download_direct_3mf(public_dns, monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: FakeResponse(TMF, {"Content-Type": "application/octet-stream"}))
    job, source = job_and_source("https://files.example/part.3mf")
    dest = jobs_sources.direct_download(job, source, tmp_path / "source")
    assert dest.name == "part.3mf" and dest.read_bytes() == TMF


def test_download_names_file_from_content_disposition(public_dns, monkeypatch, tmp_path):
    headers = {"Content-Type": "application/octet-stream",
               "Content-Disposition": 'attachment; filename="big-model.stl"'}
    serve(monkeypatch, lambda url: FakeResponse(STL, headers))
    job = jobs_sources.inventory_for_url("https://www.dropbox.com/s/abc123?dl=0")
    dest = jobs_sources.direct_download(job, job["sources"][0], tmp_path / "source")
    assert dest.name == "big-model.stl"


def test_download_follows_public_redirect(public_dns, monkeypatch, tmp_path):
    def handler(url):
        if url == DIRECT_URL:
            raise redirect("https://cdn.example/final.stl")
        return FakeResponse(STL, {"Content-Type": "application/octet-stream"})

    calls = serve(monkeypatch, handler)
    job, source = job_and_source()
    dest = jobs_sources.direct_download(job, source, tmp_path / "source")
    assert dest.name == "final.stl" and len(calls) == 2


def test_download_redirect_to_private_address_refused(public_dns, monkeypatch, tmp_path):
    calls = serve(monkeypatch, lambda url: (_ for _ in ()).throw(
        redirect("http://127.0.0.1/evil.stl")))
    job, source = job_and_source()
    with pytest.raises(jobs_sources.DirectLinkError, match="not a public address"):
        jobs_sources.direct_download(job, source, tmp_path / "source")
    assert len(calls) == 1  # the private target was never opened


def test_download_redirect_loop_refused(public_dns, monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: (_ for _ in ()).throw(redirect(DIRECT_URL)))
    job, source = job_and_source()
    with pytest.raises(jobs_sources.DirectLinkError, match="redirected too many times"):
        jobs_sources.direct_download(job, source, tmp_path / "source")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/model.stl",
    "http://localhost/model.stl",
    "https://user:secret@files.example/model.stl",
])
def test_download_non_public_or_credentialed_refused(monkeypatch, tmp_path, url):
    calls = serve(monkeypatch, lambda u: FakeResponse(STL))
    job = jobs_sources.direct_inventory(url)
    with pytest.raises(jobs_sources.DirectLinkError):
        jobs_sources.direct_download(job, job["sources"][0], tmp_path / "source")
    assert calls == []


def test_download_private_hostname_refused_before_connect(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs_sources, "_resolve_host_ips", lambda host: {"192.168.0.9"})
    calls = serve(monkeypatch, lambda u: FakeResponse(STL))
    job, source = job_and_source()
    with pytest.raises(jobs_sources.DirectLinkError, match="not a public address"):
        jobs_sources.direct_download(job, source, tmp_path / "source")
    assert calls == []


def test_download_html_page_refused(public_dns, monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: FakeResponse(
        b"<html><body>preview</body></html>", {"Content-Type": "text/html; charset=utf-8"}))
    job, source = job_and_source()
    target = tmp_path / "source"
    with pytest.raises(jobs_sources.DirectLinkError, match="web page instead of a print file"):
        jobs_sources.direct_download(job, source, target)
    assert list(target.iterdir()) == []


def test_download_html_bytes_at_stl_url_refused(public_dns, monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: FakeResponse(
        b"<!DOCTYPE html><html>share page</html>", {"Content-Type": "application/octet-stream"}))
    job, source = job_and_source()
    target = tmp_path / "source"
    with pytest.raises(jobs_sources.DirectLinkError, match="web page instead of a print file"):
        jobs_sources.direct_download(job, source, target)
    assert list(target.iterdir()) == []  # the spoofed file was removed, not saved as a model


def test_download_non_zip_3mf_refused(public_dns, monkeypatch, tmp_path):
    serve(monkeypatch, lambda url: FakeResponse(STL, {"Content-Type": "application/octet-stream"}))
    job, source = job_and_source("https://files.example/part.3mf")
    with pytest.raises(jobs_sources.DirectLinkError, match="not a 3MF file"):
        jobs_sources.direct_download(job, source, tmp_path / "source")


def test_download_unsupported_type_refused(public_dns, monkeypatch, tmp_path):
    headers = {"Content-Type": "application/zip",
               "Content-Disposition": 'attachment; filename="model.zip"'}
    serve(monkeypatch, lambda url: FakeResponse(b"PK\x03\x04rest", headers))
    job, source = job_and_source()
    with pytest.raises(jobs_sources.DirectLinkError, match="not a print file"):
        jobs_sources.direct_download(job, source, tmp_path / "source")


def test_download_oversize_aborted_at_intake_cap(public_dns, monkeypatch, tmp_path):
    monkeypatch.setattr(intake_resources, "MAX_DOWNLOAD_BYTES", 64)
    serve(monkeypatch, lambda url: FakeResponse(b"x" * 4096,
                                                {"Content-Type": "application/octet-stream"}))
    job, source = job_and_source()
    target = tmp_path / "source"
    with pytest.raises(intake_resources.IntakeLimitExceeded, match="exceeds"):
        jobs_sources.direct_download(job, source, target)
    assert list(target.iterdir()) == []  # save_download removes the partial file


def test_download_host_unreachable_named(public_dns, monkeypatch, tmp_path):
    def handler(url):
        raise urllib.error.URLError("no route")

    serve(monkeypatch, handler)
    job, source = job_and_source()
    with pytest.raises(jobs_sources.DirectLinkError, match="could not reach the host"):
        jobs_sources.direct_download(job, source, tmp_path / "source")


def test_download_http_404_named(public_dns, monkeypatch, tmp_path):
    def handler(url):
        raise urllib.error.HTTPError(url, 404, "not found", Message(), io.BytesIO(b""))

    serve(monkeypatch, handler)
    job, source = job_and_source()
    with pytest.raises(jobs_sources.DirectLinkError, match="HTTP 404"):
        jobs_sources.direct_download(job, source, tmp_path / "source")


# --------------------------------------------------------------------- prepared intake e2e

def test_prepared_intake_direct_link_html_refusal_names_cause(env, monkeypatch):
    monkeypatch.setattr(jobs_sources, "_resolve_host_ips", lambda host: set(PUBLIC))
    monkeypatch.setattr(jobs_sources, "_open_once", lambda url: FakeResponse(
        b"<html><body>share page</body></html>", {"Content-Type": "text/html"}))

    async def fake_preview(*args, **kw):
        pass

    monkeypatch.setattr(prepared_intake, "send_preview", fake_preview)
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id="direct1", text=DIRECT_URL)
    adapter = Adapter([])
    asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = json.loads(next(env.root.glob("*.json")).read_text())
    assert receipt["status"] == "FAILED"
    posts = [c for c in adapter.sent if "web page instead of a print file" in c]
    assert posts and posts[0].startswith("That file could not be fetched.")
