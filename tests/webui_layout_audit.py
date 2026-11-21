"""Explicit GET route inventory and rendered-page samples for routes.py."""

# Route pattern -> response class. Keep every pattern from routes.py in exactly
# one mapping. Rendered pages are sampled below at all three viewport sizes.
PAGE_PATTERNS = {
    "/login": "HTML page", "/offline": "HTML page", "/u/*": "HTML token page",
    "/walkthrough": "HTML page", "/": "HTML dashboard", "/data": "HTML page",
    "/wrapped": "HTML page", "/firmware": "HTML page", "/cost": "HTML page",
    "/spools": "HTML page", "/printers": "HTML page", "/printers/*/screen": "HTML page",
    "/spools/*": "HTML page", "/config": "HTML page", "/printer-settings": "HTML page",
    "/chat": "HTML page", "/workflow": "HTML page", "/upload": "HTML page",
    "/sources": "HTML page", "/designs": "HTML page", "/designs/*": "HTML page",
    "/sliced": "HTML page", "/sliced/*": "HTML page", "/external-intake": "HTML page",
    "/firmware/update": "HTML confirmation or redirect (HTML navigation route)",
    "/firmware/channel": "HTML confirmation or redirect (HTML navigation route)",
    "/conversion-report/*": "HTML report for browser navigation; plain text by query",
}
NON_PAGE_PATTERNS = {
    "/healthz": "JSON health probe", "/setup": "redirect", "/favicon.ico": "binary icon",
    "/static/*": "static/binary asset", "/manifest.webmanifest": "JSON manifest",
    "/manifest.json": "JSON manifest alias", "/sw.js": "service worker JavaScript",
    "/theme": "compatibility redirect", "/api/status": "JSON API",
    "/api/printers": "JSON API", "/diagnostics/download": "binary download",
    "/api/data": "JSON/data API", "/api/wrapped": "JSON/data API",
    "/wrapped/video-file": "binary download", "/api/wrapped/video-status": "JSON API",
    "/wrapped/reel-file": "binary download", "/api/wrapped/reel-status": "JSON API",
    "/api/cost": "JSON API alias", "/readyz": "JSON readiness probe",
    "/api/printers/*": "JSON API", "/chat/state": "JSON API", "/workflow/state": "JSON API",
    "/workflow/download/*": "binary download", "/designs/state": "JSON API",
    "/settings-report/*": "JSON download", "/editable-export/*": "binary/config download",
    "/slice-result/*": "JSON API", "/u1-feasibility/*": "JSON API",
    "*": "catch-all not-found response",
}

PAGE_ROUTES = {
    "/login": "login", "/offline": "offline",
    "/walkthrough": "walkthrough", "/": "dashboard", "/data": "seeded data",
    "/wrapped": "wrapped", "/firmware": "firmware", "/firmware/update": "firmware update",
    "/firmware/channel": "firmware channel",
    "/cost": "cost", "/spools": "seeded spool index", "/printers": "printers",
    "/config": "configuration", "/printer-settings": "printer settings", "/chat": "seeded chat",
    "/workflow": "workflow", "/upload": "upload", "/sources": "sources",
    "/designs": "seeded designs",
    "/sliced": "seeded archive",
    "/external-intake": "external intake",
}
VIEWPORTS = ((375, 812, "mobile"), (768, 1024, "tablet"), (1440, 900, "desktop"))


def assert_route_inventory():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "print_handy/webui/routes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    actual = {node.args[0].value for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "_get" and node.args
              and isinstance(node.args[0], ast.Constant)}
    page_patterns = set(PAGE_PATTERNS)
    non_page_patterns = set(NON_PAGE_PATTERNS)
    assert page_patterns.isdisjoint(non_page_patterns), "GET route classification overlap"
    assert actual == page_patterns | non_page_patterns, (
        f"GET route inventory drift: unclassified={sorted(str(item) for item in actual - page_patterns - non_page_patterns)}, "
        f"stale={sorted(str(item) for item in (page_patterns | non_page_patterns) - actual)}")
