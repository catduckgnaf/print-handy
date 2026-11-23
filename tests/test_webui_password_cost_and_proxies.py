"""PBKDF2 cost and the login throttle behind a proxy (#1145).

New passwords are hashed at the current PBKDF2-HMAC-SHA256 cost with the
count stored beside the hash; a hash saved at the old 200,000 resolves
to BROKEN and must be reset (#1817). The login throttle honors X-Forwarded-For only from
a configured trusted proxy (``[webui] trusted_proxies``, none by default).
"""
import base64
import http.client
import ipaddress
import json
import secrets
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from print_handy import settings
from print_handy.webui import access, pages, server

PASSWORD = "correct horse battery staple"
OLD_ITERATIONS = 200_000


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                     "webui:\n  enabled: true\n  port: 8765\n" + extra)
    return settings.load(local=local, environ=environ or {})


def write_old_hash(data, password=PASSWORD, username="owner"):
    """A login file exactly as a release with 200,000 iterations saved it."""
    salt = secrets.token_bytes(16)
    path = access.access_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": access.PASSWORD, "username": username, "iterations": OLD_ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(access._digest(password, salt, OLD_ITERATIONS)).decode()}))
    return path


def saved(data):
    return json.loads(access.access_path(data).read_text())


def start(data, environ=None):
    httpd = server.make_server(data, environ=environ or {}, port=0,
                               snapshot_func=lambda: {})
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        hdrs = dict(headers or {})
        if body is not None:
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        conn.request(method, path, body=body, headers=hdrs)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def login(port, password=PASSWORD, username="owner", headers=None):
    return call(port, "POST", "/login",
                urlencode({pages.LOGIN_USER_FIELD: username,
                           pages.LOGIN_TOKEN_FIELD: password}), headers)


# -- PBKDF2 cost --------------------------------------------------------------

def test_the_cost_meets_the_owasp_minimum():
    assert access._ITERATIONS >= 600_000


def test_new_hashes_record_the_current_count(tmp_path):
    data = load_settings(tmp_path)
    access.save_password(data, PASSWORD, PASSWORD, username="owner")
    assert saved(data)["iterations"] == access._ITERATIONS
    assert saved(data)["username"] == "owner"
    resolved = access.resolve(data, "127.0.0.1", {})
    assert resolved.mode == access.PASSWORD and resolved.matches(PASSWORD)


def test_an_old_hash_is_broken_and_asks_for_a_reset(tmp_path):
    # #1817: a hash saved at fewer iterations is no longer verified at its
    # own count and re-hashed; the login resolves to BROKEN, even beside the
    # environment token, and the message says to reset it.
    data = load_settings(tmp_path)
    write_old_hash(data)
    for environ in ({}, {"WEBUI_ACCESS_TOKEN": "env-token-xyz"}):
        resolved = access.resolve(data, "127.0.0.1", environ)
        assert resolved.mode == access.BROKEN
        assert not resolved.matches_login("owner", PASSWORD)
        assert "Reset the login" in resolved.problem
        assert access.ACCESS_FILE in resolved.problem
    assert not hasattr(access, "rehash_password")


def test_a_login_without_a_username_is_broken_and_asks_for_a_reset(tmp_path):
    data = load_settings(tmp_path)
    path = write_old_hash(data)
    record = saved(data)
    salt = secrets.token_bytes(16)
    record.pop("username")
    record.update(iterations=access._ITERATIONS, salt=base64.b64encode(salt).decode(),
                  hash=base64.b64encode(access._digest(PASSWORD, salt, access._ITERATIONS)).decode())
    path.write_text(json.dumps(record))
    resolved = access.resolve(data, "127.0.0.1", {})
    assert resolved.mode == access.BROKEN
    assert "Reset the login" in resolved.problem
    with pytest.raises(TypeError):
        access.save_password(data, PASSWORD, PASSWORD)  # a username is required


def test_the_server_refuses_an_old_login_until_it_is_reset(tmp_path):
    data = load_settings(tmp_path)
    path = write_old_hash(data)
    httpd, port = start(data)
    try:
        code, _, body = call(port, "GET", "/api/status", headers={server.TOKEN_HEADER: PASSWORD})
        assert code == 503
        assert b"Reset the login" in body
        assert login(port)[0] == 503
    finally:
        httpd.shutdown()
        httpd.server_close()
    path.unlink()
    access.save_password(data, PASSWORD, PASSWORD, username="owner")
    httpd, port = start(data)
    try:
        assert login(port)[0] == 303
    finally:
        httpd.shutdown()
        httpd.server_close()


# -- trusted proxies ----------------------------------------------------------

NETS = (ipaddress.ip_network("192.0.2.2/32"), ipaddress.ip_network("198.51.100.0/24"))


def test_with_no_trusted_proxy_the_header_never_counts():
    assert access.client_address("192.0.2.2", ["203.0.113.9"], ()) == "192.0.2.2"


@pytest.mark.parametrize("peer, forwarded, expected", [
    # From a trusted proxy: the client it appended (the rightmost hop).
    ("192.0.2.2", ["203.0.113.9"], "203.0.113.9"),
    # A client-supplied left part cannot pick the address.
    ("192.0.2.2", ["1.2.3.4, 203.0.113.9"], "203.0.113.9"),
    # Chained trusted proxies are skipped from the right.
    ("192.0.2.2", ["203.0.113.9, 198.51.100.5"], "203.0.113.9"),
    ("192.0.2.2", ["203.0.113.9", "198.51.100.5"], "203.0.113.9"),
    # An untrusted peer's header is ignored.
    ("192.0.2.77", ["203.0.113.9"], "192.0.2.77"),
    # Garbage before an untrusted hop stays on the last trusted hop.
    ("192.0.2.2", ["not-an-ip"], "192.0.2.2"),
    ("192.0.2.2", ["203.0.113.9, nonsense, 198.51.100.5"], "198.51.100.5"),
    # No header from a trusted proxy: the proxy itself.
    ("192.0.2.2", [], "192.0.2.2"),
    # IPv6 and an IPv4-mapped peer.
    ("::ffff:192.0.2.2", ["2001:db8::1"], "2001:db8::1"),
])
def test_forwarded_for_counts_only_from_a_trusted_proxy(peer, forwarded, expected):
    assert access.client_address(peer, forwarded, NETS) == expected


def test_the_setting_defaults_to_no_trusted_proxy(tmp_path):
    data = load_settings(tmp_path)
    assert data["webui"]["trusted_proxies"] == ""
    assert settings.trusted_proxy_networks(data["webui"]["trusted_proxies"]) == ()


def test_the_setting_accepts_addresses_and_networks(tmp_path):
    data = load_settings(tmp_path, environ={
        "PRINT_HANDY__WEBUI__TRUSTED_PROXIES": "192.0.2.10, 198.51.100.0/24 2001:db8:ffff::/48"})
    assert data["webui"]["trusted_proxies"] == "192.0.2.10/32,198.51.100.0/24,2001:db8:ffff::/48"


def test_a_bad_entry_is_refused_by_name(tmp_path):
    with pytest.raises(settings.SettingsError) as caught:
        load_settings(tmp_path, environ={
            "PRINT_HANDY__WEBUI__TRUSTED_PROXIES": "192.0.2.10,proxy.example"})
    assert "proxy.example" in str(caught.value)


def lock_out(port, forwarded):
    headers = {"X-Forwarded-For": forwarded}
    for _ in range(access.LoginThrottle().max_failures):
        login(port, password="wrong passphrase here", headers=headers)
    return login(port, headers=headers)[0]


def test_behind_a_trusted_proxy_each_client_has_its_own_lockout(tmp_path):
    data = load_settings(tmp_path, "  trusted_proxies: 127.0.0.1\n")
    access.save_password(data, PASSWORD, PASSWORD, username="owner")
    httpd, port = start(data)
    try:
        assert lock_out(port, "203.0.113.9") == 429
        # Another client behind the same proxy is not locked out.
        assert login(port, headers={"X-Forwarded-For": "203.0.113.10"})[0] == 303
        # And the locked-out client stays locked out under its own address.
        assert login(port, headers={"X-Forwarded-For": "203.0.113.9"})[0] == 429
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_without_a_trusted_proxy_the_header_cannot_dodge_the_lockout(tmp_path):
    data = load_settings(tmp_path)
    access.save_password(data, PASSWORD, PASSWORD, username="owner")
    httpd, port = start(data)
    try:
        assert lock_out(port, "203.0.113.9") == 429
        # A fresh forwarded-for value per guess changes nothing.
        assert login(port, headers={"X-Forwarded-For": "203.0.113.77"})[0] == 429
    finally:
        httpd.shutdown()
        httpd.server_close()
