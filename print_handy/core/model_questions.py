"""Questions about the current model job, answered instead of turned into edits (#903).

Detection runs before edit-plan resolution, so "does this need supports?" gets an
answer from the job's own slice result and mesh, never an action. Answers are
deterministic: slice provenance, the #904 slicer warnings, modeled-support
measurement (print_handy.core.modeled_supports), job dimensions and the bed size.
"""

import re

_QUESTION_START = re.compile(
    r"^\s*(?:(?:hey|so|ok|okay|um)\W+)?"
    r"(?:does|do|did|is|are|has|have|will|would|can|could|should|how|what|which)\b",
    re.IGNORECASE)
_SUPPORTS = re.compile(r"\bsup+[oe]?r?ts?\b|\bsupport", re.IGNORECASE)
_SIZE = re.compile(r"\bhow\s+(?:big|large|tall|wide|small)\b|\bwhat\s+(?:size|dimensions?)\b"
                   r"|\bdimensions?\b", re.IGNORECASE)
_FIT = re.compile(r"\b(?:will|would|does|can)\s+(?:it|this|that)\s+fit\b|\bfit\s+on\s+the\s+bed\b",
                  re.IGNORECASE)


# A polite request is a command, not a question: "can you add supports",
# "could you shrink it?", "please add a brim" go to the edit path.
_EDIT_VERBS = (r"add|remove|delete|drop|shrink|scale|resize|rotate|flip|mirror|make|print|turn|"
               r"enlarge|grow|reduce|double|halve|change|set|switch|use|put|enable|disable|"
               r"slice|re-?slice|split|move|lay|cut")
_POLITE_REQUEST = re.compile(
    r"^\s*(?:(?:hey|so|ok|okay|um)\W+)?(?:please\W+)?"
    r"(?:(?:can|could|would|will)\s+(?:you|u|ya)\s+(?:please\s+)?(?:just\s+)?|please\s+)"
    r"(?:" + _EDIT_VERBS + r")\b",
    re.IGNORECASE)


def is_polite_request(text):
    return bool(_POLITE_REQUEST.match(str(text or '')))


def is_question(text):
    text = str(text or '').strip()
    if not text or is_polite_request(text):
        return False
    return text.endswith('?') or bool(_QUESTION_START.match(text))


def classify(text):
    """'supports', 'size', 'fit', or None for anything that is not a model question."""
    if not is_question(text):
        return None
    if _SUPPORTS.search(text):
        return 'supports'
    if _FIT.search(text):
        return 'fit'
    if _SIZE.search(text):
        return 'size'
    return None


def _provenances(job):
    rows = [job.get('provenance')] + [p.get('provenance') for p in job.get('derived_plates') or []
                                      if isinstance(p, dict)]
    return [p for p in rows if isinstance(p, dict)]


def supports_answer(job, source=None):
    """(text, offer) for a supports question. ``offer`` is True when the
    Re-slice with supports / Print without choice should be offered."""
    from print_handy.core import modeled_supports
    provs = _provenances(job)
    sliced_on = [p.get('supports_on') for p in provs if 'supports_on' in p]
    warnings = []
    for prov in provs:
        found = prov.get('slice_warnings') or {}
        if found.get('support_needed'):
            warnings += [t for t in found.get('support_texts') or [] if t not in warnings]
    lines = []
    if not sliced_on:
        lines.append('This job has not been sliced yet, so I have no slicer result for supports.')
    elif any(sliced_on):
        lines.append('Supports are on for this slice.')
    else:
        lines.append('Supports are off for this slice.')
    if warnings:
        lines.append('The slicer reported floating regions or a cantilever:')
        lines += [f'> {text}' for text in warnings[:3]]
    elif sliced_on:
        lines.append('The slicer reported no floating regions or cantilevers.')
    lines += modeled_supports.safe_describe(source)
    from print_handy.core import support_clearance
    lines += support_clearance.describe(job.get('support_clearance') or support_clearance.safe_analyze(source))
    decision = job.get('support_decision')
    if isinstance(decision, dict):
        lines.append('Decision already recorded: print '
                     + ('with' if decision.get('supports') else 'without') + ' supports.')
    offer = bool(warnings) and not any(sliced_on)
    return '\n'.join(lines), offer


def _dims(job):
    from . import edit_plan
    dims = edit_plan.job_dims(job)
    audit = job.get('audit') or {}
    if dims is None:
        raw = job.get('dimensions_mm') or (job.get('audit') or {}).get('dimensions_mm') or ()
        try:
            dims = tuple(float(v) for v in raw)[:3]
        except (TypeError, ValueError):
            return None
    if len(dims) != 3:
        return None
    scale = job.get('scale') or 1.0
    try:
        scale = float(scale)
    except (TypeError, ValueError):
        scale = 1.0
    orientation = job.get('orientation') or {}
    from_orientation = bool(orientation.get('dims_full_mm'))
    if (from_orientation or not audit.get('scaled')) and scale != 1.0:
        dims = tuple(v * scale for v in dims)
    return list(dims)


def size_answer(job):
    dims = _dims(job)
    if dims is None:
        return 'I do not have measured dimensions for this job yet.'
    x, y, z = dims
    return f'The model is {x:.1f} x {y:.1f} x {z:.1f} mm (X x Y x Z).'


def fit_answer(job, bed_mm):
    dims = _dims(job)
    if dims is None:
        return 'I do not have measured dimensions for this job yet, so I cannot check the fit.'
    try:
        bx, by = float(bed_mm[0]), float(bed_mm[1])
        bz = float(bed_mm[2]) if len(bed_mm) > 2 else None
    except (TypeError, ValueError, IndexError):
        return size_answer(job) + ' I could not read the bed size.'
    x, y, z = dims
    flat = (x <= bx and y <= by) or (x <= by and y <= bx)
    tall = bz is None or z <= bz
    bed = f'{bx:g} x {by:g}' + (f' x {bz:g}' if bz else '') + ' mm'
    if flat and tall:
        return f'Yes. The model is {x:.1f} x {y:.1f} x {z:.1f} mm and the bed is {bed}.'
    return f'No. The model is {x:.1f} x {y:.1f} x {z:.1f} mm and the bed is {bed}.'
