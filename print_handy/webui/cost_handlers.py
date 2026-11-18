"""Handlers for the Web UI cost calculator page (#1625).

GET renders the calculator; a urlencoded POST computes from the typed
fields; a multipart POST receives one dropped file through the shared
upload receive and auto-fills amount and time from it. All math is
core.cost; an unsliced file renders its not-sliced state, never a zero
cost.
"""
from __future__ import annotations

import logging

from ..core import cost_parse
from . import cost_page as cost_page_module
from . import data as data_module

logger = logging.getLogger(__name__)


def _render(handler, **kwargs):
    handler._send(200, cost_page_module.render_cost_page(
        handler.server.settings_data, theme=handler._theme(),
        form_token=handler._form_token(), **kwargs),
        "text/html; charset=utf-8")


def cost_page(handler):
    _render(handler)


def cost_post(handler, form):
    if form is None:  # multipart: a dropped file to auto-fill from
        def handle(fields, files):
            if not files:
                _render(handler, error="No file was attached. Nothing was read.")
                return
            _name, path = files[0]
            try:
                usage = cost_parse.usage_from_file(path)
            except Exception:
                logger.exception("cost auto-fill could not parse the dropped file")
                _render(handler, error="That file could not be read. Nothing was changed.")
                return
            if not usage.sliced:
                _render(handler, error=f"Cannot estimate yet: {usage.not_sliced_reason}.")
                return
            amount = usage.volume_ml if usage.volume_ml is not None else (usage.total_grams or 0)
            filled = {"technology": ["resin" if usage.volume_ml is not None else "fdm"],
                      "amount": [str(amount)], "hours": [str(round(usage.hours, 3))],
                      "parts": ["1"], "prep_minutes": ["5"], "post_minutes": ["10"],
                      "pack_minutes": ["0"], "hardware": ["0"], "packaging": ["0"]}
            try:
                job, profile, workspace = cost_page_module.job_from_form(
                    filled, handler.server.settings_data)
                job.grams_source = usage.source
                if usage.slicer_price_per_kg and job.lines:
                    job.lines[0].slicer_price_per_kg = usage.slicer_price_per_kg
                from ..core import cost as cost_module
                result = cost_module.compute(job, profile, workspace)
            except Exception:
                logger.exception("cost auto-fill compute failed")
                _render(handler, form=filled,
                        error="The file was read but the cost could not be computed.")
                return
            _render(handler, form=filled, result=result,
                    notice=f"Auto-filled from the file ({usage.source}).")
        refused = handler._receive_upload(handle)
        if refused:
            handler._html_refusal(refused[0], refused[1], "/cost")
        return
    try:
        job, profile, workspace = cost_page_module.job_from_form(
            form, handler.server.settings_data)
        from ..core import cost as cost_module
        result = cost_module.compute(job, profile, workspace)
    except (TypeError, ValueError):
        _render(handler, form=form,
                error="One of the numbers could not be read. Check the fields and try again.")
        return
    _render(handler, form=form, result=result)


def cost_history_fragment(data, records_vault=None):
    """Completed prints with their frozen costs, for history views."""
    from ..core import print_stats
    vault = data_module._vault(data, records_vault)
    records, _skipped = print_stats.load_records(vault)
    rows = cost_page_module.history_cost_rows(records)
    if not rows:
        return ""
    return ("<h2>Recorded print costs</h2><table><tr><th>Print</th><th>Cost</th></tr>"
            + "".join(rows) + "</table>")
