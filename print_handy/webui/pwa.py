"""PWA foundation support for the Print Handy Web UI (#450 Phase 1).

Builds the web app manifest, the service worker script and the offline
fallback page. The routes are declared in
:mod:`print_handy.webui.routes` with the ``public`` policy and answered
by handlers in :mod:`print_handy.webui.server`; this module only builds
the payloads. Every icon, the maskable one included, is served by the
server's single icon allowlist (``ICON_FILES``, ``/favicon.ico`` and
``/static/<name>``); nothing here resolves or serves files.

The precache holds the manifest, the offline page and the maskable
icon; the other icons are runtime-cached when the browser fetches them.
The cache name is derived from the package version and the bytes of
the manifest and the static icons, so changing any of them rotates the
cache and the worker's activate step drops the old one.

Caching is deliberately narrow. API responses (``/api/*``), token
upload links (``/u/*``), the login page, and every non-GET request
bypass the service worker, and navigation responses are never stored,
so stale printer state is never served as if it were live.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import __version__
from . import shell

STATIC_DIR = Path(__file__).resolve().parent / "static"

# The precached install assets. The other manifest icons are
# runtime-cached when fetched, never precached (a precache entry that
# 404s fails the whole install).
PRECACHE_URLS = [
    "/manifest.webmanifest",
    "/offline",
    "/static/icon-512-maskable.png",
]

MANIFEST = {
    "name": "Print Handy",
    "short_name": "Print Handy",
    # A stable explicit id: PWA identity must not silently follow a
    # future start_url change. Each self-hosted instance is its own
    # origin, so "/" identifies the app on that origin.
    "id": "/",
    "description": "Local-first 3D printer companion for Bambu Lab, Klipper, Flashforge, Prusa, and more.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    # The shell tokens (shell.py): the dark background. The Web UI is
    # dark only, so the manifest and the theme-color meta agree.
    "background_color": "#12110f",
    "theme_color": "#12110f",
    "orientation": "any",
    "icons": [
        {
            "src": "/static/icon-192.png",
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/static/icon-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any",
        },
        {
            # A separate maskable artwork: the mascot sits inside the
            # safe zone (about 10% padding) instead of filling the
            # frame like the "any" icons.
            "src": "/static/icon-512-maskable.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
        {
            "src": "/static/icon-180.png",
            "sizes": "180x180",
            "type": "image/png",
            "purpose": "any",
        },
        {
            "src": "/favicon.ico",
            "sizes": "32x32 16x16",
            "type": "image/x-icon",
        },
    ],
    "categories": ["utilities", "productivity"],
}

# The service worker implementation.

def _cache_name() -> str:
    """Cache name tied to the content it caches: the package version plus
    a digest of the manifest and every static icon. Any change to them
    rotates the name, so no release can ship a stale cache by forgetting
    a manual bump."""
    digest = hashlib.sha256(json.dumps(MANIFEST, sort_keys=True).encode())
    for path in sorted(STATIC_DIR.glob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return f"ph-static-{__version__}-{digest.hexdigest()[:12]}"


CACHE_NAME = _cache_name()

SW_SCRIPT = f"""// Print Handy Service Worker (PWA Phase 1, #450)
var CACHE_NAME = {json.dumps(CACHE_NAME)};
var PRECACHE_URLS = {json.dumps(PRECACHE_URLS, indent=2)};

self.addEventListener('install', function(event) {{
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {{
      return cache.addAll(PRECACHE_URLS);
    }}).then(function() {{
      return self.skipWaiting();
    }})
  );
}});

self.addEventListener('activate', function(event) {{
  event.waitUntil(
    caches.keys().then(function(keys) {{
      return Promise.all(
        keys.map(function(key) {{
          if (key !== CACHE_NAME) {{
            return caches.delete(key);
          }}
        }})
      );
    }}).then(function() {{
      return self.clients.claim();
    }})
  );
}});

self.addEventListener('fetch', function(event) {{
  // Only handle GET requests; all mutations and other methods bypass the service worker.
  if (event.request.method !== 'GET') {{
    return;
  }}

  var url = new URL(event.request.url);

  // Cross-origin requests are never cached or intercepted.
  if (url.origin !== self.location.origin) {{
    return;
  }}

  // Never cache API responses (/api/*), token upload links (/u/*),
  // the login route, or the theme route: they carry live printer
  // state, credentials, or per-visitor choices.
  if (
    url.pathname.indexOf('/api/') === 0 ||
    url.pathname.indexOf('/u/') === 0 ||
    url.pathname === '/login' ||
    url.pathname === '/theme'
  ) {{
    return;
  }}

  // Static install assets: cache-first (with network fallback).
  var isStatic =
    url.pathname.indexOf('/static/') === 0 ||
    url.pathname === '/favicon.ico' ||
    url.pathname === '/manifest.webmanifest' ||
    url.pathname === '/manifest.json' ||
    url.pathname === '/offline';

  if (isStatic) {{
    event.respondWith(
      caches.match(event.request).then(function(cached) {{
        if (cached) {{
          return cached;
        }}
        return fetch(event.request).then(function(response) {{
          if (response && response.status === 200) {{
            var copy = response.clone();
            caches.open(CACHE_NAME).then(function(cache) {{
              cache.put(event.request, copy);
            }});
          }}
          return response;
        }});
      }})
    );
    return;
  }}

  // HTML navigation requests: network-only with the offline fallback.
  // The navigation response itself is never cached, so an
  // authenticated page or live printer state is never replayed.
  if (event.request.mode === 'navigate') {{
    event.respondWith(
      fetch(event.request).catch(function() {{
        return caches.match('/offline');
      }})
    );
    return;
  }}
}});
"""


def manifest_json() -> str:
    """The JSON representation of the web app manifest."""
    return json.dumps(MANIFEST, indent=2)


def render_offline_page(theme: str = shell.DEFAULT_THEME) -> str:
    """The themed standalone offline fallback page."""
    body = (
        '<div class="card" style="text-align: center; padding: 2rem 1rem;">'
        '<h2>Print Handy is currently unreachable</h2>'
        '<p class="muted">Check your local network connection, VPN, or reverse proxy status.</p>'
        '<p style="margin-top: 1.5rem;">'
        '<button type="button" onclick="window.location.reload()" class="accent">Try Reconnecting</button>'
        '</p>'
        '</div>'
    )
    return shell.page("Offline", body, path="/offline", theme=theme, chrome=False, heading="Offline")


__all__ = [
    "CACHE_NAME", "MANIFEST", "PRECACHE_URLS", "SW_SCRIPT",
    "manifest_json", "render_offline_page",
]
