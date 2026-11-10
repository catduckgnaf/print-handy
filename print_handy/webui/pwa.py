"""PWA foundation support for Print Handy Web UI (#450 Phase 1).

Provides the web app manifest, service worker script, offline fallback shell,
and static install asset resolution. Adheres to strict non-caching rules:
authenticated pages, API responses (/api/*), printer control actions, camera
snapshots, and token upload routes are never cached.
"""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

from . import shell

STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_BRANDING_DIR = Path(__file__).resolve().parents[2] / "assets" / "branding"

# Cache name versioned for Phase 1.
CACHE_NAME = "ph-static-v1"

# The pre-cached static install assets.
PRECACHE_URLS = [
    "/manifest.webmanifest",
    "/favicon.ico",
    "/offline",
    "/static/icon-16.png",
    "/static/icon-32.png",
    "/static/icon-64.png",
    "/static/icon-180.png",
    "/static/icon-192.png",
    "/static/icon-512.png",
    "/static/icon-512-maskable.png",
]

MANIFEST = {
    "name": "Print Handy",
    "short_name": "Print Handy",
    "description": "Local-first 3D printer companion for Bambu Lab, Klipper, Flashforge, Prusa, and more.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
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

# The Service Worker implementation.
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

  // Security-conscious caching policy:
  // NEVER cache API responses (/api/*), printer control actions, camera snapshots,
  // login/auth routes, token upload endpoints (/u/*), or dynamic user configurations.
  if (
    url.pathname.indexOf('/api/') === 0 ||
    url.pathname.indexOf('/u/') === 0 ||
    url.pathname === '/login' ||
    url.pathname === '/theme'
  ) {{
    return;
  }}

  // Static assets: Cache-First (with network fallback)
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

  // HTML Navigation requests: Network-Only with offline fallback.
  // We NEVER cache the navigation response itself to prevent leaking or caching
  // authenticated pages (e.g. status, config, spools).
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

SW_REGISTER_SCRIPT = (
    "<script>"
    "if('serviceWorker' in navigator){"
    "window.addEventListener('load',function(){"
    "navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){});"
    "});"
    "}"
    "</script>"
)

PWA_HEAD_TAGS = (
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="apple-mobile-web-app-title" content="Print Handy">'
    '<meta name="theme-color" content="#12110f" media="(prefers-color-scheme: dark)">'
    '<meta name="theme-color" content="#f3efe6" media="(prefers-color-scheme: light)">'
    '<link rel="manifest" href="/manifest.webmanifest">'
    '<link rel="icon" type="image/x-icon" href="/favicon.ico">'
    '<link rel="icon" type="image/png" sizes="32x32" href="/static/icon-32.png">'
    '<link rel="icon" type="image/png" sizes="16x16" href="/static/icon-16.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/static/icon-180.png">'
)


def manifest_json() -> str:
    """The JSON representation of the Web App Manifest."""
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


def resolve_static_file(name: str) -> Optional[Tuple[str, bytes]]:
    """Resolve a safe static asset file by name from package static or repo branding."""
    # Prevent directory traversal: allow only simple filenames without slashes
    if "/" in name or "\\" in name or name.startswith("."):
        return None

    path = STATIC_DIR / name
    if not path.is_file():
        path = REPO_BRANDING_DIR / name
        if not path.is_file():
            return None

    content_type, _ = mimetypes.guess_type(name)
    if name.endswith(".ico"):
        content_type = "image/x-icon"
    elif name.endswith(".webmanifest"):
        content_type = "application/manifest+json; charset=utf-8"
    elif not content_type:
        content_type = "application/octet-stream"

    return content_type, path.read_bytes()
