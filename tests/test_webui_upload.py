"""Web UI upload + inbox: files land in the inbox and nowhere else."""
import http.client
import re
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.core import intake_resources
from print_handy.webui import pages, server, upload


def load_settings(tmp):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n")
    return settings.load(local=local, environ={})


class UploadTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.data = load_settings(self.tmp)

    def test_stl_and_3mf_land_in_the_inbox(self):
        saved, reason = upload.save_upload(self.data, "clip.stl", b"solid x\n")
        self.assertIsNone(reason)
        self.assertEqual(saved.parent, upload.inbox_dir(self.data))
        self.assertEqual(saved.read_bytes(), b"solid x\n")
        saved2, _ = upload.save_upload(self.data, "clip.3mf", b"PK\x03\x04")
        self.assertTrue(saved2.exists())
        self.assertEqual([row[0] for row in upload.list_inbox(self.data)],
                         ["clip.3mf", "clip.stl"])

    def test_other_extensions_and_empty_files_are_refused(self):
        for name, content in [("notes.txt", b"hi"), ("model.gcode", b"g"),
                              ("clip.stl", b"")]:
            saved, reason = upload.save_upload(self.data, name, content)
            self.assertIsNone(saved)
            self.assertTrue(reason)
        self.assertEqual(upload.list_inbox(self.data), [])

    def test_stl_over_the_intake_limit_is_refused(self):
        big = b"x" * (intake_resources.MAX_STL_BYTES + 1)
        saved, reason = upload.save_upload(self.data, "big.stl", big)
        self.assertIsNone(saved)
        self.assertIn("32", reason)

    def test_a_path_in_the_name_never_reaches_disk(self):
        saved, _ = upload.save_upload(self.data, "../../etc/evil.stl", b"solid")
        self.assertEqual(saved.parent, upload.inbox_dir(self.data))
        self.assertEqual(saved.name, "evil.stl")

    def test_name_clash_gets_a_suffix_not_an_overwrite(self):
        first, _ = upload.save_upload(self.data, "a.stl", b"one")
        second, _ = upload.save_upload(self.data, "a.stl", b"two")
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), b"one")
        self.assertEqual(second.read_bytes(), b"two")


class ConcurrentUploadTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.data = load_settings(self.tmp)

    def test_concurrent_same_name_uploads_both_land(self):
        # The Web UI server is threaded, so two uploads with the same
        # name can arrive together. Pause both threads right after the
        # name-clash check so they interleave the way the race does:
        # without protection both pass the check for the same target.
        import threading
        from unittest import mock

        barrier = threading.Barrier(2)
        real_exists = Path.exists

        def synced_exists(path_self):
            if path_self.name == "race.stl":
                try:
                    barrier.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return real_exists(path_self)

        results, errors = {}, {}

        def run(idx):
            try:
                results[idx] = upload.save_upload(
                    self.data, "race.stl", f"content-{idx}".encode())
            except Exception as exc:  # noqa: BLE001 - recorded, asserted below
                errors[idx] = exc

        threads = [threading.Thread(target=run, args=(idx,)) for idx in range(2)]
        with mock.patch.object(Path, "exists", synced_exists):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, {})
        self.assertEqual(sorted(results), [0, 1])
        paths = []
        for idx in range(2):
            saved, reason = results[idx]
            self.assertIsNone(reason)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.read_bytes(), f"content-{idx}".encode())
            paths.append(saved)
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(len(upload.list_inbox(self.data)), 2)


class MultipartTests(unittest.TestCase):
    def test_parse_multipart_round_trip(self):
        boundary = "----ph-test"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="_csrf"\r\n\r\n'
            "tok123\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="a.stl"\r\n'
            "Content-Type: model/stl\r\n\r\n"
        ).encode() + b"solid \x00\x01\r\n" + f"--{boundary}--\r\n".encode()
        fields, files = upload.parse_multipart(
            body, f"multipart/form-data; boundary={boundary}")
        self.assertEqual(fields["_csrf"], "tok123")
        self.assertEqual(files[0][0], "a.stl")
        self.assertTrue(files[0][1].startswith(b"solid"))

    def test_parse_multipart_refuses_other_content_types(self):
        self.assertEqual(upload.parse_multipart(b"a=b", "application/x-www-form-urlencoded"),
                         (None, None))


class LinkTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.data = load_settings(self.tmp)

    def test_link_lifecycle(self):
        token = upload.create_link(self.data)
        self.assertTrue(upload.link_valid(self.data, token))
        self.assertFalse(upload.link_valid(self.data, token + "x"))
        self.assertFalse(upload.link_valid(self.data, ""))
        upload.note_link_use(self.data, token)
        rows = upload.list_links(self.data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 1)
        # The token itself is never stored.
        stored = (Path(self.tmp) / "hermes" / "state" /
                  upload.LINKS_FILE_NAME).read_text()
        self.assertNotIn(token, stored)
        self.assertTrue(upload.revoke_link(self.data, rows[0][0]))
        self.assertFalse(upload.link_valid(self.data, token))

    def test_pages_render(self):
        html = upload.render_upload_page(self.data, form_token="tok")
        self.assertIn("Upload to inbox", html)
        self.assertIn('enctype="multipart/form-data"', html)
        bare = upload.render_token_page()
        self.assertIn("Upload a model file", bare)
        self.assertNotIn("/config", bare)
        self.assertNotIn("Model sources", bare)


class ConcurrentLinkTests(unittest.TestCase):
    """The link store is read-modify-write JSON on a threaded server:
    concurrent create / use / revoke operations must not lose each
    other, resurrect a revoked link, or clobber a shared temp file."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.data = load_settings(self.tmp)

    def _synced_loads(self):
        # Pause every load at the same barrier so concurrent callers
        # interleave the way the race does. Once the fix serializes
        # the callers, the first load's barrier simply times out and
        # breaks, and later loads pass through the broken barrier.
        barrier = threading.Barrier(2)
        real_load = upload._load_links

        def synced(data):
            try:
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                pass
            return real_load(data)

        return mock.patch.object(upload, "_load_links", synced)

    def _run_pair(self, fn):
        results, errors = {}, {}

        def run(idx):
            try:
                results[idx] = fn(idx)
            except Exception as exc:  # noqa: BLE001 - recorded, asserted below
                errors[idx] = exc

        threads = [threading.Thread(target=run, args=(idx,)) for idx in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, {})
        return results

    def test_concurrent_create_links_none_lost(self):
        with self._synced_loads():
            results = self._run_pair(lambda idx: upload.create_link(self.data))
        self.assertNotEqual(results[0], results[1])
        self.assertEqual(len(upload.list_links(self.data)), 2)

    def test_concurrent_note_link_use_counts_every_use(self):
        token = upload.create_link(self.data)
        with self._synced_loads():
            self._run_pair(lambda idx: upload.note_link_use(self.data, token))
        rows = upload.list_links(self.data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 2)

    def test_concurrent_link_saves_do_not_share_a_temp_file(self):
        # Sync both writers after their temp write, before the replace:
        # with one shared temp path, the second replace then has no
        # temp file left and fails.
        barrier = threading.Barrier(2)
        real_write_text = Path.write_text

        def synced_write_text(path_self, *args, **kwargs):
            result = real_write_text(path_self, *args, **kwargs)
            if path_self.name.startswith("webui_upload_links"):
                try:
                    barrier.wait(timeout=3)
                except threading.BrokenBarrierError:
                    pass
            return result

        payloads = [{"a": {"created": 1, "uses": 0}},
                    {"b": {"created": 2, "uses": 0}}]
        with mock.patch.object(Path, "write_text", synced_write_text):
            self._run_pair(lambda idx: upload._save_links(self.data, payloads[idx]))
        self.assertIn(upload._load_links(self.data), payloads)


ROUTE_TOKEN = "test-webui-token-12345"


def multipart_body(fields, filename, content):
    boundary = "----ph-route-test"
    chunks = []
    for name, value in fields.items():
        chunks.append((
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n").encode())
    if filename is not None:
        chunks.append((
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: model/stl\r\n\r\n").encode() + content + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class UploadRouteTests(unittest.TestCase):
    """Route-level contract for /upload and /u/<token> over real HTTP."""

    def start(self, tmp):
        environ = {"WEBUI_ACCESS_TOKEN": ROUTE_TOKEN}
        local = Path(tmp) / "local.yaml"
        local.write_text(
            f"paths:\n  hermes_home: {tmp}/hermes\n"
            "webui:\n  enabled: true\n  port: 8765\n")
        data = settings.load(local=local, environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: {})
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def request(self, port, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login_cookie(self, port):
        from urllib.parse import urlencode
        code, headers, _ = self.request(
            port, "POST", "/login",
            urlencode({pages.LOGIN_TOKEN_FIELD: ROUTE_TOKEN}),
            {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(code, 303)
        return headers["Set-Cookie"].split(";")[0]

    def form_token(self, port, headers):
        code, _, raw = self.request(port, "GET", "/upload", headers=headers)
        self.assertEqual(code, 200)
        match = re.search(r'name="_csrf" value="([^"]+)"',
                          raw.decode("utf-8", "replace"))
        self.assertIsNotNone(match)
        return match.group(1)

    def test_upload_body_is_not_parsed_before_auth(self):
        # An unauthenticated multipart POST must be refused before the
        # (up to ~200 MiB) body is read or parsed at all.
        import tempfile
        parse_calls = []
        real_parse = upload.parse_multipart

        def recording_parse(body, content_type):
            parse_calls.append(len(body))
            return real_parse(body, content_type)

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            body, content_type = multipart_body({}, "a.stl", b"solid x\n")
            with mock.patch.object(upload, "parse_multipart", recording_parse):
                code, _, _ = self.request(
                    port, "POST", "/upload", body,
                    {"Content-Type": content_type})
            self.assertEqual(code, 403)
            self.assertEqual(parse_calls, [])
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])

    def test_upload_post_with_session_and_form_token_saves_binary_stl(self):
        import tempfile
        content = b"solid\x00\x01\r\nline\xff\xfe end"
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            cookie = self.login_cookie(port)
            csrf = self.form_token(port, {"Cookie": cookie})
            body, content_type = multipart_body({"_csrf": csrf}, "bin.stl", content)
            code, _, _ = self.request(
                port, "POST", "/upload", body,
                {"Content-Type": content_type, "Cookie": cookie})
            self.assertEqual(code, 200)
            rows = upload.list_inbox(httpd.settings_data)
            self.assertEqual([row[0] for row in rows], ["bin.stl"])
            saved = upload.inbox_dir(httpd.settings_data) / "bin.stl"
            self.assertEqual(saved.read_bytes(), content)

    def test_upload_post_with_session_but_wrong_form_token_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            cookie = self.login_cookie(port)
            self.form_token(port, {"Cookie": cookie})
            body, content_type = multipart_body({"_csrf": "wrong"}, "a.stl",
                                                b"solid x\n")
            code, _, _ = self.request(
                port, "POST", "/upload", body,
                {"Content-Type": content_type, "Cookie": cookie})
            self.assertEqual(code, 403)
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])

    def test_token_routes_serve_only_the_upload_form(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            data = httpd.settings_data
            token = upload.create_link(data)
            code, _, raw = self.request(port, "GET", "/u/not-a-token")
            self.assertEqual(code, 404)
            code, _, raw = self.request(port, "GET", f"/u/{token}")
            self.assertEqual(code, 200)
            page = raw.decode("utf-8", "replace")
            self.assertIn("Upload a model file", page)
            self.assertNotIn("/config", page)
            # The link token opens no other route.
            code, _, _ = self.request(port, "GET", "/api/status")
            self.assertEqual(code, 401)
            body, content_type = multipart_body({}, "via-link.stl", b"solid y\n")
            code, _, _ = self.request(
                port, "POST", "/u/not-a-token", body,
                {"Content-Type": content_type})
            self.assertEqual(code, 404)
            self.assertEqual(upload.list_inbox(data), [])
            code, _, _ = self.request(
                port, "POST", f"/u/{token}", body,
                {"Content-Type": content_type})
            self.assertEqual(code, 200)
            self.assertEqual([row[0] for row in upload.list_inbox(data)],
                             ["via-link.stl"])
            self.assertEqual(upload.list_links(data)[0][2], 1)

    def test_link_revoked_during_the_upload_read_saves_nothing(self):
        import tempfile
        real_parse = upload.parse_multipart

        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            data = httpd.settings_data
            token = upload.create_link(data)
            digest = upload.list_links(data)[0][0]

            def revoking_parse(body, content_type):
                upload.revoke_link(data, digest)
                return real_parse(body, content_type)

            body, content_type = multipart_body({}, "slow.stl", b"solid z\n")
            with mock.patch.object(upload, "parse_multipart", revoking_parse):
                code, _, _ = self.request(
                    port, "POST", f"/u/{token}", body,
                    {"Content-Type": content_type})
            self.assertEqual(code, 404)
            self.assertEqual(upload.list_inbox(data), [])

    def test_oversized_upload_is_a_413(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            httpd, port = self.start(tmp)
            token = upload.create_link(httpd.settings_data)
            for path, headers in (
                    (f"/u/{token}", {}),
                    ("/upload", {server.TOKEN_HEADER: ROUTE_TOKEN})):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                try:
                    conn.putrequest("POST", path)
                    conn.putheader("Content-Type",
                                   "multipart/form-data; boundary=x")
                    conn.putheader("Content-Length",
                                   str(upload.MAX_UPLOAD_BYTES + 65537))
                    for name, value in headers.items():
                        conn.putheader(name, value)
                    conn.endheaders()
                    response = conn.getresponse()
                    self.assertEqual(response.status, 413, path)
                    response.read()
                finally:
                    conn.close()
            self.assertEqual(upload.list_inbox(httpd.settings_data), [])


if __name__ == "__main__":
    unittest.main()
