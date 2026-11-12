"""Material mismatches at print start: a warning the operator may accept, never a hard block (#1336).

Owner ruling (catduck, 2.2.9 #1190/#1213, restated 2026-11-11): no filament or
material condition hard-blocks a print. A mismatch is a warning that names the
problem and its consequence, and the operator can accept it and print.

#1190/#1213 made that true inside slicing. This module is the start-time half:
every launch check that compares the G-code's material with what is loaded (or
the loaded material with the nozzle) calls ``problem`` at its decision point.

- Not accepted: ``problem`` returns the warning text carrying an ``[accept KEY]``
  token. The check refuses as before, and when every problem in the refusal is
  such a warning the refusal starts with ``PREFIX``, which the chat flows read
  (``pending``) to ask "Reslice for <loaded>" (the default) / "Print as sliced"
  / Cancel. Mixed refusals (a material warning plus a real fault) are plain
  refusals: the fault has to be fixed first, so no accept is offered.
- Accepted: ``problem`` prints the warning (nothing is silent) and returns [],
  and the run is marked as having used an acceptance.

An acceptance lives only for one print-now run (``accepting``), and only when
the operator's answer named the exact G-code SHA-256 that run verifies: it is
never written to the job and never carries over to another G-code, a reprint
or the next copy. Keys name the materials involved, so a later run that sees a
different mismatch asks again.

Safety that no acceptance waives: the printer's mechanical limits (axis and
heater maxima, ``model_job.require_mechanical_limits``) run on every accepted
start, and an accepted start re-checks the loaded material against the nozzle
(``recheck_loaded_nozzle``) and shows the result: abrasive filament on a brass
nozzle is its own warning, worded plainly, accepted separately.
"""
from __future__ import annotations

import contextlib
import contextvars
import re

PREFIX = 'material warning, your answer is needed: '
RESLICE_LABEL = 'Reslice for the loaded material'
PRINT_AS_SLICED_LABEL = 'Print as sliced'
PRINT_ANYWAY_LABEL = 'Print anyway'
CANCEL_LABEL = 'Cancel'
TOKEN = re.compile(r'\s*\[accept ([^\]]+)\]')

# What each kind of warning costs, in the operator's words (every warning names its consequence).
CONSEQUENCE = {
    'material': ('printing it as sliced heats to the G-code\'s own temperatures, which are for the sliced '
                 'material, not the loaded one: expect weak layers, poor bed adhesion, stringing or a clog'),
    'abrasive': 'this filament will wear out the nozzle: abrasive filament grinds a brass or soft nozzle '
                'oversize within a few prints, ruining the nozzle and dimensional accuracy',
    'not_recommended': 'the printer maker does not recommend this material on this printer: expect clogs, '
                       'warping or extra wear',
    'tray': 'the print feeds from a tray that does not hold the material the G-code was sliced for',
    'remap': 'the slot holds another material than the one the G-code was sliced for, so it prints at the '
             'sliced temperatures',
    'label': 'the printer screen names another material for this slot than the record does',
    'filament': ('the spool can run out partway through: the print then pauses (or stops) until the slot '
                 'is reloaded, and a color can end up missing or short'),
}

# Kinds a reslice cannot help: the answer is print anyway or cancel, never Reslice.
NO_RESLICE = frozenset({'filament'})

_ACCEPTED = contextvars.ContextVar('material_accept_keys', default=frozenset())
_ACCEPT_ALL = contextvars.ContextVar('material_accept_all', default=False)
_USED = contextvars.ContextVar('material_accept_used', default=None)


def _part(value):
    return ' '.join(str(value if value is not None else 'none').replace(']', ')').replace('|', '/').split())


def key(kind, *parts):
    """The stable key of one warning: its kind and the materials (and slot or tray) it names."""
    return '|'.join([kind, *(_part(p) for p in parts)])


def accepted(warning_key):
    return _ACCEPT_ALL.get() or warning_key in _ACCEPTED.get()


def problem(kind, text, *parts):
    """[] when this run accepted the warning (it is printed, never silent), else [text + its token]."""
    warning_key = key(kind, *parts)
    text = ' '.join(str(text).split())
    if accepted(warning_key):
        label = PRINT_ANYWAY_LABEL if kind in NO_RESLICE else PRINT_AS_SLICED_LABEL
        print(f'{"FILAMENT" if kind in NO_RESLICE else "MATERIAL"} WARNING ACCEPTED: {text}; '
              f'{CONSEQUENCE.get(kind, "")}. The operator chose {label} for this G-code.', flush=True)
        used = _USED.get()
        if used is not None and warning_key not in used:
            used.append(warning_key)
        return []
    return [f'{text} [accept {warning_key}]']


def message(problems, prefix):
    """The refusal text for problems: PREFIX when each one is an acceptable warning, else ``prefix``."""
    problems = [str(p) for p in problems]
    lead = PREFIX if problems and all(TOKEN.search(p) for p in problems) else prefix
    return lead + '; '.join(problems)


def used():
    """The warning keys this run accepted ([] outside an accepting run)."""
    return list(_USED.get() or [])


@contextlib.contextmanager
def accepting(keys=(), *, accept_all=False, gcode_sha256=None, expected_sha256=None):
    """Hold the operator's acceptances for one print-now run.

    ``expected_sha256`` is the SHA the operator's answer was given for; when it
    is not the SHA of the G-code this run verifies, the acceptance is dropped
    (and said so): an answer never carries to a different G-code.
    """
    keys = frozenset(str(k) for k in keys or ())
    if (keys or accept_all) and expected_sha256 and gcode_sha256 and expected_sha256 != gcode_sha256:
        print('MATERIAL WARNING: the accepted warnings were for another G-code; they do not apply to '
              'this one and are asked again.', flush=True)
        keys, accept_all = frozenset(), False
    t_keys, t_all, t_used = _ACCEPTED.set(keys), _ACCEPT_ALL.set(bool(accept_all)), _USED.set([])
    try:
        yield
    finally:
        _USED.reset(t_used)
        _ACCEPT_ALL.reset(t_all)
        _ACCEPTED.reset(t_keys)


def active():
    """True inside a run that holds any acceptance."""
    return bool(_ACCEPT_ALL.get() or _ACCEPTED.get())


def recheck_loaded_nozzle(nozzle_type, loaded_material):
    """After an accepted mismatch: the loaded material against the nozzle, shown and asked for itself.

    Raises RuntimeError carrying the abrasive warning (an acceptable problem)
    unless this run accepted it too; prints the outcome either way.
    """
    from .nozzle import AbrasiveNozzleError, NozzleError, check_nozzle_material_safety, normalize_nozzle_type
    try:
        check_nozzle_material_safety(nozzle_type, loaded_material)
    except AbrasiveNozzleError:
        try:
            nozzle = normalize_nozzle_type(nozzle_type).replace('_', ' ')
        except NozzleError:
            nozzle = str(nozzle_type)
        found = abrasive_problem(loaded_material, nozzle)
        if found:
            raise RuntimeError(message(found, 'the loaded material does not suit the nozzle: '))
        return
    except NozzleError:
        return
    if loaded_material:
        print(f'NOZZLE CHECK: the loaded {loaded_material} suits the fitted nozzle.', flush=True)


def abrasive_text(material, nozzle):
    """The abrasive-on-a-soft-nozzle warning, in plain words (#1336 ruling); ABRASIVE_TEXT reads it back."""
    return (f'{material} is abrasive and the fitted nozzle is {nozzle}: this filament will wear out the '
            f'{nozzle} nozzle')


ABRASIVE_TEXT = re.compile(r'^(.+?) is abrasive and the fitted nozzle is (.+?): this filament will wear out')


def abrasive_problem(material, nozzle):
    return problem('abrasive', abrasive_text(material, nozzle), material, nozzle)


def material_problem(sliced, loaded, where=''):
    """The sliced-versus-loaded warning (site 1 of #1336)."""
    return problem('material', f"G-code filament_type {sliced!r} != loaded {loaded!r}{where}: the G-code was "
                               f'sliced for {sliced} and {loaded} is loaded', sliced, loaded)


def shortage_problem(slot, spool_id, name, need_g, have_g, source, position_noun='Slot'):
    """A spool with less filament than the print needs: a warning to accept, never a block.

    ``source`` is where ``have_g`` came from ('spoolman' or 'record'). A 0 g
    reading is said as such, so the operator can tell an empty spool from a
    Spoolman spool whose weight was never set.
    """
    where = f'{position_noun} {slot}' if slot is not None else 'The loaded spool'
    spool = ' '.join(str(x) for x in (name, f'spool #{spool_id}' if spool_id is not None else '') if x)
    said = 'Spoolman says' if source == 'spoolman' else 'the slot record says'
    text = (f'{where} ({spool}) needs about {need_g:g} g but {said} the spool has {have_g:g} g left, '
            f'short by about {round(need_g - have_g, 1):g} g')
    if have_g <= 0:
        text += (' (Spoolman shows 0 g: if the spool is not empty, its remaining weight may not be set '
                 'in Spoolman)' if source == 'spoolman' else ' (0 g recorded for this slot)')
    return problem('filament', text, slot, spool_id)


def pending(tail):
    """Every acceptable warning one refused print-now run stopped on, or [] (also for a mixed refusal)."""
    lines = [ln.strip() for ln in str(tail or '').splitlines() if ln.strip()]
    raw = next((ln for ln in reversed(lines) if ln.startswith('REFUSED:')), '')
    if PREFIX not in raw:
        return []
    body = raw.split(PREFIX, 1)[1]
    found = []
    for match in TOKEN.finditer(body):
        start = body.rfind('; ', 0, match.start())
        text = body[start + 2 if start >= 0 else 0:match.start()].strip()
        warning_key = match.group(1)
        kind, *parts = warning_key.split('|')
        if all(item['key'] != warning_key for item in found):
            found.append({'key': warning_key, 'kind': kind, 'parts': parts, 'text': text})
    return found


def loaded_name(warnings):
    """The loaded material the warnings name, for the Reslice button ('' when none names one)."""
    for item in warnings:
        if item['kind'] in ('material', 'remap', 'tray') and len(item['parts']) >= 2:
            return item['parts'][-1]
    return ''


def reslice_label(warnings):
    loaded = loaded_name(warnings)
    return f'Reslice for {loaded}' if loaded else RESLICE_LABEL


def shortage_only(warnings):
    """True when every warning is one a reslice cannot help (a filament shortage)."""
    return bool(warnings) and all(item['kind'] in NO_RESLICE for item in warnings)


def print_label(warnings):
    """The button that accepts these warnings: Print anyway for a shortage, else Print as sliced."""
    return PRINT_ANYWAY_LABEL if shortage_only(warnings) else PRINT_AS_SLICED_LABEL


def choices(warnings):
    """Reslice first (the default answer), then Print as sliced, then Cancel.

    A filament shortage alone has nothing to reslice: Print anyway, then Cancel.
    """
    if shortage_only(warnings):
        return [PRINT_ANYWAY_LABEL, CANCEL_LABEL]
    return [reslice_label(warnings), PRINT_AS_SLICED_LABEL, CANCEL_LABEL]


def question(warnings):
    lines = []
    for item in warnings:
        consequence = CONSEQUENCE.get(item['kind'], '')
        lines.append(f"- {item['text']}" + (f'. If you print anyway, {consequence}.' if consequence else '.'))
    if shortage_only(warnings):
        return ('Heads-up: ' + ('a spool is' if len(lines) == 1 else 'some spools are')
                + ' short of filament for this print.\n' + '\n'.join(lines)
                + '\nNothing is uploaded yet. Load more filament (or correct the weight in Spoolman) and '
                  'start again, print anyway, or cancel?')
    return ('Heads-up: ' + ('a material warning' if len(lines) == 1 else 'some material warnings')
            + ' before this print starts.\n' + '\n'.join(lines)
            + '\nNothing is uploaded yet. Reslicing for what is loaded is the safe choice. '
              'Print as sliced keeps the G-code as it is (its own temperatures, never past the '
              "printer's limits), or cancel?")


def reslice_message(warnings):
    if shortage_only(warnings):
        return ('Not started. Load more filament into the short slot (or correct the spool weight in '
                'Spoolman), then start again. Nothing uploaded or started.')
    loaded = loaded_name(warnings) or 'the loaded material'
    return (f'Not started. To reslice for {loaded}, send the model or link again (or slice the job again) '
            f'and it is sliced for what is loaded now. Nothing uploaded or started.')


def argv(keys, gcode_sha256):
    """print-now arguments carrying one answer, bound to the exact G-code it was given for."""
    out = []
    for warning_key in keys or ():
        out += ['--accept-material', str(warning_key)]
    if out and gcode_sha256:
        out += ['--accept-material-sha256', str(gcode_sha256)]
    return out

