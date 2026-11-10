"""Tests for Web UI PWA foundation (#450 Phase 1).

Verifies the Web App Manifest, Service Worker script, offline shell fallback,
static install assets, HTML head tags, service worker registration script, and
the security-conscious non-caching contract for authenticated / API responses.
"""
import json
import tempfile
import unittest

from print_handy import __version__
from print_handy.webui import pwa, server, shell
from tests.test_webui_webserver import TOKEN, WebServerCase


class PWAFoundationUnitTests(unittest.TestCase):
    """Unit tests for the PWA module and shell integration."""

    def test_manifest_structure_and_types(self):
        manifest_str = pwa.manifest_json()
        manifest = json.loads(manifest_str)
        self.assertEqual(manifest["name"], "Print Handy")
        self.assertEqual(manifest["short_name"], "Print Handy")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["theme_color"], "#12110f")
        self.assertEqual(manifest["background_color"], "#12110f")
        self.assertEqual(manifest["orientation"], "any")
        self.assertIn("utilities", manifest["categories"])

        # Icons must include 192, 512, 512 maskable, 180, and favicon
        icons = manifest["icons"]
        purposes = {icon.get("purpose") for icon in icons}
        self.assertIn("any", purposes)
        self.assertIn("maskable", purposes)

        sizes = {icon["sizes"] for icon in icons}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertIn("180x180", sizes)

        # Check maskable icon explicitly
        maskable = [i for i in icons if i.get("purpose") == "maskable"]
        self.assertTrue(maskable)
        self.assertEqual(maskable[0]["src"], "/static/icon-512-maskable.png")

    def test_service_worker_script_contract(self):
        sw = pwa.SW_SCRIPT
        # Cache versioning & precache assets
        self.assertIn(pwa.CACHE_NAME, sw)
        self.assertIn("/manifest.webmanifest", sw)
        self.assertIn("/offline", sw)
        self.assertIn("/favicon.ico", sw)
        self.assertIn("/static/icon-192.png", sw)
        self.assertIn("/static/icon-512.png", sw)
        self.assertIn("/static/icon-512-maskable.png", sw)

        # Non-caching security boundaries: API, auth, controls, tokens
        self.assertIn("indexOf('/api/') === 0", sw)
        self.assertIn("indexOf('/u/') === 0", sw)
        self.assertIn("'/login'", sw)
        self.assertIn("'/theme'", sw)
        self.assertIn("event.request.method !== 'GET'", sw)
        self.assertIn("url.origin !== self.location.origin", sw)

        # HTML navigation: network-only with cached /offline fallback;
        # never stores dynamic navigation responses in cache
        self.assertIn("event.request.mode === 'navigate'", sw)
        self.assertIn("caches.match('/offline')", sw)

    def test_offline_page_rendering(self):
        page_html = pwa.render_offline_page(theme="dark")
        self.assertIn("Print Handy", page_html)
        self.assertIn("Offline", page_html)
        self.assertIn("unreachable", page_html)
        self.assertIn("Try Reconnecting", page_html)
        self.assertIn("window.location.reload()", page_html)
        # Inherits the AGPL source footer
        self.assertIn(f"Print Handy {__version__} is free software", page_html)

    def test_shell_head_tags_and_sw_registration(self):
        # Shell page includes PWA manifest and icons
        html = shell.page("Status", "<p>content</p>", path="/")
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', html)
        self.assertIn('<meta name="theme-color" content="#12110f"', html)
        self.assertIn('<link rel="icon" type="image/x-icon" href="/favicon.ico">', html)
        self.assertIn('<link rel="apple-touch-icon" sizes="180x180" href="/static/icon-180.png">', html)
        self.assertIn("navigator.serviceWorker.register('/sw.js'", html)

        # Bare pages (chrome=False) like /login also include PWA meta and registration
        bare_html = shell.page("Login", "<p>form</p>", path="/login", chrome=False)
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', bare_html)
        self.assertIn("navigator.serviceWorker.register('/sw.js'", bare_html)

    def test_static_asset_resolution_and_traversal_guards(self):
        # Valid files resolve with correct content type and data
        res = pwa.resolve_static_file("icon-192.png")
        self.assertIsNotNone(res)
        assert res is not None
        ctype, data = res
        self.assertEqual(ctype, "image/png")
        self.assertTrue(len(data) > 0)

        res_maskable = pwa.resolve_static_file("icon-512-maskable.png")
        self.assertIsNotNone(res_maskable)
        assert res_maskable is not None
        ctype, data = res_maskable
        self.assertEqual(ctype, "image/png")
        self.assertTrue(len(data) > 0)

        res_ico = pwa.resolve_static_file("favicon.ico")
        self.assertIsNotNone(res_ico)
        assert res_ico is not None
        ctype, data = res_ico
        self.assertEqual(ctype, "image/x-icon")
        self.assertTrue(len(data) > 0)

        # Path traversal and invalid names are refused
        self.assertIsNone(pwa.resolve_static_file("../pyproject.toml"))
        self.assertIsNone(pwa.resolve_static_file("sub/file.png"))
        self.assertIsNone(pwa.resolve_static_file(".hidden"))
        self.assertIsNone(pwa.resolve_static_file("nonexistent.png"))


class PWAFoundationServerIntegrationTests(WebServerCase):
    """End-to-end HTTP integration tests for PWA routes and cache contracts."""

    def test_pwa_routes_are_unauthenticated_and_served_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Server with mandatory token authentication
            _, port = self.start(tmp)

            # 1. /manifest.webmanifest without auth
            code, headers, body = self.get(port, "/manifest.webmanifest")
            self.assertEqual(code, 200)
            self.assertIn("application/manifest+json", headers.get("Content-Type", ""))
            self.assertIn("public, max-age=", headers.get("Cache-Control", ""))
            manifest = json.loads(body)
            self.assertEqual(manifest["name"], "Print Handy")
            self.assertEqual(manifest["scope"], "/")

            # Alias /manifest.json without auth
            code_json, headers_json, body_json = self.get(port, "/manifest.json")
            self.assertEqual(code_json, 200)
            self.assertEqual(body, body_json)

            # 2. /sw.js without auth
            code, headers, body = self.get(port, "/sw.js")
            self.assertEqual(code, 200)
            self.assertIn("application/javascript", headers.get("Content-Type", ""))
            self.assertEqual(headers.get("Service-Worker-Allowed"), "/")
            self.assertEqual(headers.get("Cache-Control"), "no-cache")
            self.assertIn("addEventListener('install'", body)

            # 3. /offline without auth
            code, headers, body = self.get(port, "/offline")
            self.assertEqual(code, 200)
            self.assertIn("text/html", headers.get("Content-Type", ""))
            self.assertIn("Print Handy", body)
            self.assertIn("Offline", body)

            # 4. /favicon.ico without auth
            code, headers, raw = self.request(port, "GET", "/favicon.ico")
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Content-Type"), "image/x-icon")
            self.assertTrue(len(raw) > 0)

            # 5. /static/icon-*.png without auth
            for icon_name in ("icon-192.png", "icon-512.png", "icon-512-maskable.png", "icon-180.png"):
                code, headers, raw = self.request(port, "GET", f"/static/{icon_name}")
                self.assertEqual(code, 200, f"Failed for {icon_name}")
                self.assertEqual(headers.get("Content-Type"), "image/png")
                self.assertIn("public, max-age=", headers.get("Cache-Control", ""))
                self.assertTrue(len(raw) > 0)

    def test_static_routes_404_on_missing_or_traversal_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)

            # Missing static file
            code, _, raw = self.request(port, "GET", "/static/does-not-exist.png")
            self.assertEqual(code, 404)
            payload = json.loads(raw)
            self.assertFalse(payload["ok"])

            # Directory traversal attempt
            code, _, _ = self.request(port, "GET", "/static/../pyproject.toml")
            self.assertEqual(code, 404)

    def test_dynamic_pages_keep_no_store_cache_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, port = self.start(tmp)

            # Unauthenticated login page
            code, headers, _ = self.get(port, "/login")
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")

            # Authenticated status page
            code, headers, _ = self.get(port, "/", headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")

            # Authenticated config page
            code, headers, _ = self.get(port, "/config", headers=self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(headers.get("Cache-Control"), "no-store")

            # API routes do not serve no-store html or static assets
            code, headers, raw = self.request(port, "GET", "/api/status", headers=self.auth())
            self.assertEqual(code, 200)
            self.assertIn("application/json", headers.get("Content-Type", ""))
