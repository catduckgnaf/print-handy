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
    "/firmware/update/status": "JSON API", "/firmware/channel/status": "JSON API",
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
    "/spools/flashforge-ad5x": "seeded per-printer spools",
    "/printers/flashforge-ad5x/screen": "seeded printer screen",
    "/config": "configuration", "/printer-settings": "printer settings", "/chat": "seeded chat",
    "/workflow": "workflow", "/upload": "upload", "/sources": "sources",
    "/designs": "seeded designs",
    "/designs/seeded-design-000001": "seeded design detail",
    "/sliced": "seeded archive",
    "/external-intake": "external intake",
}
VIEWPORTS = ((375, 812, "mobile"), (768, 1024, "tablet"), (1440, 900, "desktop"))

# Page patterns with no rendered page to audit, with the reason each is not
# a PAGE_ROUTES entry. The route-inventory test requires this to be exactly
# the set of page patterns no audited route instantiates, so a new pattern
# cannot slip through unaudited: it must be sampled or listed here.
UNSAMPLED_PAGE_PATTERNS = {
    # Sub-paths are plate file downloads, not rendered pages; the /sliced
    # index is audited above.
    "/sliced/*": "plate file downloads, no rendered page",
    # A token page; no seeded upload token exists to visit.
    "/u/*": "needs a minted upload token",
    # Needs a conversion report id; none is seeded.
    "/conversion-report/*": "needs a conversion report id",
}


def pattern_for_route(route):
    """The PAGE_PATTERNS entry a concrete audited route instantiates, or None.

    Exact routes match themselves; a route with a concrete segment where
    the pattern has ``*`` instantiates that pattern (``*`` covers exactly
    one segment, so ``/spools/printer`` instantiates ``/spools/*``).
    """
    if route in PAGE_PATTERNS:
        return route
    parts = route.strip("/").split("/")
    for pattern in PAGE_PATTERNS:
        pattern_parts = pattern.strip("/").split("/")
        if len(pattern_parts) != len(parts):
            continue
        if all(p == "*" or p == r for p, r in zip(pattern_parts, parts, strict=True)):
            return pattern
    return None


# Mobile tap targets under 44px, as the audit found them on the seeded pages
# (CI run 44945090510 on head 8e09dd85). The shell's controls are ~41px tall
# (SELECTs 39px) on every page, so the strict 44px check fails the whole
# mobile surface; that is the mobile issue #1737's scope, not this audit
# PR's, and restyling every control here would bury the audit's own signal.
# The findings are therefore pinned, not ignored: the test fails on ANY
# deviation from this map -- a new small target, a fixed one, or a renamed
# one -- so the pin must be updated deliberately when the layout changes.
# Each entry is a multiset of (tag, label) pairs, the label being the
# audit's own 60-character truncated text; dimensions are not pinned.
EXPECTED_MOBILE_SMALL_TARGETS = {
    '/': {
        ('BUTTON', 'Turn off'): 1,
        ('BUTTON', 'Turn on'): 1,
        ('INPUT', ''): 1,
    },
    '/chat': {
        ('BUTTON', 'Send'): 1,
        ('BUTTON', 'Upload and edit'): 1,
        ('INPUT', ''): 3,
    },
    '/config': {
        ('INPUT', ''): 3,
        ('SELECT', '3mf\nauto\ngcode'): 1,
        ('SELECT', 'Anycubic 4Max Pro 2 (OctoPrint)\nAnycubic Chiron (OctoPrint)\n'): 1,
    },
    '/cost': {
        ('BUTTON', 'Calculate cost'): 1,
        ('BUTTON', 'Read file'): 1,
        ('INPUT', ''): 13,
        ('SELECT', 'Filament (FDM)\nResin (MSLA)'): 1,
    },
    '/data': {
        ('BUTTON', 'Filter'): 1,
        ('SELECT', 'All printers\nSynthetic AD5X (40)'): 1,
        ('SELECT', 'Everyone\nUnattributed (40)'): 1,
    },
    '/login': {
        ('BUTTON', 'Log in'): 1,
        ('INPUT', ''): 2,
    },
    '/offline': {
        ('BUTTON', 'Try Reconnecting'): 1,
    },
    '/printer-settings': {
        ('BUTTON', 'Save changes'): 1,
        ('INPUT', ''): 2,
        ('SELECT', 'Anycubic 4Max Pro 2 (OctoPrint)\nAnycubic Chiron (OctoPrint)\n'): 1,
        ('SELECT', 'auto\ngcode\n3mf'): 1,
        ('SELECT', 'orca'): 1,
    },
    '/printers': {
        ('BUTTON', 'Add printer'): 1,
        ('INPUT', ''): 3,
        ('SELECT', 'Pick a model\nAnycubic 4Max Pro 2 (OctoPrint)\nAnycubic Chiron'): 1,
    },
    '/sources': {
        ('BUTTON', 'Start the workflow'): 1,
        ('INPUT', ''): 1,
    },
    '/spools': {
        ('BUTTON', 'Add spool'): 1,
        ('BUTTON', 'Assign'): 1,
        ('BUTTON', 'Load'): 5,
        ('BUTTON', 'Save changes'): 1,
        ('BUTTON', 'Set'): 13,
        ('BUTTON', 'Swap'): 1,
        ('BUTTON', 'Sync spools from Spoolman'): 1,
        ('BUTTON', 'Unassign'): 1,
        ('INPUT', ''): 25,
        ('SELECT', '(no brand)\neSun\nSunlu\nOther brand…'): 1,
        ('SELECT', '(the shelf)\nSlot 1\nSlot 2\nSlot 3\nSlot 4'): 1,
        ('SELECT', '(unchanged)\nBlack\nWhite\nGray\nRed\nOrange\nYellow\nGreen\nBlue\nPu'): 1,
        ('SELECT', '(unchanged)\nPLA (plain)\nPLA+\nSilk PLA\nMatte PLA\nPETG\nPCTG\nTP'): 1,
        ('SELECT', '(unchanged)\neSun\nSunlu\nOther brand…'): 1,
        ('SELECT', 'Black\nWhite\nGray\nRed\nOrange\nYellow\nGreen\nBlue\nPurple\nPink\nBr'): 1,
        ('SELECT', 'Coffee · PLA+ · #21 · 412.038 g left\nBlack · PLA · #22 · 12.'): 1,
        ('SELECT', 'PLA (plain)\nPLA+\nSilk PLA\nMatte PLA\nPETG\nPCTG\nTPU\nABS\nASA\nPA'): 1,
        ('SELECT', 'Pink · PLA · #20 · 865.722 g left\nCoffee · PLA+ · #21 · 412.'): 1,
        ('SELECT', 'Slot 1\nSlot 2\nSlot 3\nSlot 4'): 7,
        ('SELECT', 'Slot 1: Pink · PLA · #20 · 865.722 g left\nSlot 2: Coffee · P'): 2,
    },
    '/spools/flashforge-ad5x': {
        ('BUTTON', 'Add spool'): 1,
        ('BUTTON', 'Assign'): 1,
        ('BUTTON', 'Load'): 5,
        ('BUTTON', 'Save changes'): 1,
        ('BUTTON', 'Set'): 13,
        ('BUTTON', 'Swap'): 1,
        ('BUTTON', 'Sync spools from Spoolman'): 1,
        ('BUTTON', 'Unassign'): 1,
        ('INPUT', ''): 25,
        ('SELECT', '(no brand)\neSun\nSunlu\nOther brand…'): 1,
        ('SELECT', '(the shelf)\nSlot 1\nSlot 2\nSlot 3\nSlot 4'): 1,
        ('SELECT', '(unchanged)\nBlack\nWhite\nGray\nRed\nOrange\nYellow\nGreen\nBlue\nPu'): 1,
        ('SELECT', '(unchanged)\nPLA (plain)\nPLA+\nSilk PLA\nMatte PLA\nPETG\nPCTG\nTP'): 1,
        ('SELECT', '(unchanged)\neSun\nSunlu\nOther brand…'): 1,
        ('SELECT', 'Black\nWhite\nGray\nRed\nOrange\nYellow\nGreen\nBlue\nPurple\nPink\nBr'): 1,
        ('SELECT', 'Coffee · PLA+ · #21 · 412.038 g left\nBlack · PLA · #22 · 12.'): 1,
        ('SELECT', 'PLA (plain)\nPLA+\nSilk PLA\nMatte PLA\nPETG\nPCTG\nTPU\nABS\nASA\nPA'): 1,
        ('SELECT', 'Pink · PLA · #20 · 865.722 g left\nCoffee · PLA+ · #21 · 412.'): 1,
        ('SELECT', 'Slot 1\nSlot 2\nSlot 3\nSlot 4'): 7,
        ('SELECT', 'Slot 1: Pink · PLA · #20 · 865.722 g left\nSlot 2: Coffee · P'): 2,
    },
    '/upload': {
        ('BUTTON', 'Create an upload link'): 1,
        ('BUTTON', 'Upload to inbox'): 1,
        ('INPUT', ''): 1,
    },
    '/walkthrough': {
        ('BUTTON', 'Continue'): 1,
        ('BUTTON', 'Skip setup'): 1,
        ('BUTTON', 'Skip this step'): 1,
        ('INPUT', ''): 3,
        ('SELECT', '3mf\nauto\ngcode'): 1,
        ('SELECT', 'Anycubic 4Max Pro 2 (OctoPrint)\nAnycubic Chiron (OctoPrint)\n'): 1,
    },
    '/workflow': {
        ('BUTTON', 'Start the workflow'): 1,
        ('BUTTON', 'Upload and start'): 1,
        ('INPUT', ''): 2,
    },
    '/wrapped': {
        ('BUTTON', 'Export share bundle'): 1,
        ('BUTTON', 'Filter'): 1,
        ('BUTTON', 'Make my Wrapped video'): 1,
        ('BUTTON', 'Show'): 1,
        ('INPUT', ''): 6,
        ('SELECT', '2026'): 1,
        ('SELECT', 'All printers\nSynthetic AD5X (40)'): 1,
        ('SELECT', 'Everyone\nUnattributed (40)'): 1,
        ('SELECT', 'link-only (recommended)\npublic\nprivate (not available yet)'): 1,
    },
}


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
