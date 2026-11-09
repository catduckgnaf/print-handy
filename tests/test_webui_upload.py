"""Web UI upload + inbox: files land in the inbox and nowhere else."""
import time
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.core import intake_resources
from print_handy.webui import upload


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


if __name__ == "__main__":
    unittest.main()
