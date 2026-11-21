"""The Web UI cost calculator page (#1625).

One form, two modes: simple mode asks for the material amount and the
print time plus profile pickers; advanced mode discloses every input
the engine reads. Dropping a G-code or sliced 3MF file on the page
posts it to the same route, which auto-fills amount and time from the
file through core.cost_parse. The numbers come from core.cost, the
same engine chat and the completion record use, so this page can never
disagree with them.
"""
from __future__ import annotations

import html

from ..core import cost as cost_module
from . import shell

_esc = shell._esc

FIELDS = (
    ("technology", "Technology", "select"),
    ("amount", "Material amount (grams for FDM, milliliters for resin)", "number"),
    ("hours", "Print time (hours)", "number"),
    ("parts", "Parts per build", "number"),
    ("price_per_kg", "Material price per kilogram", "number"),
    ("printer_price", "Printer purchase price", "number"),
    ("watts", "Average watts", "number"),
    ("prep_minutes", "Prep minutes per build", "number"),
    ("post_minutes", "Post minutes per part", "number"),
    ("pack_minutes", "Pack minutes per build", "number"),
    ("hardware", "Hardware cost", "number"),
    ("packaging", "Packaging cost", "number"),
    ("failure_percent", "Failure rate percent", "number"),
)

ADVANCED = {"printer_price", "watts", "prep_minutes", "post_minutes", "pack_minutes",
            "hardware", "packaging", "failure_percent"}


def _value(form, key, default=""):
    if form and form.get(key):
        return str(form[key][0])
    return str(default)


def _fmt(value):
    return f"{float(value):g}"


def defaults_for(data, technology="fdm"):
    """The form's starting values: every default filled in (#1718).

    The material price, watts, printer price and failure rate used to
    start blank — the engine's fallback applied invisibly. They now
    start at the exact values the engine would fall back to (the Print
    cost settings and the printer's cost profile), so what is shown is
    what is charged, and each field's note on the page names its
    default.
    """
    workspace = cost_module.WorkspaceCost.from_settings(data)
    profile, _ = cost_module.printer_profile_for(data)
    failure_rate = (workspace.failure_rate_resin if technology == "resin"
                    else workspace.failure_rate_fdm)
    failure_percent = _fmt(failure_rate * 100)
    return {
        "technology": technology, "amount": "", "hours": "", "parts": "1",
        "price_per_kg": _fmt(workspace.default_filament_cost_per_kg),
        "printer_price": _fmt(profile.purchase_price) if profile.purchase_price else "",
        "watts": _fmt(profile.watts),
        "prep_minutes": "5", "post_minutes": "10", "pack_minutes": "0",
        "hardware": "0", "packaging": "0",
        "failure_percent": failure_percent,
        "_workspace": workspace,
        "_profile": profile,
    }


def default_notes(data):
    """One "Default: …" note per field that has a default (#1718).

    Shown under the field on the calculator page, so a default is never
    a blank the operator has to guess at.
    """
    workspace = cost_module.WorkspaceCost.from_settings(data)
    profile, _ = cost_module.printer_profile_for(data)
    notes = {
        "price_per_kg": (f"Default: {_fmt(workspace.default_filament_cost_per_kg)} per "
                         "kilogram, from the Print COST settings (default filament cost)."),
        "watts": (f"Default: {_fmt(profile.watts)} W average "
                  f"({profile.watts_basis}), from the {profile.name} cost profile."),
        "failure_percent": (f"Default: {_fmt(workspace.failure_rate_fdm * 100)}% for FDM, "
                            f"{_fmt(workspace.failure_rate_resin * 100)}% for resin, "
                            "from the Print COST settings."),
        "prep_minutes": "Default: 5 minutes.",
        "post_minutes": "Default: 10 minutes.",
        "pack_minutes": "Default: 0 minutes.",
        "hardware": "Default: 0.",
        "packaging": "Default: 0.",
        "parts": "Default: 1.",
    }
    if profile.purchase_price:
        notes["printer_price"] = (f"Default: {_fmt(profile.purchase_price)}, from the "
                                  f"{profile.name} cost profile.")
    else:
        notes["printer_price"] = (f"No printer price in the {profile.name} cost profile "
                                  "yet: 0 until one is set in the Print COST settings.")
    return notes


def job_from_form(form, data):
    """(JobCostInput, PrinterCostProfile, WorkspaceCost) from posted fields."""
    workspace = cost_module.WorkspaceCost.from_settings(data)
    technology = "resin" if _value(form, "technology") == "resin" else "fdm"
    profile, _ = cost_module.printer_profile_for(data)
    profile.technology = technology
    if technology == "resin" and profile.lifetime_hours == 5000.0:
        profile.lifetime_hours = 4000.0
        profile.watts = 60.0
    printer_price = _value(form, "printer_price")
    if printer_price:
        profile.purchase_price = float(printer_price)
    watts = _value(form, "watts")
    if watts:
        profile.watts = float(watts)
        profile.watts_basis = "manual entry"
    amount = float(_value(form, "amount", "0") or 0)
    line = cost_module.MaterialLine(
        name="Resin" if technology == "resin" else "Filament",
        grams=amount if technology == "fdm" else 0.0,
        volume_ml=amount if technology == "resin" else 0.0)
    price = _value(form, "price_per_kg")
    if price:
        line.manual_price_per_kg = float(price)
    failure = _value(form, "failure_percent")
    job = cost_module.JobCostInput(
        lines=[line],
        print_hours=float(_value(form, "hours", "0") or 0),
        parts_per_build=int(float(_value(form, "parts", "1") or 1)),
        prep_minutes=float(_value(form, "prep_minutes", "5") or 0),
        post_minutes_per_part=float(_value(form, "post_minutes", "10") or 0),
        pack_minutes=float(_value(form, "pack_minutes", "0") or 0),
        hardware_cost=float(_value(form, "hardware", "0") or 0),
        packaging_cost=float(_value(form, "packaging", "0") or 0),
        failure_rate=(float(failure) / 100.0) if failure else None,
        grams_source="manual entry (Web UI calculator)")
    return job, profile, workspace


def job_from_usage(usage, data):
    """(JobCostInput, PrinterCostProfile, WorkspaceCost) for one parsed file.

    The calculator's auto-fill as a job: the file's material amount and
    print time on the form's default shape, with the amounts' provenance
    set to the parser's source. Shared by the cost page's dropped-file
    POST and the chat attachment answer (#1661), so a file dropped in
    chat prices exactly as the same file dropped on this page.
    """
    amount = usage.volume_ml if usage.volume_ml is not None else (usage.total_grams or 0)
    filled = {"technology": ["resin" if usage.volume_ml is not None else "fdm"],
              "amount": [str(amount)], "hours": [str(round(usage.hours, 3))],
              "parts": ["1"], "prep_minutes": ["5"], "post_minutes": ["10"],
              "pack_minutes": ["0"], "hardware": ["0"], "packaging": ["0"]}
    job, profile, workspace = job_from_form(filled, data)
    job.grams_source = usage.source
    if usage.slicer_price_per_kg and job.lines:
        job.lines[0].slicer_price_per_kg = usage.slicer_price_per_kg
    return job, profile, workspace


def render_cost_page(data, *, theme=shell.DEFAULT_THEME, form=None, result=None,
                     notice="", error="", form_token="", history=""):
    values = defaults_for(data, _value(form, "technology", "fdm") if form else "fdm")
    if form:
        for key, _label, _kind in FIELDS:
            values[key] = _value(form, key, values.get(key, ""))
    notes = default_notes(data)
    rows = []
    for key, label, kind in FIELDS:
        advanced = " data-advanced" if key in ADVANCED else ""
        if kind == "select":
            control = (f'<select name="{key}">'
                       f'<option value="fdm"{" selected" if values[key] != "resin" else ""}>Filament (FDM)</option>'
                       f'<option value="resin"{" selected" if values[key] == "resin" else ""}>Resin (MSLA)</option>'
                       "</select>")
        else:
            control = (f'<input name="{key}" inputmode="decimal" '
                       f'value="{_esc(values[key])}">')
        note = notes.get(key)
        hint = f'<br><span class="help">{_esc(note)}</span>' if note else ""
        rows.append(f'<p{advanced}><label>{_esc(label)}<br>{control}</label>{hint}</p>')
    presets = "".join(
        f"<li>{_esc(entry['label'])}: {entry['watts']:g} W average "
        f"({_esc(entry['basis'])})</li>"
        for entry in cost_module.WATTAGE_PRESETS.values())
    body = []
    if notice:
        body.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        body.append(f'<p class="error">{_esc(error)}</p>')
    body.append(
        "<p>Cost is computed by the same engine as chat and the completion "
        "record. Drop a G-code or sliced 3MF file below to auto-fill the "
        "amount and time; an unsliced file says so instead of costing "
        "zero. Cost is information only and never blocks a print.</p>"
        f'<form method="post" action="/cost"><input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        # Calculate sits at the top right of the form (#1718), not at
        # the bottom under every field.
        + '<div class="design-head"><p>Defaults are filled in and named '
          'under each field; change any of them to price this job '
          'differently.</p>'
          '<button type="submit" class="accent">Calculate cost</button></div>'
        + "".join(rows)
        + '</form>'
        f'<form method="post" action="/cost" enctype="multipart/form-data">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<p><label>Auto-fill from a file<br>'
        '<input type="file" name="file" accept=".gcode,.gco,.3mf,.txt"></label></p>'
        '<p><button type="submit">Read file</button></p></form>')
    if result is not None:
        cur = _esc(result.currency_code)
        lines = "".join(
            f"<tr><td>{_esc(entry['label'])}</td><td>{cur} {entry['amount']:.2f}</td></tr>"
            for entry in result.lines)
        tiers = "".join(
            f"<li>{_esc(rate)} percent {_esc(str(result.selling.get('mode')))}: "
            f"{cur} {price:.2f}</li>"
            for rate, price in sorted((result.selling.get("tiers") or {}).items(),
                                      key=lambda item: float(item[0])))
        detail = result.totals_detail
        body.append(
            "<h2>Breakdown</h2>"
            f'<div class="table-scroll"><table><tr><th>Line</th><th>Amount</th></tr>{lines}'
            f"<tr><th>Cost per build</th><th>{cur} {result.cost_per_build:.2f}</th></tr>"
            f"<tr><th>Cost per part</th><th>{cur} {result.cost_per_part:.2f}</th></tr></table></div>"
            f"<p>Cost per gram: {cur} {detail.get('cost_per_gram') if detail.get('cost_per_gram') is not None else 'not available'}; "
            f"cost per print hour: {cur} {detail.get('cost_per_print_hour') if detail.get('cost_per_print_hour') is not None else 'not available'}.</p>"
            f"<h2>Selling prices ({_esc(str(result.selling.get('mode_label')))})</h2><ul>{tiers}</ul>"
            f"<p>Priced from: {_esc(', '.join(result.provenance.get('price_sources') or []))}; "
            f"amounts from {_esc(str(result.provenance.get('grams_source')))}; "
            f"profile {_esc(str(result.provenance.get('printer_profile')))}; "
            f"machine rate over {_esc(str(result.provenance.get('lifetime_hours')))} lifetime hours.</p>")
    body.append(f"<h2>Shipped wattage presets</h2><ul>{presets}</ul>"
                "<p>The Flashforge AD5X preset is labeled an estimate: no "
                "reliable measured average for the AD5X was found; it uses "
                "the Adventurer 5M platform figure.</p>")
    if history:
        body.append(history)
    return shell.page("Print COST", "".join(body), path="/cost", theme=theme)


def history_cost_rows(records, limit=20):
    """Rows for finished prints that carry a frozen cost, newest first (#1632 review).

    Each row: the print, its outcome, out of pocket, and the full cost.
    """
    rows = []
    priced = [r for r in records or [] if isinstance(r, dict) and isinstance(r.get("cost"), dict)]
    priced.sort(key=lambda r: str(r.get("completed_at") or r.get("started_at") or ""), reverse=True)
    for record in priced[:limit]:
        frozen = record["cost"]
        cur = html.escape(str(frozen.get("currency_code") or "USD"))
        title = html.escape(str(record.get("title") or record.get("gcode_file") or "print"))
        status = html.escape(str(record.get("status") or frozen.get("outcome") or ""))
        pocket = frozen.get("out_of_pocket")
        pocket = float(frozen.get("cost_per_build") or 0) if pocket is None else float(pocket)
        rows.append(f"<tr><td>{title}</td><td>{status}</td><td>{cur} {pocket:.2f}</td>"
                    f"<td>{cur} {float(frozen.get('cost_per_build') or 0):.2f}</td></tr>")
    return rows
