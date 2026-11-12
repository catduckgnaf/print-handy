"""#1319: every Web UI route declares one policy, and one middleware enforces it.

The route table (print_handy/webui/routes.py) is the server's only way
into a handler: _Handler.do_* hand every request to _dispatch, which
resolves the route and runs _policy before the handler. These tests
enumerate that table and fail when:

- a route is added, removed or changes policy without this snapshot
  changing with it (EXPECTED below is today's behavior, written down);
- a do_* method answers a request without going through _dispatch;
- a route handler checks a credential itself instead of the policy;
- any declared route, requested at its own path, reaches its handler
  without the policy refusing an anonymous request first, or is
  refused with a valid credential.

Offline: a loopback server with an access token, every handler replaced
by a stub that records the call, so nothing behind a route runs.
"""
import ast
import http.client
import inspect
import shutil
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.webui import routes, server, upload

TOKEN = "route-policy-test-token"

P, S, SC, H, L = (routes.PUBLIC, routes.SESSION, routes.SESSION_CSRF,
                  routes.HEADER, routes.LINK_TOKEN)

# Today's behavior (main at 0b0ebac1), one row per route, in table order.
EXPECTED = [
    ("GET", "/healthz", P),
    ("GET", "/setup", P),
    ("GET", "/login", P),
    ("GET", "/u/*", L),
    ("GET", "/theme", S),
    ("GET", "/walkthrough", S),
    ("GET", "/", S),
    ("GET", "/api/status", S),
    ("GET", "/data", S),
    ("GET", "/wrapped", S),
    ("GET", "/api/data", S),
    ("GET", "/api/wrapped", S),
    ("GET", "/wrapped/video-file", S),
    ("GET", "/api/wrapped/video-status", S),
    ("GET", "/readyz", S),
    ("GET", "/api/printers/*", S),
    ("GET", "/spools", S),
    ("GET", "/printers", S),
    ("GET", "/printers/*/screen", S),
    ("GET", "/spools/*", S),
    ("GET", "/config", S),
    ("GET", "/chat", S),
    ("GET", "/chat/state", S),
    ("GET", "/upload", S),
    ("GET", "/sources", S),
    ("GET", "/designs", S),
    ("GET", "/designs/*", S),
    ("GET", "/projects", S),
    ("GET", "/api/projects", S),
    ("GET", "/layers", S),
    ("GET", "/layers/*", S),
    ("GET", "*", S),
    ("POST", "/u/*", L),
    ("POST", "/login", P),
    ("POST", "/chat", SC),
    ("POST", "/chat/choice", SC),
    ("POST", "/upload", SC),
    ("POST", "/logout", P),
    ("POST", "/walkthrough", SC),
    ("POST", "/wrapped/video", SC),
    ("POST", "/wrapped/share", SC),
    ("POST", "/config/preview", SC),
    ("POST", "/config", SC),
    ("POST", "/debug", SC),
    ("POST", "/printers", SC),
    ("POST", "/spools/*", SC),
    ("POST", "/api/printers/*", H),
    ("POST", "*", P),
    ("PUT", "*", P),
    ("DELETE", "*", P),
    ("PATCH", "*", P),
    ("HEAD", "*", P),
    ("OPTIONS", "*", P),
]

# The only _Handler methods allowed to check a credential: the policy
# middleware and the credential primitives themselves.
AUTH_PRIMITIVES = {"_authorized", "_header_authorized", "_session_authorized",
                   "_post_authorized", "_link_valid", "_deny_header"}
POLICY_LAYER = {"_policy", "_policy_recheck", "_authorized", "_post_authorized"}


def sample_path(route):
    """A request path that the route matches at its own row."""
    if route.pattern == "*":
        return "/no-such-route-1319"
    return route.pattern.replace("*", "x")


def handler_ast():
    source = textwrap.dedent(inspect.getsource(server._Handler))
    (cls,) = ast.parse(source).body
    return {node.name: node for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def self_calls(node):
    """Names of self.<name>(...) calls anywhere inside a function."""
    found = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "self"):
            found.add(sub.func.attr)
    return found


class RouteTableTests(unittest.TestCase):
    def test_every_route_is_declared_with_todays_policy(self):
        declared = [(r.method, r.pattern, r.policy) for r in routes.ROUTES]
        self.assertEqual(declared, EXPECTED)

    def test_policies_handlers_and_catch_alls(self):
        seen = set()
        for route in routes.ROUTES:
            with self.subTest(route=(route.method, route.pattern)):
                self.assertIn(route.policy, routes.POLICIES)
                self.assertIn(route.method, routes.METHODS)
                self.assertNotIn((route.method, route.pattern), seen)
                seen.add((route.method, route.pattern))
                self.assertLessEqual(route.pattern.count("*"), 1)
                handler = getattr(server._Handler, route.handler, None)
                self.assertTrue(callable(handler), route.handler)
                # The handler takes exactly what _dispatch passes it.
                args = ([] if "*" not in route.pattern else ["capture"]) \
                    + (["/path"] if route.pass_path else []) \
                    + ([{}] if route.policy == SC else [])
                inspect.signature(handler).bind(object(), *args)
        for method in routes.METHODS:
            rows = [r for r in routes.ROUTES if r.method == method]
            self.assertEqual(rows[-1].pattern, "*", method)

    def test_every_route_is_reached_at_its_own_path(self):
        for route in routes.ROUTES:
            with self.subTest(route=(route.method, route.pattern)):
                self.assertIs(routes.resolve(route.method, sample_path(route))[0], route)

    def test_every_request_method_goes_through_the_dispatcher(self):
        methods = handler_ast()
        do_methods = {name for name in dir(server._Handler) if name.startswith("do_")}
        self.assertEqual(do_methods, {f"do_{m}" for m in routes.METHODS})
        for method in routes.METHODS:
            node = methods.get(f"do_{method}")
            self.assertIsNotNone(node, f"do_{method} is not defined on _Handler")
            (stmt,) = node.body
            call = stmt.value
            self.assertEqual(ast.unparse(call), f"self._dispatch({method!r})")

    def test_no_route_handler_checks_a_credential_itself(self):
        methods = handler_ast()
        reachable = set()
        pending = [r.handler for r in routes.ROUTES]
        while pending:  # handlers and every _Handler method they call
            name = pending.pop()
            if name in reachable or name not in methods or name in POLICY_LAYER:
                continue
            reachable.add(name)
            pending.extend(self_calls(methods[name]))
        for name in sorted(reachable):
            with self.subTest(handler=name):
                calls = self_calls(methods[name]) & AUTH_PRIMITIVES
                self.assertFalse(calls, f"{name} calls {sorted(calls)}; declare "
                                        "the policy in routes.py instead")

    def test_the_checks_catch_an_undeclared_policy_and_a_bypass(self):
        # The guards above are not vacuous: a row with an unknown policy
        # is refused, and a handler that checks a credential is found.
        bogus = routes.Route("GET", "/bogus", "cookie-only", "_healthz")
        self.assertNotIn(bogus.policy, routes.POLICIES)
        tree = ast.parse("def h(self):\n    return self._header_authorized()")
        self.assertEqual(self_calls(tree.body[0]) & AUTH_PRIMITIVES,
                         {"_header_authorized"})


class RoutePolicyEnforcementTests(unittest.TestCase):
    """Each declared route at its own path, against a claimed server."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  state_home: {tmp}/hermes\n"
                         "webui:\n  enabled: true\n  port: 8765\n")
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN}
        data = settings.load(local=local, environ=environ)
        self.httpd = server.make_server(data, environ=environ, port=0,
                                        snapshot_func=lambda: {})
        self.port = self.httpd.server_address[1]
        self.calls = []
        for name in {r.handler for r in routes.ROUTES}:
            patcher = mock.patch.object(server._Handler, name, self.stub(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.link = upload.create_link(self.httpd.settings_data,
                                       self.httpd.access.credential_fingerprint())

    def stub(self, name):
        calls = self.calls

        def handler(handler_self, *args):
            calls.append((name, args))
            handler_self._send(204, "", "text/plain")
        return handler

    def request(self, route, headers=None, link=False):
        path = sample_path(route)
        if link:
            path = "/u/" + self.link
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            body = b"" if route.method in ("POST", "PUT", "PATCH") else None
            conn.request(route.method, path, body=body, headers={
                "Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    def test_an_anonymous_request_is_refused_by_the_declared_policy(self):
        refused = {S: 401, H: 401, SC: 403, L: 404}
        for route in routes.ROUTES:
            with self.subTest(route=(route.method, route.pattern)):
                del self.calls[:]
                code = self.request(route)
                if route.policy == P:
                    self.assertEqual(code, 204)
                    self.assertEqual([c[0] for c in self.calls], [route.handler])
                else:
                    self.assertEqual(code, refused[route.policy])
                    self.assertEqual(self.calls, [], "the handler ran without the policy")

    def test_a_valid_credential_reaches_every_handler(self):
        for route in routes.ROUTES:
            with self.subTest(route=(route.method, route.pattern)):
                del self.calls[:]
                if route.policy == L:  # the link in the address is the credential
                    code = self.request(route, link=True)
                else:
                    code = self.request(route, headers={server.TOKEN_HEADER: TOKEN})
                self.assertEqual(code, 204)
                self.assertEqual([c[0] for c in self.calls], [route.handler])

    def test_a_session_cookie_alone_never_passes_a_header_or_form_route(self):
        handler = server._Handler.__new__(server._Handler)
        handler.server = self.httpd
        cookie = handler._start_session()[1].split(";", 1)[0]
        for route in routes.ROUTES:
            if route.policy not in (H, SC):
                continue
            with self.subTest(route=(route.method, route.pattern)):
                del self.calls[:]
                code = self.request(route, headers={"Cookie": cookie})
                self.assertEqual(code, 401 if route.policy == H else 403)
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
