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
refused, and a print id that already appears in the multislot
accounting ledger (`runtime.multislot_usage`) is refused as already
recorded. When the operator declares spool usage on the form, the
charge uses the same Spoolman "use" call the multislot charger makes;
a charge whose outcome is unknown is recorded as unconfirmed and is
never retried from here.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from print_handy.core import external_intake as ei

from . import shell

_esc = shell._esc
logger = logging.getLogger(__name__)


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


def _multislot_ledger_keys() -> set:
    try:
        from print_handy.runtime import multislot_usage
        ledger = _read_json(multislot_usage.ledger_path(), {})
    except Exception:
        logger.warning("external intake: the print accounting ledger is "
                       "unreadable; the already-recorded check cannot run",
                       exc_info=True)
        return set()
    keys = set()
    for key in ledger:
        keys.add(str(key))
        keys.add(str(key).split("#")[0])
    return keys


def confirm_card(data, *, sha256, path, printer_id, print_id, spool_id=None,
                 grams=None) -> dict:
    """Confirm one draft card as printed, charging exactly once.

    Raises `ei.IntakeRefused` with the concrete reason when the print is
    already recorded, the card is unknown, or a declared spool charge
    could not be confirmed.
    """
    store = _store_dir(data)
    cards = _read_json(store / "cards.json", [])
    card = next((row for row in cards
                 if row.get("sha256") == sha256 and row.get("path") == path), None)
    if card is None:
        raise ei.IntakeRefused("no draft card matches that file; refresh the page.")
    if str(print_id) in _multislot_ledger_keys():
        raise ei.IntakeRefused(
            "that print id is already recorded in the print accounting ledger; "
            "refusing to charge the same physical print twice.")
    ledger = set(_read_json(store / "ledger.json", []))
    unconfirmed = set(_read_json(store / "unconfirmed.json", []))
    identity = ei.print_identity(card, printer_id, print_id)
    if identity in unconfirmed:
        raise ei.IntakeRefused(
            "a spool charge for this print was sent but never confirmed; it is "
            "recorded as unconfirmed and is not retried here.")

    def charge(*, identity, card, printer_id):
        if spool_id is None or grams is None:
            return {"recorded": True, "spool_charge": "none declared — the print "
                    "is recorded; no spool was charged"}
        from print_handy.core import spoolman_client
        try:
            spoolman_client.request(
                "PUT", f"/api/v1/spool/{int(spool_id)}/use",
                {"use_weight": float(grams)}, timeout=8)
        except Exception as exc:
            unconfirmed.add(identity)
            _write_json(store / "unconfirmed.json", sorted(unconfirmed))
            raise ei.IntakeRefused(
                f"the spool charge for spool {spool_id} could not be confirmed "
                f"({exc}); it is recorded as unconfirmed and will not be retried "
                "from here.") from exc
        return {"recorded": True, "spool_charge": f"spool {spool_id}: {float(grams):g} g"}

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
                "<label>Spool id (optional) <input name=\"spool_id\" inputmode=\"numeric\"></label> "
                "<label>Grams used (optional) <input name=\"grams\" inputmode=\"decimal\"></label> "
                "<button type=\"submit\">Mark as printed</button></form>")
    return shell.page("external-intake", "".join(parts), path="/external-intake",
                      theme=theme, heading="External intake", nav=nav)
