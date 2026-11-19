"""The Web UI External intake page (#1525): watched-folder draft cards.

The page is the production caller for `core.external_intake`. The
[external_intake] setting gates everything: while it is off, or no
directory is configured, the page says so and no scan runs. A scan only
notices stable files in the one configured directory and records draft
cards (persisted under the state home beside the scan state); a card
never prints, never sends a printer command and never charges a spool.

Mark as printed is the operator's explicit declaration that a physical
print happened. It goes through `core.external_intake.confirm_mark_printed`
with a persisted ledger, so the same physical print (file hash +
printer + print id) is charged exactly once: a second confirmation is
refused, and a print id that names a print Print Handy's own monitored
accounting already charged or checked is refused as already recorded.
That accounting is the ledger in `runtime.multislot_usage`, for every
slot count: a multi-slot charge, a one-spool charge at the end, the
runaway check of a Moonraker live charge, and a one-spool print
Moonraker charged live. A print id matches by any identity the ledger
records: the job key with its print number or start time, the job key
alone, or Moonraker's history job id. A confirmation claims its print id
in that same ledger before it charges, so the live end of the print
then charges nothing more. When the operator declares spool usage on
the form, the charge uses the same Spoolman "use" call the multislot
charger makes; a charge whose outcome is unknown is recorded as
unconfirmed and is never retried from here.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path

from print_handy.core import external_intake as ei

from . import shell

_esc = shell._esc
logger = logging.getLogger(__name__)

# One confirmation at a time per process: the duplicate checks, the
# claim write and the spool charge are one atomic sequence, so two
# concurrent confirmations of the same card cannot both pass the
# checks and charge twice.
_CONFIRM_LOCK = threading.Lock()


def _store_dir(data) -> Path:
    from print_handy import settings
    return settings.state_dir(data) / "external-intake"


def _read_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def intake_config(data) -> dict:
    section = data.get("external_intake") or {}
    return {"enabled": bool(section.get("enabled")),
            "directory": str(section.get("directory") or ""),
            "max_bytes": int(section.get("max_bytes") or ei.DEFAULT_MAX_BYTES)}


def scan_cards(data) -> list[dict]:
    """All known draft cards: a fresh scan merged into the persisted set."""
    config = intake_config(data)
    store = _store_dir(data)
    cards = _read_json(store / "cards.json", [])
    if not config["enabled"] or not config["directory"]:
        return cards
    state = _read_json(store / "scan-state.json", {"seen": {}, "hashes": {}})
    new_cards, state = ei.scan(config["directory"], enabled=True,
                               max_bytes=config["max_bytes"], state=state)
    _write_json(store / "scan-state.json", state)
    known = {(card.get("sha256"), card.get("path")) for card in cards}
    for card in new_cards:
        if (card.get("sha256"), card.get("path")) not in known:
            cards.append(card)
    if new_cards:
        _write_json(store / "cards.json", cards)
    return cards


def _unreadable_ledger(exc) -> ei.IntakeRefused:
    logger.warning("external intake: the print accounting ledger is "
                   "unreadable; refusing the confirmation", exc_info=True)
    return ei.IntakeRefused(
        "the print accounting ledger could not be read, so Print Handy cannot "
        "check whether this print was already charged; nothing was recorded "
        f"or charged. Fix or restore the ledger and try again ({exc}).")


def _multislot_ledger_keys() -> set:
    """Every print id Print Handy's monitored accounting already charged or checked.

    Read from every accounting ledger on this install (the default one
    and each roster printer's), as `multislot_usage.accounted_print_ids`
    names them. Fails closed (#1621 review): a ledger that exists but
    cannot be read or parsed raises `ei.IntakeRefused` instead of
    returning an empty set, because an empty set would let a print
    already charged there be confirmed and charged again. Only a ledger
    that does not exist yet (a fresh install) counts as empty.
    """
    from print_handy.runtime import multislot_usage
    keys = set()
    for path in multislot_usage.all_ledger_paths():
        try:
            keys |= multislot_usage.accounted_print_ids(multislot_usage._load_strict(Path(path)))
        except (OSError, ValueError) as exc:
            raise _unreadable_ledger(exc) from exc
    return keys


def _claim_in_accounting_ledger(*, print_id, printer_id, identity) -> None:
    """Claim the print in the shared accounting ledger, or refuse (#1525).

    Rechecks under the ledger's own lock, so a print the live accounting
    recorded since the first check is still refused, and records the claim
    the printer's listener reads before it charges a finished print.
    """
    from print_handy.runtime import multislot_usage
    try:
        multislot_usage.claim_external_print(print_id, printer_id=printer_id, identity=identity)
    except multislot_usage.AlreadyAccounted as exc:
        raise ei.IntakeRefused(str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise _unreadable_ledger(exc) from exc


def _settle_claim(identity, status, *, printer_id) -> None:
    from print_handy.runtime import multislot_usage
    try:
        multislot_usage.settle_external_print(identity, status, printer_id=printer_id)
    except (OSError, ValueError):
        logger.warning("external intake: could not record the outcome %r of claim %s "
                       "in the print accounting ledger; the claim itself stands",
                       status, identity, exc_info=True)


def _validated_charge(spool_id, grams):
    """The declared spool usage, validated before it can reach Spoolman.

    Form values are untrusted: a spool id must be a positive integer
    and grams a positive, finite number, and the two are declared
    together or not at all. Anything else is refused here, before any
    accounting call, so a crafted confirmation cannot corrupt spool
    accounting with a negative or non-finite charge.
    """
    if spool_id is None and grams is None:
        return None
    if spool_id is None or grams is None:
        raise ei.IntakeRefused(
            "give both a spool id and its grams, or neither — a print with "
            "no declared spool usage is recorded without a spool charge.")
    if isinstance(spool_id, bool):
        raise ei.IntakeRefused("the spool id must be a positive whole number.")
    try:
        spool = int(spool_id)
    except (TypeError, ValueError):
        raise ei.IntakeRefused("the spool id must be a positive whole number.") from None
    if isinstance(spool_id, float) and not spool_id.is_integer():
        raise ei.IntakeRefused("the spool id must be a positive whole number.")
    if isinstance(spool_id, str) and str(spool) != spool_id.strip():
        raise ei.IntakeRefused("the spool id must be a positive whole number.")
    if spool < 1:
        raise ei.IntakeRefused("the spool id must be a positive whole number.")
    try:
        weight = float(grams)
    except (TypeError, ValueError):
        raise ei.IntakeRefused("the grams used must be a positive number.") from None
    if not math.isfinite(weight) or weight <= 0:
        raise ei.IntakeRefused("the grams used must be a positive, finite number.")
    return spool, weight


def confirm_card(data, *, sha256, path, printer_id, print_id, spool_id=None,
                 grams=None) -> dict:
    """Confirm one draft card as printed, charging exactly once.

    Raises `ei.IntakeRefused` with the concrete reason when the print is
    already recorded, the card is unknown, the declared spool usage is
    not a valid positive charge, or a declared spool charge could not
    be confirmed. The whole sequence runs under a lock, and the print's
    identity is claimed in the persisted ledger before the spool charge
    is sent, so neither a concurrent confirmation nor a crash mid-charge
    can charge the same print twice.
    """
    declared = _validated_charge(spool_id, grams)
    with _CONFIRM_LOCK:
        return _confirm_card_locked(data, sha256=sha256, path=path,
                                    printer_id=printer_id, print_id=print_id,
                                    declared=declared)


def _confirm_card_locked(data, *, sha256, path, printer_id, print_id,
                         declared) -> dict:
    store = _store_dir(data)
    cards = _read_json(store / "cards.json", [])
    card = next((row for row in cards
                 if row.get("sha256") == sha256 and row.get("path") == path), None)
    if card is None:
        raise ei.IntakeRefused("no draft card matches that file; refresh the page.")
    if str(print_id).strip() in _multislot_ledger_keys():
        raise ei.IntakeRefused(
            f"print {str(print_id).strip()} is already recorded in the print accounting "
            "ledger: Print Handy's own print monitoring already charged or checked its "
            "spool usage when it finished. Refusing to charge the same physical print "
            "twice; nothing was recorded or charged.")
    ledger = set(_read_json(store / "ledger.json", []))
    unconfirmed = set(_read_json(store / "unconfirmed.json", []))
    identity = ei.print_identity(card, printer_id, print_id)
    if identity in unconfirmed:
        raise ei.IntakeRefused(
            "a spool charge for this print was sent but never confirmed; it is "
            "recorded as unconfirmed and is not retried here.")

    def charge(*, identity, card, printer_id):
        # Claim first: the identity is persisted in the shared accounting
        # ledger (refused there when the live accounting already has the
        # print) and in this page's ledger before any charge is sent, so a
        # crash from here on can never lead to the same print being charged
        # again by a later confirmation or by the live end of the print.
        _claim_in_accounting_ledger(print_id=print_id, printer_id=printer_id,
                                    identity=identity)
        ledger.add(identity)
        _write_json(store / "ledger.json", sorted(ledger))
        if declared is None:
            _settle_claim(identity, "recorded", printer_id=printer_id)
            return {"recorded": True, "spool_charge": "none declared — the print "
                    "is recorded; no spool was charged"}
        spool, weight = declared
        from print_handy.core import spoolman_client
        try:
            spoolman_client.request(
                "PUT", f"/api/v1/spool/{spool}/use",
                {"use_weight": weight}, timeout=8)
        except Exception as exc:
            unconfirmed.add(identity)
            _write_json(store / "unconfirmed.json", sorted(unconfirmed))
            _settle_claim(identity, "unconfirmed", printer_id=printer_id)
            raise ei.IntakeRefused(
                f"the spool charge for spool {spool} could not be confirmed "
                f"({exc}); it is recorded as unconfirmed and will not be retried "
                "from here.") from exc
        _settle_claim(identity, "charged", printer_id=printer_id)
        return {"recorded": True, "spool_charge": f"spool {spool}: {weight:g} g"}

    result = ei.confirm_mark_printed(card, printer_id=printer_id,
                                     print_id=print_id, ledger=ledger, charge=charge)
    _write_json(store / "ledger.json", sorted(ledger))
    card["status"] = "printed"
    card["print_id"] = str(print_id)
    card["printer_id"] = str(printer_id)
    _write_json(store / "cards.json", cards)
    return result


def render_page(data, cards, *, theme=shell.DEFAULT_THEME, nav=None,
                form_token="", notice=None, error=None) -> str:
    config = intake_config(data)
    parts = []
    if notice:
        parts.append(f"<p><strong>{_esc(notice)}</strong></p>")
    if error:
        parts.append(f"<p><strong>{_esc(error)}</strong></p>")
    if not config["enabled"]:
        parts.append("<p>External intake is off. Set <code>[external_intake] "
                     "enabled</code> and a <code>directory</code> in the settings "
                     "file to watch one folder for jobs sliced outside Print "
                     "Handy. While it is off, nothing is scanned.</p>")
        return shell.page("external-intake", "".join(parts), path="/external-intake",
                          theme=theme, heading="External intake", nav=nav)
    if not config["directory"]:
        parts.append("<p>External intake is on but no directory is configured, "
                     "so nothing is scanned. Set <code>[external_intake] "
                     "directory</code> to the one folder to watch.</p>")
        return shell.page("external-intake", "".join(parts), path="/external-intake",
                          theme=theme, heading="External intake", nav=nav)
    parts.append(f"<p>Draft cards for sliced files found in "
                 f"<code>{_esc(config['directory'])}</code>. A card is a draft: "
                 "nothing was printed, sent or charged. Confirm a physical "
                 "print with Mark as printed — each print is recorded exactly "
                 "once.</p>")
    if not cards:
        parts.append("<p>No draft cards yet. A file becomes a card once it is "
                     "stable across two visits to this page.</p>")
    for card in cards:
        parts.append(
            "<table>"
            f"<tr><th>File</th><td>{_esc(Path(str(card.get('path') or '')).name)}</td></tr>"
            f"<tr><th>Source</th><td>{_esc(card.get('source_kind') or 'unknown')} "
            f"({_esc(card.get('source_class') or 'unknown')})</td></tr>"
            f"<tr><th>SHA-256</th><td>{_esc(card.get('sha256'))}</td></tr>"
            f"<tr><th>Status</th><td>{_esc(card.get('status'))}</td></tr>"
            "</table>")
        if card.get("status") != "printed":
            parts.append(
                "<form method=\"post\" action=\"/external-intake/confirm\">"
                f"<input type=\"hidden\" name=\"_csrf\" value=\"{_esc(form_token)}\">"
                f"<input type=\"hidden\" name=\"sha256\" value=\"{_esc(card.get('sha256'))}\">"
                f"<input type=\"hidden\" name=\"path\" value=\"{_esc(card.get('path'))}\">"
                "<label>Printer id <input name=\"printer_id\" required></label> "
                "<label>Print id <input name=\"print_id\" required></label> "
                "<small>When Print Handy monitored this print, use its Moonraker job id "
                "or its file and print number (file#number): a print it already "
                "charged is refused.</small> "
                "<label>Spool id (optional) <input name=\"spool_id\" inputmode=\"numeric\"></label> "
                "<label>Grams used (optional) <input name=\"grams\" inputmode=\"decimal\"></label> "
                "<button type=\"submit\">Mark as printed</button></form>")
    return shell.page("external-intake", "".join(parts), path="/external-intake",
                      theme=theme, heading="External intake", nav=nav)
