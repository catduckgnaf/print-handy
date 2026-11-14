"""Tests for the Web UI PWA foundation (#450 Phase 1).

Verifies the web app manifest, the service worker script, the offline
fallback page, the static install assets, the shell head tags and
registration script, the routes-table declaration of the PWA routes,
and the non-caching contract for authenticated and API responses.
"""
import json
import struct
import tempfile
import unittest

from print_handy import __version__
from print_handy.webui import pwa, routes, shell
from tests.test_webui_webserver import WebServerCase


def png_size(data):
    """(width, height) from a PNG's IHDR chunk, stdlib only."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


class PWAFoundationUnitTests(unittest.TestCase):
    """Unit tests for the PWA module and shell integration."""

    def test_manifest_structure_and_types(self):
        manifest = json.loads(pwa.manifest_json())
        self.assertEqual(manifest["name"], "Print Handy")
        self.assertEqual(manifest["short_name"], "Print Handy")
        # A stable explicit id, not left to default to start_url.
        self.assertEqual(manifest["id"], "/")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["theme_color"], "#12110f")
        self.assertEqual(manifest["background_color"], "#12110f")
        self.assertEqual(manifest["orientation"], "any")
        self.assertIn("utilities", manifest["categories"])
        self.assertTrue(manifest["description"])

        icons = manifest["icons"]
        purposes = {icon.get("purpose") for icon in icons}
        self.assertIn("any", purposes)
        self.assertIn("maskable", purposes)

        sizes = {icon["sizes"] for icon in icons}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertIn("180x180", sizes)

        maskable = [i for i in icons if i.get("purpose") == "maskable"]
        self.assertTrue(maskable)
        self.assertEqual(maskable[0]["src"], "/static/icon-512-maskable.png")
        self.assertEqual(maskable[0]["sizes"], "512x512")
        # The maskable icon is a separate file from the "any" 512 icon,
        # never one file declared as both purposes.
        any_512 = [i for i in icons
                   if i.get("purpose") == "any" and i["sizes"] == "512x512"]
        self.assertTrue(any_512)
        self.assertNotEqual(any_512[0]["src"], maskable[0]["src"])

    def test_maskable_icon_is_a_distinct_padded_png(self):
        _, regular = pwa.resolve_static_file("icon-512.png")
        _, maskable = pwa.resolve_static_file("icon-512-maskable.png")
        self.assertEqual(png_size(regular), (512, 512))
        self.assertEqual(png_size(maskable), (512, 512))
        self.assertNotEqual(regular, maskable)

    def test_service_worker_script_contract(self):
        sw = pwa.SW_SCRIPT
        self.assertIn(pwa.CACHE_NAME, sw)
        for url in pwa.PRECACHE_URLS:
            self.assertIn(url, sw)
        self.assertIn("/manifest.webmanifest", sw)
        self.assertIn("/offline", sw)
        self.assertIn("/favicon.ico", sw)
        self.assertIn("/static/icon-192.png", sw)
        self.assertIn("/static/icon-512.png", sw)
        self.assertIn("/static/icon-512-maskable.png", sw)
        # Old caches are cleaned up on activate.
        self.assertIn("addEventListener('activate'", sw)
        self.assertIn("caches.delete(key)", sw)

        # Non-caching boundaries: API, token uploads, auth, theme.
        self.assertIn("indexOf('/api/') === 0", sw)
        self.assertIn("indexOf('/u/') === 0", sw)
        self.assertIn("'/login'", sw)
        self.assertIn("'/theme'", sw)
        self.assertIn("event.request.method !== 'GET'", sw)
        self.assertIn("url.origin !== self.location.origin", sw)

        # Navigations: network-only with the cached /offline fallback;
        # the navigation response itself is never stored.
        self.assertIn("event.request.mode === 'navigate'", sw)
        self.assertIn("caches.match('/offline')", sw)

    def test_offline_page_rendering(self):
        page_html = pwa.render_offline_page(theme="dark")
        self.assertIn("Print Handy", page_html)
        self.assertIn("Offline", page_html)
        self.assertIn("unreachable", page_html)
        self.assertIn("Try Reconnecting", page_html)
        self.assertIn("window.location.reload()", page_html)
        self.assertIn(f"Print Handy {__version__} is free software", page_html)

    def test_shell_head_tags_and_pwa_script(self):
        html = shell.page("Status", "<p>content</p>", path="/")
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)
        self.assertIn('<meta name="theme-color" content="#12110f"', html)
        self.assertIn('<meta name="theme-color" content="#f3efe6"', html)
        self.assertIn('<link rel="icon" type="image/x-icon" href="/favicon.ico">', html)
        self.assertIn('<link rel="apple-touch-icon" sizes="180x180" href="/static/icon-180.png">', html)
        self.assertIn('<meta name="apple-mobile-web-app-capable" content="yes">', html)
        self.assertIn('<meta name="mobile-web-app-capable" content="yes">', html)
        # Registration is guarded to secure contexts, requests
        # persistent storage, and wires the install affordances.
        self.assertIn("window.isSecureContext", html)
        self.assertIn("navigator.serviceWorker.register('/sw.js'", html)
        self.assertIn("navigator.storage.persist()", html)
        self.assertIn("beforeinstallprompt", html)
        self.assertIn("Install app", html)
        self.assertIn("Add to Home Screen", html)

        # Bare pages (chrome=False) like /login carry the same head
        # tags and registration script.
        bare_html = shell.page("Login", "<p>form</p>", path="/login", chrome=False)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', bare_html)
        self.assertIn("navigator.serviceWorker.register('/sw.js'", bare_html)

    def test_pwa_routes_are_declared_public_in_the_route_table(self):
        expected = {
            "/manifest.webmanifest": "_pwa_manifest",
            "/manifest.json": "_pwa_manifest",
            "/sw.js": "_pwa_service_worker",
            "/offline": "_pwa_offline",
            "/favicon.ico": "_pwa_favicon",
        }
        for path, handler in expected.items():
            route, capture = routes.resolve("GET", path)
            self.assertIsNotNone(route, path)
            self.assertEqual(route.policy, routes.PUBLIC, path)
            self.assertEqual(route.handler, handler, path)
            self.assertIsNone(capture, path)
        route, capture = routes.resolve("GET", "/static/icon-192.png")
        self.assertEqual(route.policy, routes.PUBLIC)
        self.assertEqual(route.handler, "_pwa_static")
        self.assertEqual(capture, "icon-192.png")
        # A POST to a PWA path is not a PWA route: it lands on the
        # read-only refusal like every other undeclared POST.
        route, _ = routes.resolve("POST", "/sw.js")
        self.assertEqual(route.handler, "_read_only")

    def test_static_asset_resolution_and_traversal_guards(self):
        res = pwa.resolve_static_file("icon-192.png")
        self.assertIsNotNone(res)
        ctype, data = res
        self.assertEqual(ctype, "image/png")
        self.assertEqual(png_size(data), (192, 192))

        res_maskable = pwa.resolve_static_file("icon-512-maskable.png")
        self.assertIsNotNone(res_maskable)
        ctype, data = res_maskable
        self.assertEqual(ctype, "image/png")
        self.assertTrue(len(data) > 0)

        res_ico = pwa.resolve_static_file("favicon.ico")
        self.assertIsNotNone(res_ico)
        ctype, data = res_ico
        self.assertEqual(ctype, "image/x-icon")
        self.assertTrue(len(data) > 0)

        # Path traversal and invalid names are refused.
        self.assertIsNone(pwa.resolve_static_file("../pyproject.toml"))
        self.assertIsNone(pwa.resolve_static_file(".."))
        self.assertIsNone(pwa.resolve_static_file("sub/file.png"))
        self.assertIsNone(pwa.resolve_static_file(".hidden"))
        self.assertIsNone(pwa.resolve_static_file(""))
        self.assertIsNone(pwa.resolve_static_file("nonexistent.png"))


class PWAFoundationServerIntegrationTests(WebServerCase):
    """End-to-end HTTP integration tests for PWA routes and cache contracts."""

    def test_pwa_routes_are_unauthenticated_and_served_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)

            code, headers, body = self.get(port, "/manifest.webmanifest")
            self.assertEqual(code, 200)
            self.assertIn("application/manifest+json", headers.get("Content-Type", ""))
            self.assertIn("public, max-age=", headers.get("Cache-Control", ""))
            manifest = json.loads(body)
            self.assertEqual(manifest["name"], "Print Handy")
            self.assertEqual(manifest["id"], "/")
            self.assertEqual(manifest["scope"], "/")

            code_json, _, body_json = self.get(port, "/manifest.json")
            self.assertEqual(code_json, 200)
            self.assertEqual(body, body_json)

            code, headers, body = self.get(port, "/sw.js")
            self.assertEqual(code, 200)
            self.assertIn("application/javascript", headers.get("Content-Type", ""))
            self.assertEqual(headers.get("Service-Worker-Allowed"), "/")
            self.assertEqual(headers.get("Cache-Control"), "no-cache")
            self.assertIn("addEventListener('install'", body)

            code, headers, body = self.get(port, "/offline")
            self.assertEqual(code, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("Print Handy", body)
            self.assertIn("Offline", body)

            code, headers, raw = self.request(port, "GET", "/favicon.ico")
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Content-Type"), "image/x-icon")
            self.assertTrue(len(raw) > 0)

            for icon_name in ("icon-192.png", "icon-512.png", "icon-512-maskable.png", "icon-180.png"):
                code, headers, raw = self.request(port, "GET", f"/static/{icon_name}")
                self.assertEqual(code, 200, f"Failed for {icon_name}")
                self.assertEqual(headers.get("Content-Type"), "image/png")
                self.assertIn("public, max-age=", headers.get("Cache-Control", ""))
                self.assertTrue(len(raw) > 0)

    def test_static_routes_404_on_missing_or_traversal_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)

            code, _, raw = self.request(port, "GET", "/static/does-not-exist.png")
            self.assertEqual(code, 404)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])

            code, _, _ = self.request(port, "GET", "/static/../pyproject.toml")
            self.assertEqual(code, 404)

            # URL-encoded traversal is decoded by the route table and
            # refused by the same guard.
            code, _, _ = self.request(port, "GET", "/static/%2e%2e%2fpyproject.toml")
            self.assertEqual(code, 404)

    def test_dynamic_pages_keep_no_store_cache_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)

            code, headers, _ = self.get(port, "/login")
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")

            code, headers, _ = self.get(port, "/", headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")

            code, headers, _ = self.get(port, "/config", headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")

            code, headers, raw = self.request(port, "GET", "/api/status", headers=self.auth())
            self.assertEqual(code, 200)
            self.assertIn("application/json", headers.get("Content-Type", ""))

    def test_shell_pages_served_with_pwa_head_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)
            code, _, body = self.get(port, "/login")
            self.assertEqual(code, 200)
            self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', body)
            self.assertIn("window.isSecureContext", body)


if __name__ == "__main__":
    unittest.main()
