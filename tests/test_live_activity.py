"""Offline tests for the Discord Activity core (issue #311, stage 3).

No network and no Discord: the OAuth transport is a fake that records
what it was handed, and the HTTP surface runs on an ephemeral loopback
port. The redaction tests prove the OAuth code and the access token
appear in no error, no result and no response.
"""
import http.client
import json
import threading
import time

import pytest

from print_handy import live_activity as la

CODE = "oauth-code-SECRET-1"
TOKEN = "access-token-SECRET-2"
SECRET = b"session-signing-secret"
ALLOWLIST = ("42", "43")


class FakeTransport:
    """Records the code/token it saw; answers like Discord would."""

    def __init__(self, user=None, exchange_error=None, user_error=None):
        self.user = {"id": "42", "username": "operator"} if user is None else user
        self.exchange_error = exchange_error
        self.user_error = user_error
        self.codes = []
        self.tokens = []

    def exchange_code(self, code):
        self.codes.append(code)
        if self.exchange_error is not None:
            raise self.exchange_error
        return TOKEN

    def fetch_user(self, access_token):
        self.tokens.append(access_token)
        if self.user_error is not None:
            raise self.user_error
        return self.user


# --------------------------------------------------------------------- OAuth allowlist

def test_authenticate_allows_a_listed_user_and_hides_the_token():
    transport = FakeTransport()
    result = la.authenticate_code(CODE, transport=transport, allowlist=ALLOWLIST)
    assert result.user_id == "42" and result.username == "operator"
    assert transport.codes == [CODE] and transport.tokens == [TOKEN]
    assert TOKEN not in repr(result) and CODE not in repr(result)


def test_authenticate_denies_an_unlisted_user():
    transport = FakeTransport(user={"id": "999", "username": "stranger"})
    with pytest.raises(la.ActivityAuthError, match="allowlist"):
        la.authenticate_code(CODE, transport=transport, allowlist=ALLOWLIST)


def test_authenticate_denies_by_default_on_every_failure():
    cases = [
        FakeTransport(exchange_error=OSError("down")),
        FakeTransport(user_error=OSError("down")),
        FakeTransport(user={"username": "no-id"}),
    ]
    for transport in cases:
        with pytest.raises(la.ActivityAuthError):
            la.authenticate_code(CODE, transport=transport, allowlist=ALLOWLIST)
    with pytest.raises(la.ActivityAuthError):
        la.authenticate_code("", transport=FakeTransport(), allowlist=ALLOWLIST)
    with pytest.raises(la.ActivityAuthError):
        la.authenticate_code(CODE, transport=FakeTransport(), allowlist=())


def test_authenticate_errors_never_carry_the_code_or_token():
    for transport in (FakeTransport(exchange_error=RuntimeError(f"boom {CODE} {TOKEN}")),
                      FakeTransport(user_error=RuntimeError(f"boom {TOKEN}"))):
        with pytest.raises(la.ActivityAuthError) as caught:
            la.authenticate_code(CODE, transport=transport, allowlist=ALLOWLIST)
        assert CODE not in str(caught.value) and TOKEN not in str(caught.value)


def test_urllib_transport_posts_the_exchange_and_reads_the_profile():
    seen = {}

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def opener(request, timeout=None):
        seen[request.full_url] = request
        if request.full_url == la.TOKEN_URL:
            return FakeResponse({"access_token": TOKEN})
        return FakeResponse({"id": "42", "username": "operator"})

    transport = la.UrllibOAuthTransport("123", "client-secret", opener=opener)
    assert transport.exchange_code(CODE) == TOKEN
    body = seen[la.TOKEN_URL].data.decode()
    assert "grant_type=authorization_code" in body and f"code={CODE}" in body
    user = transport.fetch_user(TOKEN)
    assert user["id"] == "42"
    assert seen[la.USER_URL].headers["Authorization"] == f"Bearer {TOKEN}"


def test_client_secret_comes_from_the_named_environment_variable():
    assert la.client_secret({"DISCORD_CLIENT_SECRET": "s3cret"}) == "s3cret"
    assert la.client_secret({}) == ""
    assert la.session_secret({"LIVE_STREAM_SESSION_SECRET": "abc"}) == b"abc"


# --------------------------------------------------------------------- session markers

def test_session_token_round_trips_and_expires():
    marker = la.make_session_token("42", secret=SECRET, now=1000.0, ttl_s=60)
    assert la.verify_session_token(marker, secret=SECRET, now=1000.0) == "42"
    assert la.verify_session_token(marker, secret=SECRET, now=1059.0) == "42"
    assert la.verify_session_token(marker, secret=SECRET, now=1060.0) is None


def test_session_token_rejects_tampering_wrong_secret_and_garbage():
    marker = la.make_session_token("42", secret=SECRET, now=1000.0)
    user_id, expiry, signature = marker.split(".")
    assert la.verify_session_token(f"43.{expiry}.{signature}", secret=SECRET, now=1000.0) is None
    assert la.verify_session_token(marker, secret=b"other", now=1000.0) is None
    assert la.verify_session_token("garbage", secret=SECRET, now=1000.0) is None
    assert la.verify_session_token("", secret=SECRET, now=1000.0) is None
    assert la.verify_session_token(marker, secret=b"", now=1000.0) is None
    with pytest.raises(la.ActivityAuthError):
        la.make_session_token("42", secret=b"", now=1000.0)


# --------------------------------------------------------------------- the page

def test_page_is_self_contained_and_speaks_go2rtc_mse():
    page = la.ACTIVITY_PAGE
    assert "http://" not in page and "https://" not in page
    assert "/api/ws?src=" in page and '"mse"' in page
    assert 'id="status"' in page and "camera feed" in page.lower()
    assert la.DEFAULT_STREAM_NAME in page


# --------------------------------------------------------------------- the sign-in bootstrap

def test_bootstrap_page_runs_the_sdk_authorize_flow_and_nothing_else():
    # The 2026-10-30 #311 status check's blocker 1: the Activity could
    # never sign anyone in, because the only page was behind the session
    # it was supposed to create. The bootstrap page is the unauthenticated
    # start: it loads the vendored SDK from this same origin, runs
    # authorize() for a one-time code, posts the code (and only the code)
    # to /api/token, then reloads / so the session cookie the exchange
    # sets brings back the real page. Nothing external, no feed, no
    # stream name: a leaked bootstrap teaches an attacker nothing.
    page = la.BOOTSTRAP_PAGE
    assert "http://" not in page and "https://" not in page
    assert f'from "{la.SDK_ROUTE}"' in page and "DiscordSDK" in page
    assert "authorize" in page and '"/api/token"' in page
    assert '"identify"' in page
    assert "/api/ws" not in page and la.DEFAULT_STREAM_NAME not in page
    assert CODE not in page and TOKEN not in page


def test_bootstrap_page_carries_the_configured_client_id_only():
    served = la.bootstrap_page_for("700000000000000001")
    assert '"700000000000000001"' in served
    assert "__CLIENT_ID_JSON__" not in served
    # No client id configured: the page still serves and says so in its
    # own flow (the JS refuses before touching the SDK), never crashes.
    assert '""' in la.bootstrap_page_for("")


def test_sdk_bundle_is_the_pinned_vendored_sdk():
    # The SDK is vendored, not fetched: the CSP allows no external hosts,
    # so the bundle ships in the package at a recorded pin. The header
    # names the pin; the bundle is the SDK's own single-file build.
    bundle = la.sdk_bundle()
    assert bundle.startswith(b"/*")
    header = bundle[:800].decode("utf-8", "replace")
    assert "@discord/embedded-app-sdk" in header and la.SDK_VERSION in header
    assert "MIT" in header
    assert b"DiscordSDK" in bundle and len(bundle) > 100_000


def test_sdk_route_serves_the_bundle_and_the_bootstrap_csp_allows_it(server):
    instance, _ = server
    status, headers, payload = _request(instance, "GET", la.SDK_ROUTE)
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert payload == la.sdk_bundle()
    # The SDK route is open like /health: it is public library code and
    # reveals no configuration (the client id is not in it).
    assert b"700000000000000001" not in payload
    status, headers, _ = _request(instance, "GET", "/")
    assert status == 200
    csp = headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "connect-src 'self'" in csp
    # The authenticated feed page keeps its stricter inline-only CSP.
    _, headers, _ = _request(
        instance, "POST", "/api/token", body=json.dumps({"code": CODE}),
        headers={"Content-Type": "application/json"})
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    _, headers, _ = _request(instance, "GET", "/", headers={"Cookie": cookie})
    assert "script-src 'unsafe-inline'" in headers["Content-Security-Policy"]


# --------------------------------------------------------------------- the HTTP surface

@pytest.fixture()
def server():
    transport = FakeTransport()
    instance = la.make_server("127.0.0.1", 0, transport=transport,
                              allowlist=ALLOWLIST, secret=SECRET,
                              client_id="700000000000000001")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, transport
    instance.shutdown()
    instance.server_close()


def _request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = (response.status, dict(response.getheaders()), payload)
    connection.close()
    return result


def test_health_is_open_and_reveals_nothing(server):
    instance, _ = server
    status, _, payload = _request(instance, "GET", "/health")
    assert status == 200 and json.loads(payload) == {"ok": True, "service": "print-handy-live"}


def test_page_requires_a_session_and_the_exchange_sets_one(server):
    instance, _ = server
    # Without a session, / serves the sign-in bootstrap, never the feed
    # page: the bootstrap is the only unauthenticated page, and it can
    # do nothing but run the authorize -> /api/token flow below.
    status, _, payload = _request(instance, "GET", "/")
    assert status == 200
    assert payload.decode() == la.bootstrap_page_for("700000000000000001")
    assert payload.decode() != la.ACTIVITY_PAGE
    status, headers, payload = _request(
        instance, "POST", "/api/token", body=json.dumps({"code": CODE}),
        headers={"Content-Type": "application/json"})
    assert status == 200 and json.loads(payload)["user_id"] == "42"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{la.SESSION_COOKIE}=") and "HttpOnly" in cookie
    marker = cookie.split(";", 1)[0].split("=", 1)[1]
    assert la.verify_session_token(marker, secret=SECRET, now=time.time()) == "42"
    status, headers, payload = _request(
        instance, "GET", "/", headers={"Cookie": f"{la.SESSION_COOKIE}={marker}"})
    assert status == 200 and payload.decode() == la.ACTIVITY_PAGE
    assert "Content-Security-Policy" in headers


def test_exchange_refusals_are_403_and_never_echo_secrets(server):
    instance, _ = server
    status, _, payload = _request(
        instance, "POST", "/api/token", body=json.dumps({"code": ""}),
        headers={"Content-Type": "application/json"})
    assert status == 403
    assert CODE.encode() not in payload and TOKEN.encode() not in payload


def test_unknown_paths_and_methods(server):
    instance, _ = server
    assert _request(instance, "GET", "/nope")[0] == 404
    assert _request(instance, "POST", "/nope", body="{}")[0] == 404
    assert _request(instance, "PUT", "/")[0] == 405


# --------------------------------------------------------------------- presence callbacks

def test_presence_callbacks_fire_on_exchange_page_and_leave():
    seen, left = [], []
    transport = FakeTransport()
    instance = la.make_server("127.0.0.1", 0, transport=transport,
                              allowlist=ALLOWLIST, secret=SECRET,
                              on_viewer=seen.append, on_leave=left.append)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, headers, _ = _request(
            instance, "POST", "/api/token", body=json.dumps({"code": CODE}),
            headers={"Content-Type": "application/json"})
        assert status == 200 and seen == ["42"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, _ = _request(instance, "GET", "/", headers={"Cookie": cookie})
        assert status == 200 and seen == ["42", "42"]
        status, _, _ = _request(instance, "POST", "/api/leave", headers={"Cookie": cookie})
        assert status == 200 and left == ["42"]
        # The leave beacon without a session is refused, like the page.
        assert _request(instance, "POST", "/api/leave")[0] == 403
    finally:
        instance.shutdown()
        instance.server_close()


# --------------------------------------------------------------------- the feed proxy

import socket as _socket  # noqa: E402 — grouped with the proxy fixtures


class FakeGo2rtc(threading.Thread):
    """A one-shot fake go2rtc /api/ws: answers the upgrade, sends a frame,
    and echoes back whatever the client sends (the proxy is a byte pipe)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.listener = _socket.socket()
        self.listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.request_head = b""

    def run(self):
        connection, _ = self.listener.accept()
        with connection:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.request_head = head
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                               b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            connection.sendall(b"\x82\x03fmp")
            echo = connection.recv(4096)
            if echo:
                connection.sendall(b"echo:" + echo)
            time.sleep(0.2)


@pytest.fixture()
def feed_server():
    upstream = FakeGo2rtc()
    upstream.start()
    instance = la.make_server("127.0.0.1", 0, transport=FakeTransport(),
                              allowlist=ALLOWLIST, secret=SECRET,
                              feed_upstream=("127.0.0.1", upstream.port))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, upstream
    instance.shutdown()
    instance.server_close()
    upstream.join(timeout=5)


def _ws_handshake(server, path, cookie=None, extra_headers=()):
    """Returns (connection, head, rest): `rest` is whatever arrived with the
    headers (a fast upstream's first frame can share the segment, so a
    test must not expect it from a later recv)."""
    connection = _socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    lines = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1", "Upgrade: websocket",
             "Connection: Upgrade", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
             "Sec-WebSocket-Version: 13"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    lines.extend(extra_headers)
    connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = connection.recv(4096)
        assert chunk, "server closed during the handshake"
        head += chunk
    head, _, rest = head.partition(b"\r\n\r\n")
    return connection, head + b"\r\n\r\n", rest


def _session_cookie(instance):
    status, headers, _ = _request(
        instance, "POST", "/api/token", body=json.dumps({"code": CODE}),
        headers={"Content-Type": "application/json"})
    assert status == 200
    return headers["Set-Cookie"].split(";", 1)[0]


def test_feed_proxy_requires_a_session_and_the_one_stream(feed_server):
    instance, upstream = feed_server
    # No session: refused before anything reaches the upstream.
    connection, head, _ = _ws_handshake(instance, "/api/ws?src=camera")
    assert b"403" in head.split(b"\r\n", 1)[0]
    connection.close()
    # A stream that is not the one published name: also refused.
    cookie = _session_cookie(instance)
    connection, head, _ = _ws_handshake(instance, "/api/ws?src=other", cookie=cookie)
    assert b"404" in head.split(b"\r\n", 1)[0]
    connection.close()
    assert upstream.request_head == b""


def test_feed_proxy_relays_the_handshake_and_frames(feed_server):
    instance, upstream = feed_server
    cookie = _session_cookie(instance)
    connection, head, rest = _ws_handshake(instance, "/api/ws?src=camera", cookie=cookie)
    try:
        assert b"101" in head.split(b"\r\n", 1)[0]
        frame = rest or connection.recv(4096)
        assert frame == b"\x82\x03fmp"  # the upstream's frame, byte for byte
        connection.sendall(b"mse-offer")
        assert connection.recv(4096) == b"echo:mse-offer"
    finally:
        connection.close()
    # The upstream saw the rebuilt request: the published stream path and
    # no session cookie (go2rtc never learns the marker).
    assert b"GET /api/ws?src=camera" in upstream.request_head
    assert b"Cookie" not in upstream.request_head


def test_transcoded_stream_name_drives_the_page_default_and_the_proxy():
    # 2.2.2: when a [live_stream] transcode value is set, go2rtc
    # publishes the transcoded stream under its own name and the
    # Activity server is started with that name: the page asks for it
    # by default and the proxy serves only it — the raw name is refused.
    upstream = FakeGo2rtc()
    upstream.start()
    instance = la.make_server("127.0.0.1", 0, transport=FakeTransport(),
                              allowlist=ALLOWLIST, secret=SECRET,
                              feed_upstream=("127.0.0.1", upstream.port),
                              stream_name="camera-live")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        cookie = _session_cookie(instance)
        status, _, page = _request(instance, "GET", "/",
                                   headers={"Cookie": cookie})
        assert status == 200
        assert '|| "camera-live"' in page.decode()
        connection, head, _ = _ws_handshake(instance, "/api/ws?src=camera", cookie=cookie)
        assert b"404" in head.split(b"\r\n", 1)[0]
        connection.close()
        connection, head, rest = _ws_handshake(instance, "/api/ws?src=camera-live",
                                               cookie=cookie)
        try:
            assert b"101" in head.split(b"\r\n", 1)[0]
            assert (rest or connection.recv(4096)) == b"\x82\x03fmp"
        finally:
            connection.close()
        assert b"GET /api/ws?src=camera-live" in upstream.request_head
    finally:
        instance.shutdown()
        instance.server_close()
        upstream.join(timeout=5)


def test_feed_proxy_rewrites_the_browser_origin_for_the_loopback_hop(feed_server):
    # P1-88: through the tunnel the browser's Origin is the public
    # origin, while the proxy rebuilds Host as the loopback upstream.
    # go2rtc (api.origin unset) refuses an Origin whose host differs
    # from Host, so forwarding the browser Origin verbatim earns an
    # upstream 403; the proxy must rewrite it to the loopback origin.
    instance, upstream = feed_server
    cookie = _session_cookie(instance)
    connection, head, rest = _ws_handshake(
        instance, "/api/ws?src=camera", cookie=cookie,
        extra_headers=("Origin: https://tunnel.example.invalid",))
    try:
        assert b"101" in head.split(b"\r\n", 1)[0]
        assert (rest or connection.recv(4096)) == b"\x82\x03fmp"
    finally:
        connection.close()
    assert b"tunnel.example.invalid" not in upstream.request_head
    assert (f"Origin: http://127.0.0.1:{upstream.port}".encode()
            in upstream.request_head)


def _session_feed_server(session_active):
    upstream = FakeGo2rtc()
    upstream.start()
    instance = la.make_server("127.0.0.1", 0, transport=FakeTransport(),
                              allowlist=ALLOWLIST, secret=SECRET,
                              feed_upstream=("127.0.0.1", upstream.port),
                              session_active=session_active)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    return instance, upstream


def test_feed_proxy_refuses_a_new_connection_when_the_session_is_not_live():
    # P1-89: a valid cookie alone must not open the feed while the
    # shared viewing session is idle (never started, stopped, expired
    # or ended with the print). The refusal happens before the upstream
    # is contacted, and a session check that fails is a refusal too
    # (fail closed: the privacy gate never fails open).
    for session_active in (lambda: False,
                           lambda: (_ for _ in ()).throw(RuntimeError("record gone"))):
        instance, upstream = _session_feed_server(session_active)
        try:
            cookie = _session_cookie(instance)
            connection, head, _ = _ws_handshake(instance, "/api/ws?src=camera",
                                                cookie=cookie)
            assert b"403" in head.split(b"\r\n", 1)[0]
            connection.close()
            assert upstream.request_head == b""
        finally:
            instance.shutdown()
            instance.server_close()
            upstream.join(timeout=5)


class StreamingGo2rtc(threading.Thread):
    """A fake go2rtc that keeps the feed open, sending a frame every
    0.1 s until the socket dies — the shape a real relay has while a
    viewer is watching."""

    def __init__(self):
        super().__init__(daemon=True)
        self.listener = _socket.socket()
        self.listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.request_head = b""

    def run(self):
        connection, _ = self.listener.accept()
        with connection:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                head += chunk
            self.request_head = head
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                               b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            try:
                while True:
                    connection.sendall(b"\x82\x03fmp")
                    time.sleep(0.1)
            except OSError:
                return


def test_feed_proxy_closes_an_open_relay_when_the_session_ends():
    # P1-89: ending the viewing session (stop, expiry, print end) must
    # end the frames too — an already-open relay is closed by the proxy
    # once the session is no longer live, not left running until the
    # viewer happens to disconnect.
    live = {"on": True}
    upstream = StreamingGo2rtc()
    upstream.start()
    instance = la.make_server("127.0.0.1", 0, transport=FakeTransport(),
                              allowlist=ALLOWLIST, secret=SECRET,
                              feed_upstream=("127.0.0.1", upstream.port),
                              session_active=lambda: live["on"])
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        cookie = _session_cookie(instance)
        connection, head, rest = _ws_handshake(instance, "/api/ws?src=camera",
                                               cookie=cookie)
        try:
            assert b"101" in head.split(b"\r\n", 1)[0]
            assert (rest or connection.recv(4096)) == b"\x82\x03fmp"
            live["on"] = False
            connection.settimeout(5)
            closed = False
            try:
                while True:
                    if not connection.recv(4096):
                        closed = True
                        break
            except ConnectionResetError:
                closed = True
            except _socket.timeout:
                closed = False
            assert closed, "the relay kept running after the session ended"
        finally:
            connection.close()
    finally:
        instance.shutdown()
        instance.server_close()
        upstream.join(timeout=5)


def test_feed_proxy_reports_a_dead_upstream_as_502():
    instance = la.make_server("127.0.0.1", 0, transport=FakeTransport(),
                              allowlist=ALLOWLIST, secret=SECRET,
                              feed_upstream=("127.0.0.1", 1))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        cookie = _session_cookie(instance)
        connection, head, _ = _ws_handshake(instance, "/api/ws?src=camera", cookie=cookie)
        assert b"502" in head.split(b"\r\n", 1)[0]
        connection.close()
    finally:
        instance.shutdown()
        instance.server_close()


# --------------------------------------------------------------------- P2-122: the handshake waits for both

def test_page_sends_the_mse_handshake_only_once_both_sides_are_open():
    # P2-122: sourceopen can fire while the WebSocket is still
    # CONNECTING, and ws.send then throws InvalidStateError — with no
    # retry the feed never starts. The handshake must be gated on both
    # the socket's open event and the MediaSource's sourceopen.
    import re

    page = la.ACTIVITY_PAGE
    sourceopen = re.search(r'addEventListener\("sourceopen".*?\}\);', page, re.S)
    assert sourceopen, "the page lost its sourceopen handler"
    assert "ws.send" not in sourceopen.group(0), \
        "the sourceopen handler still sends on the socket unconditionally"
    assert "ws.onopen" in page or 'addEventListener("open"' in page, \
        "the page never listens for the WebSocket open event"
    assert page.count('ws.send(JSON.stringify({type: "mse"') == 1


# --------------------------------------------------------------------- P2-142: one-way relay survival

def test_pump_preserves_the_relay_when_only_the_camera_sends(monkeypatch):
    # P2-142: during MSE playback the page sends its offer once and
    # then only receives. A client direction silent for the whole
    # stream must not tear the relay down while frames keep flowing.
    import select as select_module
    import socket

    real_select = select_module.select

    def capped(rlist, wlist, xlist, timeout=None):
        # Compress the pump's idle waits so the pre-fix 60 s client
        # silence cutoff fires inside the test's time budget.
        return real_select(rlist, wlist, xlist,
                           0.05 if timeout is None else min(timeout, 0.05))

    monkeypatch.setattr(select_module, "select", capped)
    browser, client = socket.socketpair()
    go2rtc, upstream = socket.socketpair()
    pump = threading.Thread(target=la._pump, args=(client, upstream), daemon=True)
    pump.start()
    try:
        for index in range(10):
            go2rtc.sendall(b"frame-%d;" % index)
            time.sleep(0.1)
        assert pump.is_alive(), \
            "the relay died while the camera was still sending frames"
        browser.settimeout(2)
        received = b""
        while received.count(b"frame-") < 10:
            chunk = browser.recv(65536)
            assert chunk, "the relay closed the browser side mid-stream"
            received += chunk
    finally:
        go2rtc.close()
        pump.join(timeout=5)
        browser.close()
    assert not pump.is_alive(), "the relay did not end when the camera side closed"


def test_pump_ends_once_both_directions_have_been_idle_past_the_cutoff(monkeypatch):
    import socket

    monkeypatch.setattr(la, "_PUMP_IDLE_CUTOFF_S", 0.4)
    monkeypatch.setattr(la, "_PUMP_POLL_S", 0.05)
    browser, client = socket.socketpair()
    go2rtc, upstream = socket.socketpair()
    pump = threading.Thread(target=la._pump, args=(client, upstream), daemon=True)
    pump.start()
    pump.join(timeout=5)
    try:
        assert not pump.is_alive(), \
            "the relay lingered after both directions stayed idle"
    finally:
        browser.close()
        go2rtc.close()


def test_pump_rate_limits_the_should_close_check_while_chunks_flow():
    # 2.2.5 review of P1-89: should_close re-reads the session file, so a busy relay
    # must not call it once per chunk.
    import socket

    calls = []

    def should_close():
        calls.append(time.monotonic())
        return False

    browser, client = socket.socketpair()
    go2rtc, upstream = socket.socketpair()
    pump = threading.Thread(target=la._pump, args=(client, upstream, should_close), daemon=True)
    pump.start()
    try:
        for _ in range(100):
            go2rtc.sendall(b"frame;")
            time.sleep(0.002)
        time.sleep(0.1)
    finally:
        go2rtc.close()
        pump.join(timeout=5)
        browser.close()
    assert 1 <= len(calls) <= 3, f"should_close ran {len(calls)} times for 100 chunks"


# --------------------------------------------------------------------- P2-143: presence takes the shared lock

def test_presence_updates_wait_for_the_shared_session_lock(tmp_path):
    # P2-143: a presence write is a read-modify-write transaction over
    # a record other processes own too (the bot's /print-live, the
    # listener's print-end hook). While another writer holds the
    # cross-process lock, the presence update must wait — not read a
    # stale record and overwrite the other writer's transition.
    from print_handy import live_stream

    path = tmp_path / "live_session.json"
    live_stream.write_session_file(
        live_stream.LiveStreamSession().start("42", now=1000.0), path)
    presence = la.SessionPresence(path, {})
    done = threading.Event()

    def note():
        presence.note_viewer("42", now=1001.0)
        done.set()

    with live_stream.session_file_lock(path):
        worker = threading.Thread(target=note, daemon=True)
        worker.start()
        assert not done.wait(0.5), \
            "the presence update ran while another writer held the session lock"
    worker.join(timeout=5)
    assert done.is_set()
    assert live_stream.read_session_file(path).viewers == 1


# --------------------------------------------------------------------- #1404: main joins shared logging

def test_main_configures_shared_logging_and_logs_the_version(monkeypatch, tmp_path):
    # #1404: once the gates pass, main() configures the shared logging
    # (crash capture for the live-activity role) and logs its serving
    # line — with the version — through the module logger instead of a
    # bare print. The refusal paths stay untouched (covered by the gate
    # suites).
    from types import SimpleNamespace
    from unittest import mock

    from print_handy import __version__, live_gate, live_stream, logs, settings

    data = {"live_stream": {}, "logging": {"level": "INFO"}}
    config = SimpleNamespace(ready=True, enabled=True, missing=[], port=0,
                             activity_origin="https://activity.example",
                             client_id="123")
    fake_server = SimpleNamespace(serve_forever=lambda: None,
                                  server_close=lambda: None)
    monkeypatch.setattr(settings, "load", lambda: data)
    monkeypatch.setattr(live_gate, "live_config", lambda cfg: config)
    monkeypatch.setattr(live_stream, "session_file_path",
                        lambda cfg: tmp_path / "live_session.json")
    monkeypatch.setattr(la, "allowlist_from_settings", lambda cfg: ("42",))
    monkeypatch.setattr(la, "SessionPresence",
                        lambda *a, **k: SimpleNamespace(
                            tick=lambda: None, note_viewer=lambda *a: None,
                            note_leave=lambda *a: None, is_live=lambda: False))
    monkeypatch.setattr(la, "make_server", lambda *a, **k: fake_server)
    configure = mock.Mock()
    monkeypatch.setattr(logs, "configure", configure)
    fake_log = mock.Mock()
    monkeypatch.setattr(la, "log", fake_log)

    assert la.main([]) == 0
    configure.assert_called_once_with({"level": "INFO"}, role="live-activity")
    serving = [call.args[0] for call in fake_log.info.call_args_list
               if call.args and "serving on" in str(call.args[0])]
    assert serving, "the serving line was not logged"
    assert "127.0.0.1:0" in serving[0]
    assert "https://activity.example" in serving[0]
    assert f"Print Handy {__version__}" in serving[0]
