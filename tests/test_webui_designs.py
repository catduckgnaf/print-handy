"""Web UI designs catalog (#388, stage 3): the read-only /designs pages.

Everything runs offline: settings come from temp YAML files, design
sessions are fabricated records in a temp cache (the same shape the
engine writes), and the server binds an ephemeral loopback port. The
catalog reads the session store through print_handy.core.design, so
the engine-level tests here pin that read API too.
"""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.core import design
from print_handy.webui import designs, server

TOKEN = "webui-test-token"
NOW = time.time()
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-preview"
STL_BYTES = b"solid fake\n" + bytes(64)
SCAD_TEXT = "// a bracket\ncube([40, 20, 3]);\n"


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(
        f"paths:\n  state_home: {tmp}/hermes\n  cache: {tmp}/cache\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def record(**overrides):
    base = {"id": "abc123def456", "owner_key": "o", "chat_id": "1",
            "description": "a 40x20x3 plate with two holes", "kind": "create",
            "source": None, "edit_mode": None, "base_stl": None,
            "body_file": None, "scad_deps": [], "status": "active", "turns": 1,
            "created_at": NOW - 60, "updated_at": NOW - 60,
            "scad": "turn-1.scad", "stl": "turn-1.stl",
            "preview": "turn-1.png", "geometry": None}
    base.update(overrides)
    return base


def write_session(cache, session, files):
    folder = design.sessions_root(cache) / session["id"]
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (folder / name).write_bytes(
            content if isinstance(content, bytes) else content.encode())
    (folder / "session.json").write_text(json.dumps(session))
    return folder


def full_files(scad=SCAD_TEXT):
    return {"turn-1.scad": scad, "turn-1.stl": STL_BYTES, "turn-1.png": PNG_BYTES}


class CatalogApiTests(unittest.TestCase):
    """The engine's read API the catalog is built on."""

    def test_list_sessions_newest_first_within_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = design.sessions_root(tmp)
            write_session(tmp, record(id="older1111111", updated_at=NOW - 3600), {})
            write_session(tmp, record(id="newer2222222", updated_at=NOW - 60), {})
            write_session(tmp, record(id="stale3333333",
                                      updated_at=NOW - 15 * 86400), {})
            broken = root / "broken444444"
            broken.mkdir(parents=True)
            (broken / "session.json").write_text("not json")
            found = design.list_sessions(root, now=NOW)
        self.assertEqual([s["id"] for s in found], ["newer2222222", "older1111111"])

    def test_session_file_resolves_only_record_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_session(tmp, record(), full_files())
            root = design.sessions_root(tmp)
            self.assertEqual(design.session_file(root, "abc123def456", "scad").name,
                             "turn-1.scad")
            self.assertEqual(design.session_file(root, "abc123def456", "stl").read_bytes(),
                             STL_BYTES)
            self.assertIsNotNone(design.session_file(root, "abc123def456", "preview"))
            self.assertIsNone(design.session_file(root, "abc123def456", "body"))
            self.assertIsNone(design.session_file(root, "missing99999", "stl"))
            self.assertIsNone(design.session_file(root, "..", "stl"))

    def test_session_file_resolves_the_base_mesh(self):
        # P2-165: a mesh edit's composed .scad imports the session-local
        # source.stl (the record's base_stl), so the catalog must be able
        # to serve that base mesh alongside the source.
        with tempfile.TemporaryDirectory() as tmp:
            edit = record(kind="edit", edit_mode="mesh", base_stl="source.stl",
                          source={"origin": "file", "name": "gear.stl", "type": "stl"})
            write_session(tmp, edit, {"source.stl": STL_BYTES, "turn-1.scad": SCAD_TEXT})
            root = design.sessions_root(tmp)
            self.assertEqual(design.session_file(root, edit["id"], "base").read_bytes(),
                             STL_BYTES)
        with tempfile.TemporaryDirectory() as tmp:
            write_session(tmp, record(), full_files())  # a create: no base mesh
            root = design.sessions_root(tmp)
            self.assertIsNone(design.session_file(root, "abc123def456", "base"))

    def test_session_file_refuses_names_that_escape_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret = Path(tmp) / "secret.txt"
            secret.write_text("not a design file")
            sneaky = record(stl="../secret.txt", preview=str(secret))
            write_session(tmp, sneaky, {"turn-1.scad": SCAD_TEXT})
            root = design.sessions_root(tmp)
            self.assertIsNone(design.session_file(root, sneaky["id"], "stl"))
            self.assertIsNone(design.session_file(root, sneaky["id"], "preview"))


class DerivationTests(unittest.TestCase):
    """Name / state / kind come from the session record's own fields."""

    def test_name_from_description_then_source_then_placeholder(self):
        self.assertEqual(designs.session_name(record()), 
                         "a 40x20x3 plate with two holes")
        self.assertEqual(designs.session_name(record(description="  a\n  b  ")), "a b")
        long = designs.session_name(record(description="x" * 200))
        self.assertLessEqual(len(long), 80)
        edit = record(description="", kind="edit",
                      source={"origin": "file", "name": "gear.stl", "type": "stl"})
        self.assertEqual(designs.session_name(edit), "Edit of gear.stl")
        self.assertEqual(designs.session_name(record(description="", source=None)),
                         "Untitled design")

    def test_state_mapping(self):
        self.assertEqual(designs.session_state(record(status="cancelled")), "Cancelled")
        self.assertEqual(designs.session_state(record(status="handed_off")), "Handed off")
        self.assertEqual(designs.session_state(record(status="awaiting_revision")),
                         "In progress")
        self.assertEqual(designs.session_state(record(status="active")), "Ready")
        self.assertEqual(designs.session_state(record(status="active", stl=None)),
                         "In progress")

    def test_kind_wording(self):
        self.assertEqual(designs.session_kind(record()), "Created from a description")
        mesh = record(kind="edit", edit_mode="mesh",
                      source={"origin": "file", "name": "gear.stl", "type": "stl"})
        self.assertTrue(designs.session_kind(mesh).startswith("Editing `gear.stl` as a mesh"))
        scad = record(kind="edit", edit_mode="scad",
                      source={"origin": "file", "name": "gear.scad", "type": "scad"})
        self.assertIn("in source form", designs.session_kind(scad))


class DesignsRouteTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None):
        data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
                             environ=environ)
        httpd = server.make_server(data, environ={} if environ is None else environ,
                                   port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd.server_address[1]

    def request(self, port, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def seed(self, tmp):
        """Two sessions: a ready create (all files) and a handed-off edit."""
        create = record(id="create111111", updated_at=NOW - 30,
                        created_at=NOW - 300, turns=2)
        write_session(f"{tmp}/cache", create, full_files())
        edit = record(id="edit22222222", kind="edit", edit_mode="mesh",
                      description="make this twice as tall", status="handed_off",
                      updated_at=NOW - 120, created_at=NOW - 600, turns=1,
                      source={"origin": "file", "name": "gear.stl", "type": "stl"},
                      scad="turn-1.scad", stl=None, preview=None)
        write_session(f"{tmp}/cache", edit, {"turn-1.scad": SCAD_TEXT})
        return create, edit

    def test_listing_renders_session_fields_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp)
            port = self.start(tmp)
            code, _headers, body = self.request(port, "GET", "/designs")
        text = body.decode()
        self.assertEqual(code, 200)
        self.assertIn("a 40x20x3 plate with two holes", text)
        self.assertIn("Created from a description", text)
        self.assertIn("Ready", text)
        self.assertIn("make this twice as tall", text)
        self.assertIn("Editing `gear.stl` as a mesh", text)
        self.assertIn("Handed off", text)
        self.assertIn('src="/designs/create111111/preview"', text)
        self.assertLess(text.index("create111111"), text.index("edit22222222"))

    def test_mesh_edit_offers_the_base_mesh_download(self):
        # P2-165: without source.stl the downloaded .scad cannot render —
        # its base() module imports that file — so a mesh edit's detail
        # page offers the base mesh and the route serves it.
        with tempfile.TemporaryDirectory() as tmp:
            edit = record(id="edit33333333", kind="edit", edit_mode="mesh",
                          description="make this twice as tall",
                          base_stl="source.stl",
                          source={"origin": "file", "name": "gear.stl", "type": "stl"},
                          scad="turn-1.scad", stl=None, preview=None)
            write_session(f"{tmp}/cache", edit,
                          {"source.stl": STL_BYTES, "turn-1.scad": SCAD_TEXT})
            port = self.start(tmp)
            code, _headers, body = self.request(port, "GET", "/designs/edit33333333/base")
            self.assertEqual(code, 200)
            self.assertEqual(body, STL_BYTES)
            code, _headers, body = self.request(port, "GET", "/designs/edit33333333")
            self.assertEqual(code, 200)
            self.assertIn("/designs/edit33333333/base", body.decode())

    def test_listing_works_with_design_disabled(self):
        # The catalog lists what is on disk; [design] enabled=false (the
        # default, spelled out here) hides nothing already made.
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp)
            port = self.start(tmp, extra="design:\n  enabled: false\n")
            code, _headers, body = self.request(port, "GET", "/designs")
        self.assertEqual(code, 200)
        self.assertIn("a 40x20x3 plate with two holes", body.decode())

    def test_empty_state_explains_designs_come_from_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            port = self.start(tmp)
            code, _headers, body = self.request(port, "GET", "/designs")
        self.assertEqual(code, 200)
        self.assertIn("No designs yet", body.decode())
        self.assertIn("/print-design", body.decode())

    def test_detail_shows_escaped_scad_and_available_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            create, edit = self.seed(tmp)
            write_session(f"{tmp}/cache", create,
                          full_files(scad="// <script>alert(1)</script>\ncube([1,1,1]);\n"))
            port = self.start(tmp)
            code, _h, body = self.request(port, "GET", "/designs/create111111")
            text = body.decode()
            self.assertEqual(code, 200)
            self.assertIn("&lt;script&gt;alert(1)", text)
            self.assertNotIn("<script>alert(1)", text)
            self.assertIn("/designs/create111111/scad", text)
            self.assertIn("/designs/create111111/stl", text)
            self.assertIn("/designs/create111111/preview", text)
            code, _h, body = self.request(port, "GET", "/designs/edit22222222")
            text = body.decode()
            self.assertEqual(code, 200)
            self.assertIn("/designs/edit22222222/scad", text)
            self.assertNotIn("/designs/edit22222222/stl", text)  # no STL recorded

    def test_downloads_serve_the_right_bytes_and_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp)
            port = self.start(tmp)
            code, headers, body = self.request(port, "GET", "/designs/create111111/scad")
            self.assertEqual(code, 200)
            self.assertEqual(body, SCAD_TEXT.encode())
            self.assertTrue(headers["Content-Type"].startswith("text/plain"))
            self.assertIn("attachment", headers["Content-Disposition"])
            code, headers, body = self.request(port, "GET", "/designs/create111111/stl")
            self.assertEqual(code, 200)
            self.assertEqual(body, STL_BYTES)
            self.assertEqual(headers["Content-Type"], "model/stl")
            code, headers, body = self.request(port, "GET", "/designs/create111111/preview")
            self.assertEqual(code, 200)
            self.assertEqual(body, PNG_BYTES)
            self.assertEqual(headers["Content-Type"], "image/png")

    def test_unknown_ids_kinds_and_traversal_are_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp)
            port = self.start(tmp)
            for path in ("/designs/missing99999",
                         "/designs/create111111/bogus",
                         "/designs/create111111/stl/extra",
                         "/designs/../secret",
                         "/designs/%2e%2e",
                         "/designs/create111111/..%2f..%2fsession.json"):
                code, _h, _b = self.request(port, "GET", path)
                self.assertEqual(code, 404, path)

    def test_token_required_when_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp)
            port = self.start(tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
            code, _h, _b = self.request(port, "GET", "/designs")
            self.assertEqual(code, 401)
            code, _h, _b = self.request(port, "GET", "/designs/create111111",
                                        headers={server.TOKEN_HEADER: "wrong"})
            self.assertEqual(code, 401)
            code, _h, body = self.request(port, "GET", "/designs",
                                          headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertNotIn(TOKEN, body.decode())

    def test_non_get_methods_are_405(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.seed(tmp)
            port = self.start(tmp)
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                for path in ("/designs", "/designs/create111111",
                             "/designs/create111111/stl"):
                    code, _h, _b = self.request(port, method, path)
                    self.assertEqual(code, 405, (method, path))


if __name__ == "__main__":
    unittest.main()
