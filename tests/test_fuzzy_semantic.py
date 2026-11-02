"""PRINT_FUZZY_URL endpoint contract tests (#468 stage 4).

semantic_matches() speaks the spike's small contract to an operator's
semantic-search endpoint — POST {query, candidates}, answer {matches,
degraded} — consulted only when a /print-lookup query matched nothing
locally. These tests pin the contract against a loopback stub (the same
stub an operator's Worker would implement), the opt-in gate, and the
degradation rules from docs/SETTINGS.md: every failure returns [] and the
local behaviour stands. Loopback only; nothing leaves this machine.
"""
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from print_handy import knowledge
from print_handy.core import fuzzy_semantic

CANDIDATES = [
    {"id": "symptom:first-layer-not-sticking", "text": "First layer not sticking (bed adhesion)"},
    {"id": "flow:clog", "text": "Nozzle clog"},
]


class _Stub:
    """A loopback endpoint speaking the contract, recording what it was sent.

    status/payload/raw/sleep are read at request time, so a test can change
    the stub's answer between calls.
    """

    def __init__(self, status=200, payload=None, raw=None, sleep=0):
        self.status = status
        self.payload = payload
        self.raw = raw
        self.sleep = sleep
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
                if stub.sleep:
                    time.sleep(stub.sleep)
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
        # The secret lives in the URL path, like PRINT_VISION_URL endpoints.
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/sekrit/fuzzy"

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


@pytest.fixture(scope="module")
def registry():
    return knowledge.load_registry()


def _refused_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/fuzzy"


def test_unset_url_is_not_configured_and_never_networks(monkeypatch):
    monkeypatch.delenv("PRINT_FUZZY_URL", raising=False)
    assert fuzzy_semantic.configured() is False
    with monkeypatch.context() as m:
        m.setattr(fuzzy_semantic.urllib.request, "urlopen",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
        assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []


def test_request_shape_and_matches(stub_factory, monkeypatch):
    stub = stub_factory(payload={"matches": [{"id": "symptom:first-layer-not-sticking",
                                              "text": "First layer not sticking (bed adhesion)",
                                              "score": 0.83}], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    assert fuzzy_semantic.configured() is True
    out = fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES)
    assert out == [{"id": "symptom:first-layer-not-sticking",
                    "text": "First layer not sticking (bed adhesion)", "score": 0.83}]
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["path"] == "/sekrit/fuzzy"
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None  # the secret is in the path; no key is sent
    assert json.loads(request["body"]) == {"query": "bed wont stick", "candidates": CANDIDATES,
                                            "threshold": 0.55, "limit": 5}


def test_degraded_answer_returns_empty(stub_factory, monkeypatch, caplog):
    stub = stub_factory(payload={"matches": [], "degraded": True, "reason": "binding missing"})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    with caplog.at_level(logging.WARNING, logger="print_handy.core.fuzzy_semantic"):
        assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    assert "degraded" in caplog.text


def test_http_error_and_malformed_body_degrade_and_log(stub_factory, monkeypatch, caplog):
    stub = stub_factory(status=500, payload={"matches": [{"id": "flow:clog", "text": "x", "score": 0.9}]})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    with caplog.at_level(logging.WARNING, logger="print_handy.core.fuzzy_semantic"):
        assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    stub.status, stub.raw = 200, b"not json"
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    assert "PRINT_FUZZY_URL" in caplog.text


def test_unreachable_and_slow_endpoints_degrade(stub_factory, monkeypatch):
    monkeypatch.setenv("PRINT_FUZZY_URL", _refused_url())
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    stub = stub_factory(sleep=2, payload={"matches": [], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    monkeypatch.setattr(fuzzy_semantic, "TIMEOUT_S", 0.2)
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []


# ------------------------------------------------------- lookup integration

def test_lookup_text_semantic_suggestions_only_on_a_local_miss(stub_factory, monkeypatch, registry):
    stub = stub_factory(payload={"matches": [{"id": "symptom:first-layer-not-sticking",
                                              "text": "First layer not sticking (bed adhesion)",
                                              "score": 0.83}], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    text = knowledge.lookup_text("bed wont stick", registry=registry)
    assert "Closest by meaning" in text and "First layer will not stick" in text
    assert text.endswith(knowledge.ADVISORY_NOTE)
    assert len(stub.requests) == 1
    sent = json.loads(stub.requests[0]["body"])
    assert sent["query"] == "bed wont stick"
    # Candidates are the registries' public entries only — ids and display
    # names, nothing from this install.
    ids = {c["id"] for c in sent["candidates"]}
    assert "symptom:first-layer-not-sticking" in ids and "flow:clog" in ids
    assert all(set(c) == {"id", "text"} for c in sent["candidates"])


def test_lookup_text_local_hit_never_calls_the_endpoint(stub_factory, monkeypatch, registry):
    stub = stub_factory(payload={"matches": [], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    text = knowledge.lookup_text("clog", registry=registry)
    assert "Nothing in the registries matches" not in text
    assert stub.requests == []


def test_lookup_text_endpoint_failure_keeps_the_plain_no_match_reply(stub_factory, monkeypatch, registry):
    stub = stub_factory(status=500, payload={})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    text = knowledge.lookup_text("bed wont stick", registry=registry)
    assert "Nothing in the registries matches 'bed wont stick'" in text


def test_lookup_text_ignores_match_ids_that_were_not_candidates(stub_factory, monkeypatch, registry):
    stub = stub_factory(payload={"matches": [{"id": "symptom:made-up", "text": "Nope", "score": 0.99}],
                                 "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    text = knowledge.lookup_text("bed wont stick", registry=registry)
    assert "Nothing in the registries matches 'bed wont stick'" in text
    assert "Nope" not in text


def test_lookup_text_without_the_url_set_is_unchanged(monkeypatch, registry):
    monkeypatch.delenv("PRINT_FUZZY_URL", raising=False)
    text = knowledge.lookup_text("bed wont stick", registry=registry)
    assert "Nothing in the registries matches 'bed wont stick'" in text


# ------------------------------------------- hardening: hostile payloads

@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"),
                                   "0.9", True, None, [0.9], {"s": 0.9}])
def test_hostile_scores_are_rejected_and_degrade(stub_factory, monkeypatch, score):
    # json.dumps emits the non-standard NaN/Infinity literals for the
    # float cases, exactly what a buggy or hostile endpoint can send.
    stub = stub_factory(payload={"matches": [{"id": "flow:clog", "text": "Nozzle clog",
                                              "score": score}], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []


def test_nan_score_no_longer_crashes_the_lookup(stub_factory, monkeypatch, registry):
    """F1 regression: before the hardening, round(NaN * 100) raised inside
    _semantic_lookup and the dispatcher answered "Lookup failed"."""
    stub = stub_factory(payload={"matches": [{"id": "symptom:first-layer-not-sticking",
                                              "text": "x", "score": float("nan")}], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    text = knowledge.lookup_text("bed wont stick", registry=registry)
    assert "Nothing in the registries matches 'bed wont stick'" in text


def test_hostile_entry_does_not_sink_a_valid_match(stub_factory, monkeypatch):
    stub = stub_factory(payload={"matches": [
        {"id": "symptom:first-layer-not-sticking", "text": "x", "score": float("inf")},
        {"id": "flow:clog", "text": "Nozzle clog", "score": 0.9}], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    out = fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES)
    assert [m["id"] for m in out] == ["flow:clog"]


def test_threshold_is_enforced_deduped_and_order_is_deterministic(stub_factory, monkeypatch):
    stub = stub_factory(payload={"matches": [
        {"id": "flow:clog", "text": "Nozzle clog", "score": 0.6},
        {"id": "symptom:first-layer-not-sticking", "text": "x", "score": 0.9},
        {"id": "flow:clog", "text": "Nozzle clog", "score": 0.8},   # duplicate: best wins
        {"id": "flow:other", "text": "x", "score": 0.4},            # below the sent threshold
    ], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    out = fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES)
    assert [(m["id"], m["score"]) for m in out] == [
        ("symptom:first-layer-not-sticking", 0.9), ("flow:clog", 0.8)]


def test_out_of_range_scores_are_clamped(stub_factory, monkeypatch):
    stub = stub_factory(payload={"matches": [{"id": "flow:clog", "text": "x", "score": 1.7}],
                                 "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    out = fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES)
    assert out[0]["score"] == 1.0


def test_oversized_response_body_degrades(stub_factory, monkeypatch):
    raw = b'{"matches": [], "degraded": false, "pad": "' + b"x" * (300 * 1024) + b'"}'
    stub = stub_factory(raw=raw)
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []


def test_slow_drip_response_hits_the_total_deadline(monkeypatch):
    """Each drip byte arrives well inside any per-read timeout, so only a
    total deadline can cut this off (F4)."""
    import socket as _socket

    server = _socket.socket()
    server.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def drip():
        conn, _ = server.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 100000\r\n\r\n{")
        try:
            for _ in range(40):
                conn.sendall(b"x")
                time.sleep(0.15)
        except OSError:
            pass
        conn.close()
        server.close()

    threading.Thread(target=drip, daemon=True).start()
    monkeypatch.setenv("PRINT_FUZZY_URL", f"http://127.0.0.1:{port}/sekrit/fuzzy")
    monkeypatch.setattr(fuzzy_semantic, "TIMEOUT_S", 0.6)
    start = time.monotonic()
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    assert time.monotonic() - start < 2.0


def test_non_http_schemes_are_a_configuration_error(monkeypatch, caplog):
    monkeypatch.setenv("PRINT_FUZZY_URL", "ftp://127.0.0.1:9/fuzzy")
    with caplog.at_level(logging.ERROR, logger="print_handy.core.fuzzy_semantic"):
        assert fuzzy_semantic.configured() is False
        assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    assert "http or https" in caplog.text


def test_non_local_plain_http_warns_about_cleartext(monkeypatch, caplog):
    # A public-range address (the TEST-NET ranges read as "private" to
    # ipaddress, so they cannot play this part); the warning is logged at
    # validation time, before the doomed connection attempt.
    monkeypatch.setenv("PRINT_FUZZY_URL", "http://8.8.8.8:81/fuzzy")
    monkeypatch.setattr(fuzzy_semantic, "TIMEOUT_S", 0.3)
    with caplog.at_level(logging.WARNING, logger="print_handy.core.fuzzy_semantic"):
        assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    assert "cleartext" in caplog.text


def test_local_plain_http_does_not_warn(stub_factory, monkeypatch, caplog):
    stub = stub_factory(payload={"matches": [], "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)  # 127.0.0.1 loopback
    with caplog.at_level(logging.WARNING, logger="print_handy.core.fuzzy_semantic"):
        assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
    assert "cleartext" not in caplog.text


def test_non_list_matches_degrades_to_local_result(stub_factory, monkeypatch):
    # P2-223: valid JSON with an invalid shape — "matches" must be a list.
    # Processing it used to raise TypeError outside the failure boundary,
    # so /print-lookup answered "the registries could not be read" instead
    # of silently keeping the local no-match answer.
    stub = stub_factory(payload={"matches": 42, "degraded": False})
    monkeypatch.setenv("PRINT_FUZZY_URL", stub.url)
    assert fuzzy_semantic.semantic_matches("bed wont stick", CANDIDATES) == []
