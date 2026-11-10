"""Every Web UI page offers the source for the running version (#1158).

Print Handy is AGPL-3.0-only; section 13 expects a user interacting
over a network to be offered the corresponding source. The one page
shell (:mod:`print_handy.webui.shell`) renders every page, so the
footer lives there and these tests pin it on the shell itself, on a
bare (chrome=False) page such as the login page, and on the version
the link names.
"""
from print_handy import __version__
from print_handy.webui import shell


def test_footer_links_the_source_tree_at_the_running_version():
    out = shell.page("Status", "<p>body</p>", path="/")
    assert f'href="{shell.SOURCE_URL}/tree/{__version__}"' in out
    assert "AGPL-3.0-only" in out
    assert __version__ in out


def test_footer_is_present_without_chrome_too():
    # The login and token-upload pages render with chrome=False; a
    # network user reaches those first, so the offer applies there too.
    out = shell.page("log in", "<p>body</p>", path="/login", chrome=False)
    assert f'href="{shell.SOURCE_URL}/tree/{__version__}"' in out


def test_footer_comes_after_the_page_body():
    out = shell.page("Status", "<p>marker-body</p>", path="/")
    assert out.index("marker-body") < out.index('<footer class="site-footer">')
