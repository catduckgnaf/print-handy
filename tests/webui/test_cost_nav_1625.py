"""#1625 review: Cost replaces the Printer settings tab in the top navigation.

Printer settings still exists; it opens from each printer's card on the
Printers page instead of having a top-level tab.
"""
from print_handy.webui import printers_page, shell


def test_cost_takes_the_printer_settings_slot_in_the_nav():
    targets = [target for target, _label in shell.NAV]
    assert "/printer-settings" not in targets
    assert "Printer settings" not in {label for _t, label in shell.NAV}
    # Cost is a header tab of its own, not a page in another tab's strip.
    top = shell.page("x", "", path="/").split('aria-label="Web UI pages"')[1].split("</nav>")[0]
    assert 'href="/cost"' in top


def test_each_printer_card_opens_its_printer_settings():
    roster = [{"id": "ad5x", "name": "Garage AD5X", "model": "flashforge.ad5x", "default": True},
              {"id": "mini", "name": "Desk Mini", "model": "bambulab.a1_mini"}]
    html = printers_page.render(roster, form_token="t")
    assert 'href="/printer-settings?printer=ad5x">Printer settings</a>' in html
    assert 'href="/printer-settings?printer=mini">Printer settings</a>' in html
