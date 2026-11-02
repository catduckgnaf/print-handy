"""PRINT_VISION_URL endpoint contract tests (#501).

visual_verdict() speaks a deliberately small contract to a custom vision
endpoint — POST {image, prompt}, answer {verdict, reason} — so an operator
can wire an external detector without any Print Handy changes. These tests
pin that contract against a loopback stub speaking it (the same stub an
operator's sidecar would implement), plus the degradation rules from
docs/SETTINGS.md: every failure becomes "uncertain", and the underlying
error is logged even though the returned detail stays generic.
Loopback only; nothing leaves this machine.
"""
import base64
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from print_handy.hermes import visual_watch as vw

FRAME = bytes((0xFF, 0xD8, 0xFF, 0xD9))  # the verdict path never decodes the frame
PROMPT = "Is there plastic on the bed under the nozzle, or is there spaghetti/detachment?"
FAILED = ("uncertain", "the vision check could not be completed")


class _Stub:
    """A loopback endpoint speaking the contract, recording what it was sent.

    status/payload/raw are read at request time, so a test can change the
    stub's answer between calls.
    """

    def __init__(self, status=200, payload=None, raw=None):
        self.status = status
        self.payload = payload
        self.raw = raw
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({
                    "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "authorization": self.headers.get("Authorization"),
                    "body": self.rfile.read(length),
                })
                body = stub.raw
                if body is None:
                    body = json.dumps(stub.payload if stub.payload is not None else {}).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/judge"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub_factory():
    stubs = []

    def make(**kwargs):
        stub = _Stub(**kwargs)
        stubs.append(stub)
        return stub

    yield make
    for stub in stubs:
        stub.stop()


def _refused_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/judge"


def _verdict(monkeypatch, url, image=FRAME, prompt=PROMPT):
    """visual_verdict on the PRINT_VISION_URL route.

    The OpenAI-route arguments are callables that fail the test if they are
    consulted: on this route chat_url/api_key must never be called.
    """
    monkeypatch.setenv("PRINT_VISION_URL", url)

    def unused(name):
        def call(*args):
            raise AssertionError(f"{name} consulted on the PRINT_VISION_URL route")
        return call

    return vw.visual_verdict(image, prompt, chat_url=unused("chat_url"), model="unused-model",
                             api_key=unused("api_key"), context="unused context")


def test_request_shape_and_verdict(stub_factory, monkeypatch):
    stub = stub_factory(payload={"verdict": "spaghetti", "reason": "tangled mess"})
    assert _verdict(monkeypatch, stub.url) == ("spaghetti", "tangled mess")
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["path"] == "/judge"
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None  # no API key is sent on this route
    assert json.loads(request["body"]) == {
        "image": base64.b64encode(FRAME).decode(),  # raw JPEG bytes, no data: prefix
        "prompt": PROMPT,
    }


def test_verdict_vocabulary_passes_through_casefolded(stub_factory, monkeypatch):
    stub = stub_factory(payload={"verdict": "OK", "reason": ""})
    for verdict in ("ok", "air", "spaghetti", "detached", "empty_idle", "uncertain"):
        stub.payload = {"verdict": verdict.upper(), "reason": "r"}
        assert _verdict(monkeypatch, stub.url) == (verdict, "r"), verdict


def test_out_of_vocabulary_verdicts_normalize_to_uncertain(stub_factory, monkeypatch):
    # The verdict contract is VISUAL_VERDICTS; anything else the model
    # emits — a new word, a misspelling, drift like "okay" — must fail
    # closed as "uncertain" (which alerts), never pass through to be
    # silently treated as safe by a caller's suspicious-verdict set.
    stub = stub_factory(payload={"verdict": "okay", "reason": "looks fine"})
    for verdict in ("okay", "fine", "detatchd", "safe", "no", "unsafe", ""):
        stub.payload = {"verdict": verdict, "reason": "r"}
        assert _verdict(monkeypatch, stub.url) == ("uncertain", "r"), verdict


def test_missing_response_fields_default(stub_factory, monkeypatch):
    stub = stub_factory(payload={})
    assert _verdict(monkeypatch, stub.url) == ("uncertain", "")
    stub.payload = {"verdict": "ok"}
    assert _verdict(monkeypatch, stub.url) == ("ok", "")


def test_reason_is_truncated_to_300_characters(stub_factory, monkeypatch):
    stub = stub_factory(payload={"verdict": "air", "reason": "x" * 400})
    verdict, reason = _verdict(monkeypatch, stub.url)
    assert verdict == "air" and reason == "x" * 300


def test_http_error_status_degrades_to_uncertain_and_logs(stub_factory, monkeypatch, caplog):
    stub = stub_factory(status=500, payload={"verdict": "ok", "reason": "fine"})
    with caplog.at_level(logging.WARNING, logger="print_handy.hermes.visual_watch"):
        assert _verdict(monkeypatch, stub.url) == FAILED
    assert "vision check failed via PRINT_VISION_URL" in caplog.text
    assert "HTTP Error 500" in caplog.text  # the underlying error is diagnosable


def test_non_json_body_degrades_to_uncertain(stub_factory, monkeypatch):
    stub = stub_factory(raw=b"<html>not json</html>")
    assert _verdict(monkeypatch, stub.url) == FAILED


def test_non_object_json_degrades_to_uncertain(stub_factory, monkeypatch):
    stub = stub_factory(raw=b'["ok"]')
    assert _verdict(monkeypatch, stub.url) == FAILED


def test_unreachable_endpoint_degrades_to_uncertain_and_logs(monkeypatch, caplog):
    with caplog.at_level(logging.WARNING, logger="print_handy.hermes.visual_watch"):
        assert _verdict(monkeypatch, _refused_url()) == FAILED
    assert "vision check failed via PRINT_VISION_URL" in caplog.text
    assert "urlopen error" in caplog.text


def test_request_timeout_is_30_seconds(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"verdict": "ok", "reason": ""}).encode()

    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(vw.urllib.request, "urlopen", fake_open)
    assert _verdict(monkeypatch, "http://detector.local/judge") == ("ok", "")
    assert seen == {"url": "http://detector.local/judge", "timeout": 30}
