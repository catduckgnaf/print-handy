"""#708 camera snapshot proxy: GET /api/printers/<id>/camera/snapshot.

One JPEG from the printer's existing camera source; the camera URL (which
can carry credentials) is never echoed. Offline: fake fetch and adapters.
"""
import tempfile
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import camera

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body"
SECRET_URL = "http://viewer:camera-secret-42@192.0.2.10/snapshot.jpg"


def load(extra=""):
    tmp = tempfile.mkdtemp()
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={})


class NoOwnCamera:
    """An adapter that takes no stills of its own (the Moonraker families)."""


class OwnCamera:
    def __init__(self, image):
        self.image = image

    def camera_snapshot(self):
        return self.image


def no_fanout(view):
    return None


class CameraSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.data = load(f"services:\n  camera_snapshot_url: {SECRET_URL}\n")
        self.printer_id = settings.default_printer_id(self.data)

    def test_direct_url_returns_the_jpeg(self):
        seen = []
        code, body, content_type = camera.snapshot(
            self.data, self.printer_id, adapter=NoOwnCamera(),
            fetch=lambda url: seen.append(url) or JPEG, fanout_url=no_fanout)
        self.assertEqual((code, body, content_type), (200, JPEG, "image/jpeg"))
        self.assertEqual(seen, [SECRET_URL])

    def test_adapter_stills_are_used_first(self):
        code, body, _ = camera.snapshot(self.data, self.printer_id, adapter=OwnCamera(JPEG),
                                        fetch=lambda url: self.fail("fetched"), fanout_url=no_fanout)
        self.assertEqual((code, body), (200, JPEG))

    def test_go2rtc_fanout_first_then_direct_fallback(self):
        seen = []

        def fetch(url):
            seen.append(url)
            if url == "http://127.0.0.1:1984/frame.jpeg":
                raise OSError("fan-out down")
            return JPEG

        code, _, _ = camera.snapshot(self.data, self.printer_id, adapter=NoOwnCamera(), fetch=fetch,
                                     fanout_url=lambda view: "http://127.0.0.1:1984/frame.jpeg")
        self.assertEqual(code, 200)
        self.assertEqual(seen, ["http://127.0.0.1:1984/frame.jpeg", SECRET_URL])

    def test_unreachable_camera_never_echoes_the_url(self):
        def fetch(url):
            raise OSError(f"connection refused to {url}")

        code, body, content_type = camera.snapshot(self.data, self.printer_id, adapter=NoOwnCamera(),
                                                   fetch=fetch, fanout_url=no_fanout)
        self.assertEqual((code, content_type), (502, "application/json"))
        self.assertNotIn("camera-secret-42", str(body))
        self.assertIn("OSError", body["reason"])

    def test_non_jpeg_answer_is_refused(self):
        code, body, _ = camera.snapshot(self.data, self.printer_id, adapter=OwnCamera(b"<html>"),
                                        fanout_url=no_fanout)
        self.assertEqual(code, 502)
        self.assertIn("not a JPEG", body["reason"])

    def test_unknown_printer_is_404(self):
        code, body, _ = camera.snapshot(self.data, "no-such-printer", adapter=NoOwnCamera(),
                                        fanout_url=no_fanout)
        self.assertEqual(code, 404)
        self.assertIn("no-such-printer", body["reason"])

    def test_no_camera_configured_is_404(self):
        data = load()
        code, body, _ = camera.snapshot(data, settings.default_printer_id(data),
                                        adapter=NoOwnCamera(), fanout_url=no_fanout)
        self.assertEqual(code, 404)
        self.assertIn("camera_snapshot_url is empty", body["reason"])

    def test_camera_source_none_is_404(self):
        data = load(f"services:\n  camera_snapshot_url: {SECRET_URL}\n  camera_source: none\n")
        code, body, _ = camera.snapshot(data, settings.default_printer_id(data),
                                        adapter=OwnCamera(JPEG), fanout_url=no_fanout)
        self.assertEqual(code, 404)
        self.assertIn("camera images are off", body["reason"])


if __name__ == "__main__":
    unittest.main()
