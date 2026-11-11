"""Upload hardening (#1145): streamed to disk with a size and a concurrency
cap, expiring links revoked when a login is set, an owner-only link store,
an inbox quota, and cleaned file names."""
import http.client
import io
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from print_handy import settings
from print_handy.webui import access, server, upload

from test_webui_upload import multipart_body

ROUTE_TOKEN = "test-webui-token-12345"


def load_settings(tmp, environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n"
                     "webui:\n  enabled: true\n  port: 8765\n")
    return settings.load(local=local, environ=environ or {})


@pytest.fixture()
def data(tmp_path):
    return load_settings(tmp_path)


def start(tmp_path, environ=None):
    environ = {"WEBUI_ACCESS_TOKEN": ROUTE_TOKEN} if environ is None else environ
    settings_data = load_settings(tmp_path, environ)
    httpd = server.make_server(settings_data, environ=environ, port=0,
                               snapshot_func=lambda: {})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.fixture()
def running(tmp_path):
    httpd, port = start(tmp_path)
    yield httpd, port
    httpd.shutdown()
    httpd.server_close()


# -- streaming and the size cap -----------------------------------------------

class ChunkRecorder(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.reads = []

    def read(self, size=-1):
        chunk = super().read(size)
        self.reads.append(len(chunk))
        return chunk


def test_the_body_is_streamed_to_a_spool_file_in_chunks(data, tmp_path):
    content = os.urandom(300_000) + b"\r\n--not-the-boundary\r\n" + os.urandom(1000)
    body, content_type = multipart_body({"_csrf": "tok"}, "big.3mf", content)
    rfile = ChunkRecorder(body)
    spool = upload.spool_dir(data)
    fields, files = upload.parse_multipart_stream(rfile, len(body), content_type, spool)
    assert fields == {"_csrf": "tok"}
    assert len(files) == 1 and files[0][0] == "big.3mf"
    assert Path(files[0][1]).parent == spool
    assert Path(files[0][1]).read_bytes() == content
    # Never one read of the whole body: bounded chunks only.
    assert len(rfile.reads) > 1 and max(rfile.reads) <= 64 * 1024
    saved, reason = upload.save_upload(data, files[0][0], files[0][1])
    assert reason is None and saved.read_bytes() == content
    assert not Path(files[0][1]).exists()  # moved, not copied
    assert list(spool.iterdir()) == []


def test_a_file_part_over_the_size_cap_stops_the_read(data):
    body, content_type = multipart_body({}, "big.stl", b"x" * 5000)
    with mock.patch.object(upload, "MAX_UPLOAD_BYTES", 1024):
        with pytest.raises(upload.UploadError) as caught:
            upload.parse_multipart_stream(io.BytesIO(body), len(body), content_type,
                                          upload.spool_dir(data))
    assert caught.value.status == 413
    assert list(upload.spool_dir(data).iterdir()) == []  # the partial file is gone


def test_a_malformed_body_is_a_400_and_leaves_nothing(data):
    body = b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.stl\"\r\n\r\nsolid"
    with pytest.raises(upload.UploadError) as caught:
        upload.parse_multipart_stream(io.BytesIO(body), len(body),
                                      "multipart/form-data; boundary=b",
                                      upload.spool_dir(data))
    assert caught.value.status == 400
    assert list(upload.spool_dir(data).iterdir()) == []


def test_route_refuses_a_file_over_the_cap_while_streaming(running):
    httpd, port = running
    token = upload.create_link(httpd.settings_data, httpd.access.credential_fingerprint())
    body, content_type = multipart_body({}, "big.stl", b"x" * 5000)
    with mock.patch.object(upload, "MAX_UPLOAD_BYTES", 1024):
        code, _, _ = request(port, "POST", f"/u/{token}", body,
                             {"Content-Type": content_type})
    assert code == 413
    assert upload.list_inbox(httpd.settings_data) == []
    assert list(upload.spool_dir(httpd.settings_data).iterdir()) == []


# -- the concurrency cap ------------------------------------------------------

def test_uploads_past_the_concurrency_cap_are_refused_unread(running):
    httpd, port = running
    token = upload.create_link(httpd.settings_data, httpd.access.credential_fingerprint())
    taken = 0
    while upload.UPLOAD_SLOTS.try_acquire():
        taken += 1
    assert taken == upload.MAX_CONCURRENT_UPLOADS
    try:
        body, content_type = multipart_body({}, "a.stl", b"solid a\n")
        with mock.patch.object(upload, "parse_multipart_stream") as parse:
            code, headers, raw = request(port, "POST", f"/u/{token}", body,
                                         {"Content-Type": content_type})
        assert code == 503
        assert headers.get("Retry-After") == "30"
        assert b"Try again in a moment" in raw
        parse.assert_not_called()
        assert upload.list_inbox(httpd.settings_data) == []
    finally:
        for _ in range(taken):
            upload.UPLOAD_SLOTS.release()
    # With a slot free again the same upload goes through, and the slot
    # is handed back afterwards.
    code, _, _ = request(port, "POST", f"/u/{token}", body,
                         {"Content-Type": content_type})
    assert code == 200
    assert upload.UPLOAD_SLOTS.try_acquire()
    upload.UPLOAD_SLOTS.release()


# -- link expiry and revocation -----------------------------------------------

def test_an_expired_link_is_refused(running):
    httpd, port = running
    data, fingerprint = httpd.settings_data, httpd.access.credential_fingerprint()
    token = upload.create_link(data, fingerprint)
    assert upload.link_valid(data, token, fingerprint)
    later = time.time() + upload.LINK_TTL_S + 1
    with mock.patch.object(upload.time, "time", return_value=later):
        assert not upload.link_valid(data, token, fingerprint)
        assert upload.list_links(data, fingerprint) == []
        code, _, _ = request(port, "GET", f"/u/{token}")
        assert code == 404
        body, content_type = multipart_body({}, "late.stl", b"solid l\n")
        code, _, _ = request(port, "POST", f"/u/{token}", body,
                             {"Content-Type": content_type})
        assert code == 404
    assert upload.list_inbox(data) == []


def test_a_link_from_before_expiry_existed_runs_out_from_its_creation(data):
    path = Path(settings.state_dir(data)) / upload.LINKS_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    token = "an-old-link-token"
    old = int(time.time()) - upload.LINK_TTL_S - 10
    path.write_text(json.dumps({upload._digest(token): {"created": old, "uses": 3}}))
    assert not upload.link_valid(data, token)


def test_links_are_revoked_when_a_login_is_set(tmp_path):
    # An open install (no token, nothing saved): mint a link, then set a
    # password from another process (chat setup, the CLI). The next
    # request applies it and every link minted while open stops working.
    httpd, port = start(tmp_path, environ={})
    try:
        data = httpd.settings_data
        assert not httpd.access.required
        token = upload.create_link(data, httpd.access.credential_fingerprint())
        assert request(port, "GET", f"/u/{token}")[0] == 200
        password = "correct horse battery staple"
        access.save_password(data, password, password, username="owner")
        # Any request syncs the access rule first; the link is now dead.
        assert request(port, "GET", f"/u/{token}")[0] == 404
        assert httpd.access.mode == access.PASSWORD
        stored = json.loads((Path(settings.state_dir(data)) / upload.LINKS_FILE_NAME)
                            .read_text())
        assert [key for key in stored if key != upload._FINGERPRINT_KEY] == []
        assert upload.list_links(data, httpd.access.credential_fingerprint()) == []
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_link_minted_under_another_credential_is_not_valid(data):
    token = upload.create_link(data, "fingerprint-a")
    assert upload.link_valid(data, token, "fingerprint-a")
    assert not upload.link_valid(data, token, "fingerprint-b")
    saved, reason = upload.save_upload_for_link(data, token, "a.stl", b"solid",
                                                "fingerprint-b")
    assert (saved, reason) == (None, None)
    # Minting under the new credential drops the old store's links.
    upload.create_link(data, "fingerprint-b")
    assert not upload.link_valid(data, token, "fingerprint-a")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_the_link_store_is_owner_only(data):
    upload.create_link(data, "fp")
    path = Path(settings.state_dir(data)) / upload.LINKS_FILE_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    upload.revoke_all_links(data, "fp2")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_spool_files_are_owner_only(data):
    body, content_type = multipart_body({}, "a.stl", b"solid a\n")
    _fields, files = upload.parse_multipart_stream(
        io.BytesIO(body), len(body), content_type, upload.spool_dir(data))
    assert stat.S_IMODE(Path(files[0][1]).stat().st_mode) == 0o600
    upload.discard_spooled(files)


# -- the inbox quota ----------------------------------------------------------

def test_the_inbox_quota_refuses_with_a_clear_message(data):
    with mock.patch.object(upload, "INBOX_QUOTA_BYTES", 100):
        saved, reason = upload.save_upload(data, "a.stl", b"x" * 60)
        assert reason is None
        saved, reason = upload.save_upload(data, "b.stl", b"x" * 60)
    assert saved is None
    assert reason.startswith(upload.INBOX_FULL)
    assert "Delete files from the inbox" in reason and "nothing was saved" in reason
    assert [row[0] for row in upload.list_inbox(data)] == ["a.stl"]
    # Deleting a file frees the room.
    assert upload.delete_inbox_file(data, "a.stl")
    with mock.patch.object(upload, "INBOX_QUOTA_BYTES", 100):
        saved, reason = upload.save_upload(data, "b.stl", b"x" * 60)
    assert reason is None


def test_route_refuses_an_upload_past_the_quota_before_reading(running):
    httpd, port = running
    token = upload.create_link(httpd.settings_data, httpd.access.credential_fingerprint())
    body, content_type = multipart_body({}, "a.stl", b"x" * 200_000)
    with mock.patch.object(upload, "INBOX_QUOTA_BYTES", 1000), \
            mock.patch.object(upload, "parse_multipart_stream") as parse:
        code, _, raw = request(port, "POST", f"/u/{token}", body,
                               {"Content-Type": content_type})
    assert code == 507
    assert b"The upload inbox is full" in raw
    parse.assert_not_called()
    assert upload.list_inbox(httpd.settings_data) == []


def test_the_pre_read_quota_check_counts_the_whole_body(running):
    # duckcatgnaf on #1219 (head adc531f3): subtracting the whole multipart
    # allowance from Content-Length let a file that cannot fit be received
    # before the save-time check refused it. The early check counts the
    # whole body, so this file (room for it only if the allowance were
    # subtracted) is refused before a byte is read.
    httpd, port = running
    token = upload.create_link(httpd.settings_data, httpd.access.credential_fingerprint())
    body, content_type = multipart_body({}, "a.stl", b"x" * 2000)
    with mock.patch.object(upload, "INBOX_QUOTA_BYTES", 1500),             mock.patch.object(upload, "MULTIPART_OVERHEAD_BYTES", 1000),             mock.patch.object(upload, "parse_multipart_stream") as parse:
        code, _, raw = request(port, "POST", f"/u/{token}", body,
                               {"Content-Type": content_type})
    assert code == 507
    parse.assert_not_called()


def test_stale_spool_files_are_pruned_and_live_ones_kept(data):
    spool = upload.spool_dir(data)
    spool.mkdir(parents=True)
    stale = spool / "upload-old.part"
    live = spool / "upload-new.part"
    stale.write_bytes(b"x" * 100)
    live.write_bytes(b"y" * 100)
    old = time.time() - upload.STALE_SPOOL_S - 60
    os.utime(stale, (old, old))
    assert upload.inbox_usage(data) == 200
    assert upload.prune_stale_spool(data) == 1
    assert not stale.exists() and live.exists()
    assert upload.inbox_usage(data) == 100
    # The server prunes at startup too, so an orphan never holds quota.
    os.utime(live, (old, old))
    httpd = server.make_server(data, environ={}, port=0, snapshot_func=lambda: {})
    try:
        assert not live.exists()
    finally:
        httpd.server_close()


def test_delete_accepts_only_a_listed_inbox_name(data):
    upload.save_upload(data, "keep.stl", b"solid k\n")
    (Path(settings.state_dir(data)) / "other.json").write_text("{}")
    for name in ("../other.json", "other.json", ".incoming", "", "keep.stl/.."):
        assert not upload.delete_inbox_file(data, name)
    assert (Path(settings.state_dir(data)) / "other.json").exists()
    assert upload.delete_inbox_file(data, "keep.stl")
    assert upload.list_inbox(data) == []


# -- name sanitizing ----------------------------------------------------------

@pytest.mark.parametrize("submitted, expected", [
    ("bra\x1b[31mcket.stl", "bra[31mcket.stl"),
    ("line\nbreak\r.stl", "linebreak.stl"),
    ("evil‮gnp.stl", "evilgnp.stl"),         # right-to-left override
    ("zero​width.3mf", "zerowidth.3mf"),      # zero-width space (format)
    ("tab\there.stl", "tabhere.stl"),
    ("..\\..\\win\\path.stl", "path.stl"),
    ("...hidden.stl", "hidden.stl"),
    ("  spaced name.stl  ", "spaced name.stl"),
    ("nul\x00byte.stl", "nulbyte.stl"),
    ("café bénchy.stl", "café bénchy.stl"),  # ordinary text stays
])
def test_names_lose_control_and_format_characters(submitted, expected):
    assert upload.clean_name(submitted) == expected


def test_an_over_long_name_keeps_its_extension(data):
    name = upload.clean_name("a" * 500 + ".stl")
    assert name.endswith(".stl") and len(name) == upload.MAX_NAME_CHARS


def test_a_saved_file_carries_the_cleaned_name(data):
    saved, reason = upload.save_upload(data, "ok\x07‮name.stl", b"solid n\n")
    assert reason is None
    assert saved.name == "okname.stl"
    # A name that is nothing but control characters and an extension is refused.
    saved, reason = upload.save_upload(data, "\x1b\x07.stl", b"solid n\n")
    assert saved is None and reason
