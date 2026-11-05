"""Slicer warnings from a successful slice (Orca/Bambu Studio ``result.json``).

Orca reports non-fatal findings in ``result.json`` (``warnings[]`` with a class, and
``sliced_plates[].warning_message``). Print Handy used to read that file only on failure, so
"floating cantilever ... enable support generation" never reached the operator.

Policy (Andrew, 2026-11-04):
  * Only the support finding (floating cantilever / floating regions / "enable support
    generation") ever pauses a job, and only as a question, never a block.
  * Every other warning is information: de-duplicated, one compact line, harmless ones listed in
    ``print_handy/data/slice_warning_ignore.json`` are logged and not shown.
  * An author 3MF ``enable_support = 0`` is not an operator decision. Only a recorded decision
    (``job['support_decision']`` or an orientation choice that carried ``supports``) is.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

IGNORE_FILE = Path(__file__).resolve().parent.parent / 'data' / 'slice_warning_ignore.json'

_SUPPORT = re.compile(r'floating\s+(?:cantilever|region)|enable\s+support\s+generation', re.I)
_OBJECT = re.compile(r'object\s+(.+?)\s+has\b', re.I)


def _norm(text):
    return ' '.join(str(text).split())


def _ignore_patterns(path=None):
    """Compiled patterns from the ignore list; a missing or broken file ignores nothing."""
    try:
        rows = json.loads(Path(path or IGNORE_FILE).read_text()).get('ignore') or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for row in rows:
        try:
            out.append(re.compile(row['pattern'], re.I))
        except (KeyError, TypeError, re.error):
            logger.warning('bad slice warning ignore entry: %r', row)
    return out


def _texts(payload):
    """Every warning string in a result.json payload, tolerant of shape (dicts, strings, lists)."""
    found = []

    def add(value):
        if isinstance(value, dict):
            value = value.get('text') or value.get('message')
        if isinstance(value, str) and value.strip():
            found.append(_norm(value))

    warnings = payload.get('warnings')
    for row in warnings if isinstance(warnings, list) else ([warnings] if warnings else []):
        add(row)
    plates = payload.get('sliced_plates')
    for plate in plates if isinstance(plates, list) else []:
        if isinstance(plate, dict):
            message = plate.get('warning_message')
            for piece in message if isinstance(message, list) else [message]:
                add(piece)
    return found


def parse(result, ignore_path=None):
    """Classify a result.json (path or already-loaded dict).

    Returns ``{'support_needed': bool, 'objects': [...], 'support_texts': [...],
    'info': [...], 'ignored': [...]}``; ``None`` when the file is absent, unreadable or reports a
    failed slice (failures have their own path).
    """
    payload = result
    if not isinstance(result, dict):
        try:
            payload = json.loads(Path(result).read_text())
        except (OSError, ValueError, TypeError):
            return None
    if not isinstance(payload, dict) or payload.get('return_code') not in (0, None):
        return None
    patterns = _ignore_patterns(ignore_path)
    seen, objects, support_texts, info, ignored = set(), [], [], [], []
    for text in _texts(payload):
        if text in seen:
            continue
        seen.add(text)
        if _SUPPORT.search(text):
            support_texts.append(text)
            for name in _OBJECT.findall(text):
                if name not in objects:
                    objects.append(name)
        elif any(p.search(text) for p in patterns):
            ignored.append(text)
        else:
            info.append(text)
    if ignored:
        logger.info('slice warnings ignored by list: %s', '; '.join(ignored))
    return {'support_needed': bool(support_texts), 'objects': objects,
            'support_texts': support_texts, 'info': info, 'ignored': ignored}


def provenance_fields(folder, process, supports):
    """What slice_model records next to the artifact: the parsed warnings and the support state."""
    try:
        parsed = parse(Path(folder) / 'result.json')
    except Exception:  # warnings are visibility; they must never fail a slice
        logger.exception('could not read slicer warnings')
        parsed = None
    return {'slice_warnings': parsed,
            'supports_on': str(process.get('enable_support')) == '1',
            'support_decided': supports is not None}


def _provenances(job):
    rows = [job.get('provenance')] + [p.get('provenance') for p in job.get('derived_plates') or []
                                      if isinstance(p, dict)]
    return [r for r in rows if isinstance(r, dict)]


def operator_decided(job):
    """True when the operator, not the 3MF author, settled supports for this job."""
    if isinstance((job.get('support_decision') or {}).get('supports'), bool):
        return True
    orientation = job.get('orientation') or {}
    return bool(orientation.get('rotation')) and isinstance(orientation.get('supports'), bool)


def support_mismatch(job):
    """Artifacts sliced with supports off although the operator chose supports (fail closed).

    Rows from before slice warnings were recorded carry no ``supports_on`` and are skipped.
    """
    if (job.get('support_decision') or {}).get('supports') is not True:
        return []
    rows = [('the main slice', job.get('provenance'))] + [
        (f"plate {p.get('plate_id') or p.get('binding_key') or '?'}", p.get('provenance'))
        for p in job.get('derived_plates') or [] if isinstance(p, dict)]
    return [name for name, prov in rows
            if isinstance(prov, dict) and 'supports_on' in prov and not prov['supports_on']]


def support_finding(job):
    """The unanswered support question for a sliced job, or None.

    Fires only when the slicer said supports are needed, the slice ran with supports off, and no
    operator decision exists on the job. Reprints and retries therefore never ask twice.
    """
    if operator_decided(job):
        return None
    objects, texts = [], []
    for prov in _provenances(job):
        found = prov.get('slice_warnings') or {}
        if found.get('support_needed') and not prov.get('supports_on') and not prov.get('support_decided'):
            objects += [o for o in found.get('objects') or [] if o not in objects]
            texts += [t for t in found.get('support_texts') or [] if t not in texts]
    if not texts:
        return None
    return {'objects': objects, 'texts': texts}


QUESTION_PREFIX = 'The slicer says this will not print cleanly without supports'


def question(finding):
    names = ', '.join(finding['objects']) or 'part of the model'
    return (QUESTION_PREFIX + ': '
            f'{names} has floating regions or a cantilever. Supports are off in the file.\n'
            f'> {finding["texts"][0]}\nWhat should I do?')


def info_lines(job):
    """De-duplicated non-support warnings across a job's artifacts, in first-seen order."""
    out = []
    for prov in _provenances(job):
        for text in (prov.get('slice_warnings') or {}).get('info') or []:
            if text not in out:
                out.append(text)
    return out


def compact_info(job):
    """One line for the pre-print summary, or ''. The full list stays in the job's provenance."""
    lines = info_lines(job)
    if not lines:
        return ''
    more = f' (+{len(lines) - 1} more in the job details)' if len(lines) > 1 else ''
    return f'Slicer note ({len(lines)}): {lines[0]}{more}'
