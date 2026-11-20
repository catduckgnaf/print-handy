"""The header uses light-only styling and quick links, not a theme picker."""
from print_handy.webui import shell


def test_shell_ignores_legacy_dark_preference():
    assert shell.DEFAULT_THEME == "light"
    assert shell.theme_from_cookies("ph-theme=dark") == "light"
    assert shell.theme_from_cookies("ph-theme=light") == "light"
    assert shell.theme_from_cookies("ph-theme=bogus") == "light"
    out = shell.page("Status", "<p>body</p>", path="/", theme="dark")
    assert '<html lang="en" data-theme="light">' in out
    assert "data-theme=\"dark\"" not in out


def test_settings_and_notifications_are_quick_links_only():
    out = shell.page("Settings", "<p>body</p>", path="/config")
    assert '<nav class="header-quicklinks" aria-label="Quick links">' in out
    assert out.count('href="/config"') == 1
    assert out.count('href="/notifications"') == 1
    assert 'class="active">Settings</a>' in out
    assert "theme-toggle" not in out
    assert "THEMES" not in shell.CSS


def test_phone_header_rules_keep_quicklinks_and_tabs_in_flow():
    assert "@media (max-width: 40rem)" in shell.CSS
    assert ".header-quicklinks { margin-left: auto; }" in shell.CSS
    assert "nav.pills { width: 100%; }" in shell.CSS
    assert "position: absolute; top: 0.75rem; right: 1rem;" not in shell.CSS
