"""The header theme toggle: sun/moon labels in the top-right corner.

The toggle stays two plain links to GET /theme (no script); this pins
the icon labels, the active marking, and the corner placement rule, so
a shell edit cannot quietly drop the toggle back into the nav flow.
"""
from print_handy.webui import shell


def test_toggle_links_carry_sun_and_moon_icons():
    out = shell.page("Status", "<p>body</p>", path="/")
    assert '<span aria-hidden="true">☾</span>Dark</a>' in out
    assert '<span aria-hidden="true">☀</span>Light</a>' in out
    assert 'href="/theme?set=dark&amp;back=/"' in out
    assert 'href="/theme?set=light&amp;back=/"' in out


def test_toggle_marks_the_current_theme_active():
    dark = shell.page("Status", "<p>body</p>", path="/", theme="dark")
    assert 'class="active"><span aria-hidden="true">☾</span>Dark</a>' in dark
    light = shell.page("Status", "<p>body</p>", path="/", theme="light")
    assert 'class="active"><span aria-hidden="true">☀</span>Light</a>' in light


def test_toggle_sits_in_the_headers_top_right_corner():
    out = shell.page("Status", "<p>body</p>", path="/")
    # The toggle is a direct child of the header wrap, outside the
    # nav's .chrome-right flow, and the stylesheet pins it top-right.
    brand_end = out.index('<div class="theme-toggle"')
    nav_start = out.index('<div class="chrome-right">')
    assert brand_end < nav_start
    assert ".theme-toggle {\n  position: absolute; top: 0.75rem; right: 1rem;" in out
