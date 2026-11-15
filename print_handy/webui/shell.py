# Shared page chrome is the frame around the controls, not their authority.
"""The one page shell for every Web UI page (#813).

Before #813 each page module assembled its own bare HTML — no
stylesheet anywhere, browser-default rendering, light only. This module
owns the single shell they all render through now: the document head
with one inline ``<style>`` (the design tokens and component styles
ported from the console prototype catduck supplied for #813), the
header chrome (brand tile, pill navigation over the real surfaces, the
Dark/Light theme toggle), and the theme rule.

The stylesheet is inline rather than a served file: the Web UI stays
single-process, with no external request of any kind — the font stacks
name system fonts only, exactly as they fall back in the prototype.
The one served-asset exception is the PWA foundation (#450):
PWA_HEAD_TAGS links the manifest the public PWA routes serve, and
PWA_SCRIPT registers the service worker on secure contexts only
(see :mod:`print_handy.webui.pwa`).

Theme: dark is the default. The ``ph-theme`` cookie (``dark`` or
``light``) is read by the server and stamped on ``<html>`` as
``data-theme`` before anything renders, so there is no flash and the
choice is correct with JavaScript disabled. The toggle in the header is
two plain links to ``GET /theme`` (see server.py), which sets the cookie
and redirects back — no script is involved anywhere in the theme flow.
The cookie is a browser presentation preference only; it is not a
setting and no environment variable mirrors it.

Every page also carries a source footer (#1158): Print Handy is
AGPL-3.0-only, and section 13 of that license expects a user
interacting over a network to be offered the corresponding source.
The footer names the running version and links the source tree at
that version's tag. It renders on bare (``chrome=False``) pages too —
the login page is the first page a network user reaches.
"""
from __future__ import annotations

import html
from http.cookies import SimpleCookie
from urllib.parse import quote

from .. import __version__

THEME_COOKIE = "ph-theme"
THEMES = ("dark", "light")
DEFAULT_THEME = "dark"

# The surfaces the header navigates to: only pages that exist. (The
# Spools page arrived with #789 while this shell was being built; the
# header picked it up on the rebase rather than inventing it early.)
NAV = (
    ("/", "Status"),
    ("/sliced", "Sliced files"),
    ("/workflow", "Workflow"),
    ("/printers", "Printers"),
    ("/printer-settings", "Printer settings"),
    ("/spools", "Spools"),
    ("/designs", "Designs"),
    ("/projects", "Projects"),
    ("/firmware", "Firmware"),
    ("/timing", "Timing"),
    ("/config", "Settings"),
)

# The project's source repository (the same address the setup wizard
# and the model-site guides use), linked from every page's footer.
# Releases are tagged with the bare version (for example "2.2.7"), so
# the source tree for the running version is SOURCE_URL + "/tree/" +
# __version__.
SOURCE_URL = "https://github.com/catduckgnaf/print-handy"

CSS = """
:root {
  --color-bg: #12110f;
  --color-surface: #1c1a17;
  --color-ink: #f3efe6;
  --color-muted: #a89b8c;
  --color-line: #34302b;
  --color-accent: #e07a3d;
  --color-accent-ink: #1a100c;
  --color-ok: #8dcea8;
  --color-ok-bg: #1a3326;
  --color-warn: #e4c07a;
  --color-warn-bg: #3a2e14;
  --color-bad: #f0a8a2;
  --color-bad-bg: #3a1c1c;
  --font-sans: "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, monospace;
  --radius-card: 0.75rem;
  color-scheme: dark;
}
html[data-theme="light"] {
  --color-bg: #f3efe6;
  --color-surface: #fffcf7;
  --color-ink: #1a1814;
  --color-muted: #6b645b;
  --color-line: #e3dcd0;
  --color-accent: #c45c26;
  --color-accent-ink: #fff8f3;
  --color-ok: #1f6b45;
  --color-ok-bg: #e5f2ea;
  --color-warn: #8a5a12;
  --color-warn-bg: #f8efd8;
  --color-bad: #8f2d2d;
  --color-bad-bg: #f8e6e4;
  color-scheme: light;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--color-bg);
  color: var(--color-ink);
  font-family: var(--font-sans);
  line-height: 1.45;
}
a { color: var(--color-accent); }
code, .mono { font-family: var(--font-mono); font-size: 0.92em; }
.wrap { max-width: 64rem; margin: 0 auto; padding: 0 1rem; }
header.site {
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-line);
}
header.site .wrap {
  display: flex; flex-wrap: wrap; align-items: center;
  justify-content: space-between; gap: 0.75rem; padding-top: 0.75rem; padding-bottom: 0.75rem;
}
.brand { display: flex; align-items: center; gap: 0.75rem; }
.brand .tile {
  display: grid; place-items: center; width: 2.25rem; height: 2.25rem;
  border-radius: var(--radius-card); background: var(--color-accent);
  color: var(--color-accent-ink); font-weight: 600; font-size: 0.875rem;
}
.brand .name { font-weight: 600; font-size: 0.875rem; line-height: 1.2; }
.brand .sub { font-family: var(--font-mono); font-size: 0.75rem; color: var(--color-muted); }
.chrome-right { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; }
nav.pills { display: flex; flex-wrap: wrap; gap: 0.25rem; }
nav.pills a {
  padding: 0.5rem 0.75rem; border-radius: var(--radius-card);
  color: var(--color-ink); text-decoration: none; font-size: 0.875rem; font-weight: 500;
}
nav.pills a:hover { background: var(--color-bg); }
nav.pills a.active { background: var(--color-ink); color: var(--color-bg); }
.theme-toggle {
  display: flex; gap: 0.125rem; padding: 0.125rem;
  border: 1px solid var(--color-line); border-radius: var(--radius-card);
}
.theme-toggle a {
  padding: 0.375rem 0.625rem; border-radius: calc(var(--radius-card) - 0.125rem);
  color: var(--color-muted); text-decoration: none; font-size: 0.875rem;
}
.theme-toggle a.active { background: var(--color-ink); color: var(--color-bg); }
main .wrap { padding-top: 1.5rem; padding-bottom: 3rem; }
h1 { font-size: 1.5rem; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 1rem; }
h2 { font-size: 1.125rem; font-weight: 600; margin: 1.75rem 0 0.75rem; }
h3 { font-size: 1rem; font-weight: 600; margin: 1.25rem 0 0.5rem; }
p { margin: 0.5rem 0; }
.eyebrow {
  font-size: 0.75rem; font-weight: 500; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--color-muted); margin: 0 0 0.25rem;
}
.muted { color: var(--color-muted); }
.site-footer { font-size: 0.85rem; color: var(--color-muted); padding: 0 0 2rem; }
.card {
  background: var(--color-surface); border: 1px solid var(--color-line);
  border-radius: var(--radius-card); padding: 1rem; margin: 1rem 0;
}
.card > :first-child { margin-top: 0; }
.card > :last-child { margin-bottom: 0; }
table {
  width: 100%; border-collapse: separate; border-spacing: 0;
  border: 1px solid var(--color-line); border-radius: var(--radius-card);
  overflow: hidden; margin: 1rem 0; font-size: 0.9375rem;
}
th, td {
  padding: 0.5rem 0.75rem; text-align: left; vertical-align: top;
  border-bottom: 1px solid var(--color-line);
}
tr:last-child > th, tr:last-child > td { border-bottom: none; }
th { color: var(--color-muted); font-weight: 500; }
td, th { overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; margin: 1rem 0; }
.table-scroll > table { margin: 0; }
form label { display: block; margin: 0 0 0.75rem; font-weight: 500; }
form label > input:not([type=checkbox]), form label > select { margin-top: 0.375rem; }
table.rows th { width: 12rem; }
form.inline { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin: 0.25rem 0; }
form.inline label { display: inline-flex; align-items: center; gap: 0.375rem; margin: 0; }
form.inline label > input:not([type=checkbox]), form.inline label > select { width: auto; margin-top: 0; }
form.inline input[type=number] { max-width: 7rem; }
.field { margin: 0 0 1.25rem; }
.field > label, .field .flabel { display: block; font-weight: 500; margin-bottom: 0.375rem; }
.badge {
  display: inline-block; padding: 0.1rem 0.5rem; margin-left: 0.375rem;
  border: 1px solid var(--color-line); border-radius: 999px;
  font-size: 0.75rem; font-weight: 400; color: var(--color-muted);
  vertical-align: middle; white-space: nowrap;
}
.badge.ok { background: var(--color-ok-bg); color: var(--color-ok); border-color: transparent; }
.badge.warn { background: var(--color-warn-bg); color: var(--color-warn); border-color: transparent; }
.badge.bad { background: var(--color-bad-bg); color: var(--color-bad); border-color: transparent; }
input, select, textarea {
  width: 100%; padding: 0.5rem 0.75rem; font: inherit;
  background: var(--color-bg); color: var(--color-ink);
  border: 1px solid var(--color-line); border-radius: var(--radius-card);
}
input[type="checkbox"] { width: 1.25rem; height: 1.25rem; accent-color: var(--color-accent); vertical-align: middle; }
input:disabled, select:disabled, textarea:disabled { opacity: 0.55; }
:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }
button, .button {
  display: inline-block; padding: 0.5rem 1rem; font: inherit; font-weight: 500;
  background: var(--color-ink); color: var(--color-bg);
  border: 1px solid transparent; border-radius: var(--radius-card);
  cursor: pointer; text-decoration: none;
}
button.secondary, .button.secondary {
  background: var(--color-surface); color: var(--color-ink); border-color: var(--color-line);
}
button.accent, .button.accent { background: var(--color-accent); color: var(--color-accent-ink); }
button:disabled { opacity: 0.55; cursor: default; }
.error { color: var(--color-bad); }
.help, .note { color: var(--color-muted); font-size: 0.875rem; }
.notice, .banner {
  background: var(--color-surface); border: 1px solid var(--color-line);
  border-radius: var(--radius-card); padding: 0.75rem 1rem; margin: 1rem 0;
}
.banner { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem; }
.overall-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem; }
.overall-head h2 { margin: 0; font-size: 1.375rem; }
.progress {
  height: 0.5rem; border-radius: 999px; background: var(--color-line);
  overflow: hidden; margin: 0.75rem 0 0.25rem;
}
.progress > i { display: block; height: 100%; background: var(--color-accent); }
.job-line { font-weight: 500; margin: 0.75rem 0 0; overflow-wrap: anywhere; }
.dash-health { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: 0.75rem; margin: 1rem 0; }
.dash-health .stat { background: var(--color-surface); border: 1px solid var(--color-line); border-radius: var(--radius-card); padding: 0.75rem 1rem; }
.dash-health .stat .v { font-size: 1.25rem; font-weight: 600; }
.dash-top { display: flex; flex-wrap: wrap; gap: 1rem; }
.dash-cam { flex: 2 1 20rem; min-width: 0; }
.dash-cam img { width: 100%; display: block; border-radius: var(--radius-card); }
.dash-side { flex: 1 1 15rem; min-width: 0; }
.ring-wrap { position: relative; width: 8.5rem; height: 8.5rem; }
.ring-center { position: absolute; inset: 0; display: grid; place-content: center; text-align: center; }
.ring-center .pct { font-size: 1.6rem; font-weight: 600; line-height: 1.1; }
.temps { display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.75rem; }
.temp-box { border: 1px solid var(--color-line); border-radius: var(--radius-card); padding: 0.5rem 0.75rem; min-width: 7rem; }
.temp-box .v { font-size: 1.375rem; font-weight: 600; }
.temp-chart { width: 100%; height: 6rem; display: block; }
.temp-chart polyline { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; }
.temp-chart .nozzle { stroke: var(--color-accent); }
.temp-chart .bed { stroke: var(--color-warn); }
.temp-chart .target { stroke-dasharray: 6 4; stroke-width: 1.5; opacity: 0.8; }
.temp-row { display: flex; align-items: center; gap: 0.75rem; margin: 0.25rem 0; }
.temp-name { width: 4.5rem; flex: none; font-size: 0.9rem; }
.temp-row .temp-plot { flex: 1; min-width: 0; }
.temp-row .temp-chart { height: 3.5rem; }
.temp-plot { position: relative; padding-left: 3rem; }
.temp-axis { position: absolute; left: 0; font-size: 0.75rem; color: var(--color-muted); }
.temp-axis.top { top: 0; }
.temp-axis.bottom { bottom: 0; }
.key { display: inline-block; width: 0.9rem; height: 0.2rem; margin-right: 0.3rem; vertical-align: middle; }
.key.nozzle { background: var(--color-accent); }
.key.bed { background: var(--color-warn); }
.dash-panel { border-top: 1px solid var(--color-line); margin-top: 1rem; padding-top: 0.75rem; }
.dash-panel h3, .hw-panel h3 { margin: 0 0 0.5rem; font-size: 1rem; }
.dash-job { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.dash-controls { margin-top: 0.75rem; }
.dash-card .job-line, .dash-card p { overflow-wrap: anywhere; }
.disc { box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.25); }
.slot .slot-action { white-space: nowrap; }
.panel-action { margin: 0.5rem 0 0.15rem; }
.hw-more summary, .sys-details summary { cursor: pointer; font-weight: 600; }
.hw-more { margin-top: 0.5rem; }
.sys-details { margin-top: 1rem; }
.sys-details[open] summary { margin-bottom: 0.75rem; }
.hw-panels { display: flex; flex-wrap: wrap; gap: 1rem; margin: 0.75rem 0; }
.hw-panel { flex: 1 1 15rem; min-width: 0; border: 1px solid var(--color-line); border-radius: var(--radius-card); padding: 0.5rem 0.75rem; }
.hw-defaults { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
.hw-defaults th, .hw-defaults td { text-align: left; padding: 0.15rem 0.4rem; }
button.danger, .button.danger { background: var(--color-bad-bg); color: var(--color-bad); }
button.estop, button.danger.estop { font-weight: 700; padding: 0.6rem 1.1rem; background: #c62828; color: #fff; border: 0; }
button.estop:hover, button.estop:focus-visible { background: #b71c1c; outline: 3px solid #ef9a9a; outline-offset: 2px; }
.slots { display: flex; flex-wrap: wrap; gap: 1rem; margin: 0.75rem 0; }
.slot { width: 6.5rem; text-align: center; }
.slot-link { display: block; color: inherit; text-decoration: none; border: 1px solid transparent; border-radius: 0.5rem; padding: 0.4rem; transition: background-color 120ms ease, border-color 120ms ease, box-shadow 120ms ease; }
.slot-link:hover { background: var(--color-panel); border-color: var(--color-line); }
.slot-link:focus-visible { outline: 3px solid var(--color-accent); outline-offset: 2px; }
.slot-action { display: inline-block; margin-top: 0.35rem; padding: 0.25rem 0.5rem; border: 1px solid var(--color-line); border-radius: 0.35rem; font-size: 0.8rem; font-weight: 600; }
.slot .disc { margin-bottom: 0.375rem; }
.disc { display: inline-block; width: 2.6rem; height: 2.6rem; border-radius: 50%; border: 2px solid var(--color-line); background: var(--color-bg); }
.disc.small { width: 1.4rem; height: 1.4rem; vertical-align: middle; }
.disc.empty { border-style: dashed; }
.shelf-row { display: flex; align-items: center; gap: 0.5rem; margin: 0.25rem 0; }
.recent-row { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.5rem 1rem; padding: 0.5rem 0; border-bottom: 1px solid var(--color-line); }
.recent-row:last-child { border-bottom: none; }
ol.steps { display: flex; flex-wrap: wrap; gap: 0.5rem; list-style: none; margin: 1rem 0; padding: 0; font-size: 0.875rem; }
ol.steps li { padding: 0.3rem 0.75rem; border-radius: var(--radius-card); background: var(--color-surface); color: var(--color-muted); border: 1px solid var(--color-line); }
ol.steps li.done { background: var(--color-ok-bg); color: var(--color-ok); border-color: transparent; }
ol.steps li.current { background: var(--color-ink); color: var(--color-bg); border-color: transparent; font-weight: 500; }
.step-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; }
.step-actions .spacer { flex: 1; }
pre {
  overflow-x: auto; background: var(--color-surface);
  border: 1px solid var(--color-line); border-radius: var(--radius-card); padding: 1rem;
}
img { max-width: 100%; }
@media (max-width: 40rem) {
  table.rows th { width: auto; }
  th, td { padding: 0.45rem 0.5rem; }
}
/* Pasta Wrapped (#1446): the Wrapped page is a scrolling story of
   full-screen sections. Every rule is scoped to .wrapped (or, for
   the scroll behavior, to a document containing one, via :has), so
   no other page's layout or scrolling changes. */
html:has(.wrapped) { scroll-behavior: smooth; }
@media (min-height: 30rem) {
  html:has(.wrapped) { scroll-snap-type: y proximity; }
}
@media (prefers-reduced-motion: reduce) {
  html:has(.wrapped) { scroll-behavior: auto; scroll-snap-type: none; }
}
.wrapped-screen {
  min-height: 100vh; min-height: 100svh;
  display: flex; flex-direction: column; justify-content: center;
  padding: 2rem 0; scroll-snap-align: start;
}
.wrapped > section:nth-of-type(even) {
  background: var(--color-surface); border: 1px solid var(--color-line);
  border-radius: var(--radius-card); padding-left: 1.5rem; padding-right: 1.5rem;
}
.wrapped-screen > h2 { font-size: 1.5rem; margin-top: 0; }
.wrapped-screen > .eyebrow { color: var(--color-accent); }
.wrapped-big {
  font-size: clamp(3rem, 16vw, 8.5rem); font-weight: 700;
  line-height: 1.02; letter-spacing: -0.02em; margin: 0.5rem 0;
  overflow-wrap: anywhere;
}
.wrapped-big-text { font-size: clamp(2rem, 9vw, 4.5rem); }
.wrapped-caption { font-size: 1.125rem; color: var(--color-muted); margin: 0.25rem 0 0; }
.wrapped-lede { font-size: 1.25rem; }
.wrapped-hero h1 { font-size: clamp(2.25rem, 8vw, 4rem); }
.wrapped-cue { color: var(--color-muted); font-size: 1.5rem; margin-top: 2rem; }
.wrapped figure { margin: 1rem 0; }
.wrapped svg { max-width: 100%; height: auto; }
.wrapped video { width: 100%; height: auto; border-radius: var(--radius-card); background: #000; }
.wrapped-sharecard { text-align: center; align-items: center; }
.wrapped-finale { padding-top: 1rem; }
""".strip()


# The one script in the shell (#840): on any form submit, honor the
# form's (or the clicked button's) data-confirm with a confirm dialog,
# then disable the form's submit buttons so a slow action cannot be
# sent twice, labeling the clicked one from its data-busy. Forms whose
# clicked button carries the value (debug on/off, the walkthrough's
# steps) get a hidden mirror of that button first: a disabled button
# drops out of the submitted data, so without the mirror the value
# would be lost. Without JavaScript every page behaves exactly as
# before — this is pending-state polish only, never a gate.
#
# A page restored from the browser's back/forward cache keeps that
# pending state, so a pageshow handler for the persisted (bfcache)
# case undoes it: buttons this script disabled (marked
# data-ph-disabled) are re-enabled, the clicked button's original
# label (saved in data-ph-label before the busy label replaced it)
# is restored, and the injected submitter mirrors (marked
# data-ph-mirror) are removed so a stale action value cannot be
# submitted again on a later submit.
SUBMIT_SCRIPT = (
    "<script>document.addEventListener('submit',function(e){var f=e.target;"
    "var ask=f.dataset.confirm||(e.submitter&&e.submitter.dataset?"
    "e.submitter.dataset.confirm:null);"
    "if(ask&&!confirm(ask)){e.preventDefault();return;}"
    "if(e.submitter&&e.submitter.name){var h=document.createElement('input');"
    "h.type='hidden';h.name=e.submitter.name;h.value=e.submitter.value;"
    "h.setAttribute('data-ph-mirror','');f.appendChild(h);}"
    "f.querySelectorAll('button[type=submit]').forEach(function(b){"
    "b.setAttribute('data-ph-disabled','');b.disabled=true;"
    "if(b===e.submitter){if(b.dataset.phLabel===undefined)"
    "b.dataset.phLabel=b.textContent;"
    "b.textContent=b.dataset.busy||'Working…';}});});"
    "window.addEventListener('pageshow',function(e){if(!e.persisted)return;"
    "document.querySelectorAll('button[type=submit][data-ph-disabled]')"
    ".forEach(function(b){b.disabled=false;b.removeAttribute('data-ph-disabled');"
    "if(b.dataset.phLabel!==undefined){b.textContent=b.dataset.phLabel;"
    "b.removeAttribute('data-ph-label');}});"
    "document.querySelectorAll('input[data-ph-mirror]')"
    ".forEach(function(h){h.remove();});});"
    "</script>")

# The favicon and touch icon links, in every page head (#1456). The
# files are the branding icons in webui/static (byte copies of
# assets/branding, pinned equal by tests/test_webui_favicon.py), served
# by the public /favicon.ico and /static/* routes in routes.py, so the
# browser can fetch them before or without a login, the login page
# included.
ICON_HEAD_TAGS = (
    '<link rel="icon" type="image/x-icon" href="/favicon.ico">'
    '<link rel="icon" type="image/png" sizes="32x32" href="/static/icon-32.png">'
    '<link rel="icon" type="image/png" sizes="16x16" href="/static/icon-16.png">'
    '<link rel="apple-touch-icon" sizes="180x180" href="/static/icon-180.png">'
)

# PWA head tags (#450 Phase 1): the manifest link, the theme colors
# (the shell tokens: #12110f is the dark background, #f3efe6 the light
# one), and the capable metas. They ride on every page, bare ones
# included, so the login page is installable too. The icon and
# apple-touch-icon link tags are the favicon change's ICON_HEAD_TAGS
# above, deliberately not duplicated here.
PWA_HEAD_TAGS = (
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
    '<meta name="apple-mobile-web-app-title" content="Print Handy">'
    '<meta name="theme-color" content="#12110f" media="(prefers-color-scheme: dark)">'
    '<meta name="theme-color" content="#f3efe6" media="(prefers-color-scheme: light)">'
    '<link rel="manifest" href="/manifest.webmanifest">'
)

# The PWA script (#450 Phase 1): progressive enhancement only, like
# SUBMIT_SCRIPT — every page works exactly as before without it.
# Registration runs only in a secure context (HTTPS or loopback; a
# plain-HTTP LAN address never registers, and the page is unaffected),
# and persistent storage is requested so the precached offline shell
# is less likely to be evicted. The install affordance appears only
# when the browser offers one: Chromium fires beforeinstallprompt,
# which is captured so a discreet "Install app" button can be added
# to the header; iOS Safari has no prompt API, so on an iPhone or
# iPad that is not already running standalone the script adds one
# small, dismissible note naming Share, then Add to Home Screen.
PWA_SCRIPT = (
    "<script>"
    "if(window.isSecureContext&&'serviceWorker' in navigator){"
    "window.addEventListener('load',function(){"
    "navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(function(){});"
    "if(navigator.storage&&navigator.storage.persist){"
    "navigator.storage.persist().catch(function(){});}});}"
    "var phInstallEvent=null;"
    "window.addEventListener('beforeinstallprompt',function(e){"
    "e.preventDefault();phInstallEvent=e;"
    "var host=document.querySelector('.chrome-right');"
    "if(!host||document.getElementById('ph-install'))return;"
    "var b=document.createElement('button');b.type='button';"
    "b.id='ph-install';b.textContent='Install app';"
    "b.addEventListener('click',function(){if(!phInstallEvent)return;"
    "phInstallEvent.prompt();"
    "phInstallEvent.userChoice.then(function(){phInstallEvent=null;b.remove();});});"
    "host.appendChild(b);});"
    "window.addEventListener('DOMContentLoaded',function(){"
    "var ios=/iphone|ipad|ipod/i.test(navigator.userAgent);"
    "if(!ios||window.navigator.standalone)return;"
    "var main=document.querySelector('main .wrap');if(!main)return;"
    "var note=document.createElement('p');note.className='note';"
    "note.id='ph-ios-install';"
    "note.textContent='To install Print Handy on this device, tap Share, "
    "then Add to Home Screen. ';"
    "var close=document.createElement('button');close.type='button';"
    "close.textContent='Dismiss';"
    "close.addEventListener('click',function(){note.remove();});"
    "note.appendChild(close);main.insertBefore(note,main.firstChild);});"
    "</script>")

def _esc(value):
    return html.escape(str(value), quote=True)


def theme_from_cookies(cookie_header):
    """The theme a Cookie header asks for; the dark default for anything else."""
    raw = cookie_header or ""
    if not raw:
        return DEFAULT_THEME
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # a malformed Cookie header carries no theme
        return DEFAULT_THEME
    morsel = jar.get(THEME_COOKIE)
    if morsel is not None and morsel.value in THEMES:
        return morsel.value
    return DEFAULT_THEME


def theme_cookie_header(theme, secure=False):
    """The Set-Cookie value that remembers ``theme`` for a year."""
    flags = "; Secure" if secure else ""
    return f"{THEME_COOKIE}={theme}; SameSite=Lax; Path=/; Max-Age=31536000{flags}"


def _header(path, theme, nav=None):
    items = []
    for target, label in (NAV if nav is None else nav):
        active = path == target or (target != "/" and path.startswith(target + "/"))
        items.append(f'<a href="{target}"{" class=\"active\"" if active else ""}>'
                     f"{_esc(label)}</a>")
    toggle = "".join(
        f'<a href="/theme?set={name}&amp;back={quote(path or "/", safe="/")}"'
        f'{" class=\"active\"" if theme == name else ""}>{name.capitalize()}</a>'
        for name in THEMES)
    return ('<header class="site"><div class="wrap">'
            '<div class="brand"><span class="tile" aria-hidden="true">PH</span>'
            f'<div><div class="name">Print Handy</div>'
            f'<div class="sub">{_esc(__version__)}</div></div></div>'
            '<div class="chrome-right">'
            f'<nav class="pills" aria-label="Web UI pages">{"".join(items)}</nav>'
            f'<div class="theme-toggle" role="group" aria-label="Color theme">{toggle}</div>'
            "</div></div></header>")


def page(title, body, *, path="", theme=DEFAULT_THEME, chrome=True, heading=None,
         eyebrow="", banner="", nav=None):
    """One full HTML document in the shared shell.

    ``title`` names the page (``Print Handy — <title>`` in the tab, and
    the default heading ``Print Handy <title>``). ``heading=""`` omits
    the heading; any other value replaces the default. ``chrome=False``
    renders the bare document (the login and token-upload pages: no
    navigation to offer). ``banner`` is inserted at the top of the content,
    above the heading. ``nav`` overrides the header navigation for this
    page only (the Data/Wrapped pages pass the extended nav when that
    feature is on, #956); the default is :data:`NAV`, unchanged for
    every existing page. The source footer (#1158) is appended on every
    page, with or without chrome. Every interpolated value here is escaped by
    the callers for body content; title and heading are escaped here.
    """
    theme_attr = ' data-theme="light"' if theme == "light" else ""
    parts = ["<!DOCTYPE html>\n<html lang=\"en\"", theme_attr, "><head><meta charset=\"utf-8\">"
             '<meta name="viewport" content="width=device-width, initial-scale=1">'
             f"{ICON_HEAD_TAGS}"
             f"{PWA_HEAD_TAGS}"
             f"<title>Print Handy — {_esc(title)}</title>"
             f"<style>{CSS}</style></head><body>"]
    if chrome:
        parts.append(_header(path, theme, nav=nav))
    parts.append('<main><div class="wrap">')
    if banner:
        parts.append(banner)
    if eyebrow:
        parts.append(f'<p class="eyebrow">{_esc(eyebrow)}</p>')
    if heading is None:
        heading = f"Print Handy {title}"
    if heading:
        parts.append(f"<h1>{_esc(heading)}</h1>")
    parts.append(body)
    parts.append("</div></main>")
    # The AGPL expects network users to be able to get the source: one
    # small footer on every page, the bare ones included, naming the
    # running version and linking its source tree (#1158).
    parts.append('<footer class="site-footer"><div class="wrap">'
                 f'Print Handy {_esc(__version__)} is free software under the '
                 'AGPL-3.0-only license. '
                 f'<a href="{_esc(SOURCE_URL)}/tree/{_esc(__version__)}" rel="noopener">'
                 'Source for this version</a></div></footer>')
    parts.append(SUBMIT_SCRIPT)
    parts.append(PWA_SCRIPT)
    parts.append("</body></html>\n")
    return "".join(parts)


__all__ = [
    "CSS", "DEFAULT_THEME", "ICON_HEAD_TAGS", "NAV", "PWA_HEAD_TAGS",
    "PWA_SCRIPT", "SUBMIT_SCRIPT", "THEME_COOKIE", "THEMES",
    "page", "theme_cookie_header", "theme_from_cookies",
]
