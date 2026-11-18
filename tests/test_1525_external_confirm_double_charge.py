"""An External intake confirmation and the monitored accounting never charge one print twice (#1525).

The External intake page's Mark as printed confirmation charges a spool
for a print the operator declares. Print Handy's own monitoring charges
(or checks the printer's live charge of) every print it watched, through
the ledger in runtime.multislot_usage. On the base commit the
confirmation only compared its print id with that ledger's keys, and a
one-spool print on a Moonraker-linked printer with no launch baseline
left no entry at all, so confirming it charged its spool a second time;
nothing stored Moonraker's job id either. The live end in turn never
looked at confirmations, so a print confirmed first was charged again
when it finished.

Each test drives the production entry points: the listener's end
transition (Listener.on_status) or the live charge function
(multislot_usage.charge_finished_print), and the Web UI confirmation
(the POST route, or confirm_card behind it), against a stateful fake
Spoolman. Nothing contacts a printer.
"""
import re
import threading
from urllib.parse import urlencode

import pytest

from print_handy.core import external_intake as ei, spoolman_client
from print_handy.runtime import multislot_usage
from print_handy.webui import external_intake_page

from tests.runtime.test_spool_usage_runaway_1374 import (
    FILE, PRINT_MM, SPOOL, BASELINE_G, FakeSpoolman, _context, _drive,
)
from tests.test_1525_completion_webui import FORM, TOKEN, authed, call, intake_settings, start

JOB_ID = "000113"          # the Moonraker history job id _drive's fake serves
PRINT_KEY = f"{FILE}#113"  # the job key and print number the ledger keys the print by


def _weight_charges(fake):
    """Charges by weight: Print Handy's end charge or a confirmation. Moonraker's reports carry a length."""
    return [c for c in fake.calls
            if c[0] == "PUT" and c[1] == f"/api/v1/spool/{SPOOL}/use" and "use_weight" in (c[2] or {})]


def _card(tmp_path):
    data, _watched = intake_settings(tmp_path / "intake")
    external_intake_page.scan_cards(data)
    (card,) = external_intake_page.scan_cards(data)
    return data, card


def _live_charged_context(with_baseline):
    """A one-spool print on a linked printer: Moonraker charges it live, Print Handy charges nothing."""
    ctx = _context()
    if not with_baseline:
        ctx.pop("spool_usage_baseline")  # Spoolman did not answer at launch
    return ctx


@pytest.fixture
def intake_dir(tmp_path):
    (tmp_path / "intake").mkdir()
    return tmp_path


@pytest.mark.parametrize("with_baseline,print_id", [
    (False, PRINT_KEY), (False, JOB_ID), (True, JOB_ID),
])
def test_a_print_the_printer_charged_live_is_refused_by_the_confirm_route(
        intake_dir, monkeypatch, with_baseline, print_id):
    tmp_path = intake_dir
    fake = _drive(monkeypatch, tmp_path, [PRINT_MM], "complete", filament_used=PRINT_MM,
                  context=_live_charged_context(with_baseline), active=SPOOL)
    used_after_print = fake.used
    data, _card_row = _card(tmp_path)
    httpd, port = start(data, environ={"WEBUI_ACCESS_TOKEN": TOKEN})
    try:
        headers = authed(httpd)
        code, _h, body = call(port, "GET", "/external-intake", headers=headers)
        text = body.decode()
        form = urlencode({"_csrf": re.search(r'name="_csrf" value="([^"]+)"', text).group(1),
                          "sha256": re.search(r'name="sha256" value="([^"]+)"', text).group(1),
                          "path": re.search(r'name="path" value="([^"]+)"', text).group(1),
                          "printer_id": "ad5x", "print_id": print_id,
                          "spool_id": str(SPOOL), "grams": "0.53"})
        code, _h, body = call(port, "POST", "/external-intake/confirm",
                              body=form, headers={**headers, **FORM})
    finally:
        httpd.shutdown()
        httpd.server_close()
    page = body.decode()
    assert code == 200
    assert "already recorded in the print accounting ledger" in page
    assert "Recorded as printed" not in page
    assert _weight_charges(fake) == []
    assert fake.used == used_after_print


def test_a_live_charged_print_with_no_baseline_is_recorded_without_a_charge(intake_dir, monkeypatch):
    fake = _drive(monkeypatch, intake_dir, [PRINT_MM], "complete", filament_used=PRINT_MM,
                  context=_live_charged_context(False), active=SPOOL)
    entry = multislot_usage.entry_for(_live_charged_context(False))
    assert entry["status"] == "printer_charged"
    assert entry["moonraker_job_id"] == JOB_ID
    assert PRINT_KEY in entry["print_ids"]
    assert _weight_charges(fake) == [] and fake.patches() == []


def _charge_at_end_context():
    """A one-spool print on a printer with no Moonraker Spoolman link: Print Handy charges it at the end (#1570)."""
    ctx = _context()
    ctx.pop("spool_usage_baseline")
    ctx["spool_charge_at_end"] = {"spool_id": SPOOL}
    ctx["spool_accounting_authority"] = "print_handy"
    return ctx


@pytest.mark.parametrize("print_id", [PRINT_KEY, JOB_ID, FILE])
def test_a_confirmed_print_is_not_charged_again_when_it_finishes(intake_dir, monkeypatch, print_id):
    tmp_path = intake_dir
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(tmp_path / "ledger.json"))
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    data, card = _card(tmp_path)
    result = external_intake_page.confirm_card(
        data, sha256=card["sha256"], path=card["path"], printer_id="ad5x",
        print_id=print_id, spool_id=SPOOL, grams=0.53)
    assert result["accounting"]["spool_charge"] == f"spool {SPOOL}: 0.53 g"
    assert len(_weight_charges(fake)) == 1
    _drive(monkeypatch, tmp_path, [], "complete", filament_used=PRINT_MM,
           context=_charge_at_end_context(), fake=fake, active=None)
    assert len(_weight_charges(fake)) == 1, "the live end charged a print already confirmed"
    entry = multislot_usage.entry_for(_charge_at_end_context())
    assert entry["reason"] == multislot_usage.EXTERNAL_REASON


def test_an_unconfirmed_print_is_still_charged_at_the_end(intake_dir, monkeypatch):
    # The control: with no confirmation the same print is charged once at the end.
    fake = _drive(monkeypatch, intake_dir, [], "complete", filament_used=PRINT_MM,
                  context=_charge_at_end_context(), active=None)
    assert len(_weight_charges(fake)) == 1


def test_a_live_end_arriving_while_a_confirmation_charges_charges_once(intake_dir, monkeypatch):
    # The print finishes while the confirmation's Spoolman charge is in
    # flight: the live end runs in its own thread, concurrently, and must
    # see the confirmation's claim rather than charge the print again.
    tmp_path = intake_dir
    ledger = tmp_path / "ledger.json"
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(ledger))
    fake = FakeSpoolman(BASELINE_G)
    live = []

    def finish():
        live.append(multislot_usage.charge_finished_print(
            _charge_at_end_context(), "complete", request=fake, path=ledger))

    def in_flight(*a, **k):
        result = fake(*a, **k)
        if not live and a[0] == "PUT":
            thread = threading.Thread(target=finish)
            thread.start()
            thread.join()
        return result

    monkeypatch.setattr(spoolman_client, "request", in_flight)
    data, card = _card(tmp_path)
    external_intake_page.confirm_card(
        data, sha256=card["sha256"], path=card["path"], printer_id="ad5x",
        print_id=PRINT_KEY, spool_id=SPOOL, grams=0.53)
    assert live and live[0]["reason"] == multislot_usage.EXTERNAL_REASON
    assert len(_weight_charges(fake)) == 1


def test_a_confirmation_arriving_while_the_live_end_charges_is_refused(intake_dir, monkeypatch):
    tmp_path = intake_dir
    ledger = tmp_path / "ledger.json"
    monkeypatch.setenv("PRINT_HANDY_MULTISLOT_USAGE_STATE", str(ledger))
    fake = FakeSpoolman(BASELINE_G)
    monkeypatch.setattr(spoolman_client, "request", fake)
    data, card = _card(tmp_path)
    outcomes = []

    def confirm():
        try:
            external_intake_page.confirm_card(
                data, sha256=card["sha256"], path=card["path"], printer_id="ad5x",
                print_id=JOB_ID, spool_id=SPOOL, grams=0.53)
            outcomes.append("confirmed")
        except ei.IntakeRefused as exc:
            outcomes.append(str(exc))

    threads = []

    def in_flight(*a, **k):
        # The live charge holds the ledger lock through its Spoolman call,
        # so the confirmation runs beside it and is joined afterwards.
        if not threads:
            threads.append(threading.Thread(target=confirm))
            threads[0].start()
        return fake(*a, **k)

    multislot_usage.charge_finished_print(_charge_at_end_context(), "complete", request=in_flight,
                                          path=ledger, job_id=JOB_ID)
    threads[0].join(timeout=30)
    assert len(outcomes) == 1 and "already recorded" in outcomes[0]
    assert len(_weight_charges(fake)) == 1
