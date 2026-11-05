"""Shared vocabulary and provenance checks for edit corpus journeys.

This module deliberately does not pretend that a resolved plan is a sliced
result. A caller must supply actual G-code from its probe to verify an edit.
"""
from print_handy.core import edit_plan


def resolve_plan(text, job, source):
    """Resolve and enrich one unambiguous chat request without mutating job."""
    resolved = edit_plan.resolve_request(text)
    if not resolved or resolved[0] != "plan":
        raise ValueError(f"not an unambiguous edit: {text!r}: {resolved!r}")
    return edit_plan.enrich_plan(resolved[1], job, source)


def verify_probe(plan, before_job, gcode_text):
    """Compare independently produced slicer output to the requested plan."""
    return edit_plan.verify_slice(plan, gcode_text=gcode_text,
                                  before_dims=edit_plan.job_dims(before_job))
