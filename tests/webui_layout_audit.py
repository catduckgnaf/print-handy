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
    "/sliced": "HTML page", "/external-intake": "HTML page",
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
    "/sliced/*": "plate attachment (the page is /sliced)",
    "/settings-report/*": "JSON download", "/editable-export/*": "binary/config download",
    "/slice-result/*": "JSON API", "/u1-feasibility/*": "JSON API",
    "*": "catch-all not-found response",
}

# Pattern -> (browser URL, required body text), not just status 200. A
# required selector takes the place of text where a form/image is the contract.
PAGE_ROUTES = {
    "/login": ("/login", None), "/offline": ("/offline", "Try Reconnecting"),
    "/walkthrough": ("/walkthrough", None), "/": ("/", "Flashforge AD5X"),
    "/data": ("/data", "Synthetic AD5X"), "/wrapped": ("/wrapped", None),
    "/firmware": ("/firmware?printer=fixture-klipper", "v0.12.0-310"),
    "/firmware/update": ("/firmware/update?printer=fixture-klipper&component=klipper", "Update now"),
    "/firmware/channel": ("/firmware/channel?printer=fixture-klipper&component=klipper",
                          "Switch klipper to"),
    "/cost": ("/cost", None), "/spools": ("/spools/flashforge-ad5x", "Weekend pink"),
    "/printers": ("/printers", "Fixture Klipper"),
    "/config": ("/config", None), "/printer-settings": ("/printer-settings", None),
    "/chat": ("/chat", "Slice summary"), "/workflow": ("/workflow", None),
    "/upload": ("/upload", None), "/sources": ("/sources", None),
    "/designs": ("/designs", "Synthetic design session"),
    "/sliced": ("/sliced", "Long synthetic archive job title"),
    "/external-intake": ("/external-intake", None),
    "/u/*": ("/u/{grant}", "Upload a model file"),
    "/spools/*": ("/spools/{printer}", "Weekend pink"),
    "/designs/*": ("/designs/seeded-design-000001", "Synthetic design session 1"),
    "/printers/*/screen": ("/printers/{printer}/screen", None),
    "/conversion-report/*": ("/conversion-report/seeded-multiplate", "layer_height"),
}
PAGE_SELECTORS = {
    "/login": 'input[name="token"]',
    "/walkthrough": 'input[name="step"][value="printer"]',
    "/printers/*/screen": "img#printer-screen",
}
VIEWPORTS = ((375, 812, "mobile"), (768, 1024, "tablet"), (1440, 900, "desktop"))


def measure_layout(page):
    return page.evaluate("""() => {
      const doc = document.documentElement;
      const describe = e => ({tag: e.tagName, id: e.id, name: e.getAttribute('name'),
        href: e.getAttribute('href'), cls: String(e.className).slice(0, 100),
        text: (e.getAttribute('aria-label') || e.innerText || '').trim().slice(0, 80)});
      const inScroller = e => e.closest('[data-horizontal-scroll], .table-scroll, .overflow-x-auto');
      const hasOwnScroll = e => getComputedStyle(e).overflowX === 'auto' ||
        getComputedStyle(e).overflowX === 'scroll';
      const visible = e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility === 'visible';
      };
      const overflow = [...document.querySelectorAll('body *')].filter(visible)
        .filter(e => !inScroller(e) && !hasOwnScroll(e) &&
          e.getBoundingClientRect().right > innerWidth + 2)
        .map(e => ({...describe(e), scroll: e.scrollWidth, client: e.clientWidth}));
      const oneChar = [...document.querySelectorAll('body *')].filter(visible)
        .filter(e => e.children.length === 0 && /[A-Za-z]{4,}/.test(e.innerText || ''))
        .filter(e => { const r = e.getBoundingClientRect(); return r.height > r.width * 5; })
        .map(describe);
      const inlineLink = e => e.matches('a') && getComputedStyle(e).display === 'inline'
        && !e.closest('nav, header') && !e.matches('.button, [role=button]');
      const interactive = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button]';
      const smallTargets = [...document.querySelectorAll(interactive)].filter(visible)
        .filter(e => !inlineLink(e) && !e.matches(':disabled, [aria-disabled=true]'))
        .map(e => ({...describe(e), width: e.getBoundingClientRect().width,
                                   height: e.getBoundingClientRect().height,
                                   kind: e.matches('nav a[href], header a[href]') ? 'nav' :
                                     e.matches('button, [role=button]') ? 'button' :
                                     e.matches('input, select, textarea') ? 'field' :
                                     e.matches('summary') ? 'summary' : 'other'}))
        .filter(e => e.width < 44 || e.height < 44);
      return {docWidth: doc.scrollWidth, overflow, oneChar, smallTargets};
    }""")


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
