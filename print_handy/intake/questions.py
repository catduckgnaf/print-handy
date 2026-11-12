"""The intake's chat questions: the ask helper, the color and label confirmations and their
recorded acceptances, and the reply parsers they share.

Moved verbatim from ``print_handy.core.prepared_intake`` (#1323). The old names are still
importable from there for one release.
"""
import contextlib, contextvars, json, re, time
from pathlib import Path

from ..core.link_intake import ask


def _typo_words(value):
    """Fold known misspellings word by word ("shrnik" -> "shrink"), keeping everything else as typed."""
    from ..core.fuzzy_vocabulary import TYPO_FORMS
    return ' '.join(TYPO_FORMS.get(word, word) for word in value.split())


def _retry_choice_reply(text, rows):
    """Typed answers for a Retry/Cancel question: 'retry'/'try again' or 'cancel'."""
    value = str(text or '').strip().casefold()
    if value in {'retry', 'try again', 'retry fit check'}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'retry']
    elif value in {'cancel', 'stop', "don't print"}:
        matches = [i for i, row in enumerate(rows) if row['kind'] == 'cancel']
    else:
        return None
    if len(matches) != 1:
        raise ValueError('that choice is not available; choose a listed button')
    return matches[0]


_LIVE_KEY = contextvars.ContextVar('prepared_intake_live_key', default=None)


def _color_guess_text(mj, job, decisions):
    """Display only: say so when a slot this job uses has no real color code.

    A slot whose color could not be worked out previews in neutral gray;
    the operator hears that before slicing rather than discovering it.
    """
    try:
        slots = {row.get('slot') for row in ((decisions or {}).get('choices') or {}).values()
                 if isinstance(row, dict) and row.get('slot') is not None}
        selection = (job or {}).get('filament_selection') or {}
        if isinstance(selection, dict) and selection.get('slot') is not None:
            slots.add(selection['slot'])
        from ..core.filament_color import chat_notes
        if not slots:
            return ''
        return '\n'.join(chat_notes(mj.printer_state.load(), slots))
    except Exception:
        return ''


async def _ask(adapter, source, key, question, choices, timeout=None, **kwargs):
    """ask() registered under the live chat session so typed replies reach the prompt."""
    live_key = kwargs.pop('live_key', None) or _LIVE_KEY.get() or key
    on_shown = kwargs.pop('on_shown', None)
    if on_shown is not None:  # fire as the question goes up, before the operator answers
        on_shown()
    from ..core import yolo
    if yolo.ACTIVE.get() and choices:
        # Yolo answers every question with its go option, out loud (no silent skips).
        pick = yolo.auto_answer(choices)
        if pick is not None:
            # An AI fix is a model change a person must see and confirm
            # (#978/#985): yolo never picks the fix offer or an apply button,
            # so it goes to a person like a restart or a rescale does.
            from ..core import ai_fix as _ai_fix
            if str(list(choices)[pick]) in (_ai_fix.FIX_LABEL, _ai_fix.APPLY_LABEL,
                                            _ai_fix.APPLY_KEEP_OPENINGS_LABEL):
                pick = None
        first = next((ln.strip() for ln in str(question or '').splitlines() if ln.strip()), 'question')
        if len(first) > 120:
            first = first[:117] + '...'
        meta = {'thread_id': source.thread_id} if getattr(source, 'thread_id', None) else None
        if pick is not None:
            with contextlib.suppress(Exception):
                await adapter.send(chat_id=source.chat_id, metadata=meta,
                                   content=f'Yolo: {first} -> {list(choices)[pick]}')
            return pick
        # A printer action or a model change: yolo never answers these itself.
        with contextlib.suppress(Exception):
            await adapter.send(chat_id=source.chat_id, metadata=meta,
                               content=f'Yolo does not answer this one; a person has to: {first}')
    return await ask(adapter, source, key, question, list(choices), timeout, live_key=live_key, **kwargs)


_NOISE = re.compile(r'^(?:TIMING \{|G-CODE OK:)')


def _raw_preupload_refusal(tail):
    """Return the refusal line, excluding timing and preflight diagnostics."""
    lines = [ln.strip() for ln in str(tail or '').splitlines()
             if ln.strip() and not _NOISE.match(ln.strip())]
    return next((ln for ln in reversed(lines) if ln.startswith('REFUSED:')), '')


_CONFIRM_COLOR = re.compile(r'printer slot (\d+) color is (.+?) but Spoolman has (.+?) there \(T(\d+)\)')
PRINT_ANYWAY_LABEL = 'Print anyway'
SET_SCREEN_LABEL = 'Set screen to Spoolman color, then print'
USE_LOADED_SPOOL_LABEL = 'Use the loaded spool · bed is clear'
SPOOL_CHANGED = 'spool changed since slicing'  # the multicolor contract's refusal (#1336 asks instead)
COLOR_CONFIRM_ROUNDS = 3  # the screen may change again between answers; the last round surfaces a Retry refusal
_COLOR_NAMES = {
    'red': 'E53935', 'orange': 'FB8C00', 'yellow': 'FDD835', 'green': '43A047', 'teal': '00897B',
    'blue': '1E88E5', 'navy': '1A237E', 'purple': '8E24AA', 'pink': 'EC407A', 'brown': '6D4C41',
    'bronze': 'CD7F32', 'gold': 'D4AF37', 'silver': 'B0B0B0', 'gray': '808080', 'white': 'FFFFFF',
    'black': '000000', 'beige': 'D8C3A5'}


def color_confirm(tail):
    """Every slot color difference print-now stopped on before upload, in slot order ([] if none) (#795)."""
    raw = _raw_preupload_refusal(tail)
    if not raw.startswith('REFUSED:'):
        return []
    return [{'slot': int(m[1]), 'live': m[2], 'recorded': m[3], 'tool': int(m[4])}
            for m in _CONFIRM_COLOR.finditer(raw)]


def _color_phrase(shown):
    """A shown hex in plain words ('red #E53935'); anything that is not a hex stays as shown."""
    from ..core import filament_color
    distances = {name: filament_color.color_distance(shown, hex_) for name, hex_ in _COLOR_NAMES.items()}
    distances = {name: d for name, d in distances.items() if d is not None}
    if not distances:
        return shown
    return f"{min(distances, key=distances.get)} {shown}"


def color_confirm_question(confirms):
    lines = [f"slot {c['slot']} (T{c['tool']}): the screen says {_color_phrase(c['live'])}, "
             f"Spoolman has {_color_phrase(c['recorded'])}" for c in confirms]
    return ("Heads-up: " + ('a slot color differs' if len(lines) == 1 else 'some slot colors differ')
            + " between the printer screen and Spoolman.\n" + "\n".join(lines)
            + "\nNothing is uploaded yet. Print anyway with the loaded spools, update the screen to the "
              "Spoolman colors and print, or cancel?")


def screen_unpushed_note(confirms):
    """The post for slots 'Set screen to Spoolman color' could not update: the print goes ahead as is (#798)."""
    slots = [f"slot {c['slot']}" for c in confirms]
    named = slots[0] if len(slots) == 1 else ', '.join(slots[:-1]) + ' and ' + slots[-1]
    return f"Couldn't update the screen color for {named}, so I'm printing with the screen color unchanged."


def accept_color_mismatch(job_json, target, confirms):
    """Record a single-use Print anyway for this exact G-code and these listed differences (#795)."""
    from ..core import model_job as mj
    job = json.loads(Path(job_json).read_text())
    keys = [mj.color_mismatch_key(c['slot'], c['live'], c['recorded']) for c in confirms]
    prior = mj.accepted_color_mismatches(job, target['gcode_sha256'])  # a later round adds to the earlier ones
    job['color_mismatch_accepted'] = {
        'gcode_sha256': target['gcode_sha256'],
        'slots': sorted(prior | set(keys)),
        'granted_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'via': 'button'}
    mj.write_job(Path(job_json).parent, job)
    return job['color_mismatch_accepted']


def clear_color_acceptance(job_json):
    """Consume the acknowledgement: it never carries over to a later start."""
    from ..core import model_job as mj
    job = json.loads(Path(job_json).read_text())
    if job.pop('color_mismatch_accepted', None) is not None:
        mj.write_job(Path(job_json).parent, job)


_CONFIRM_LABEL = re.compile(r'printer slot (\d+) is labeled (.+?) but Spoolman has (.+?) there \(T(\d+)\)')


def label_confirm(tail):
    """Every slot material-label difference print-now stopped on before upload, in slot order ([] if none) (#839)."""
    raw = _raw_preupload_refusal(tail)
    if not raw.startswith('REFUSED:'):
        return []
    return [{'slot': int(m[1]), 'live': m[2], 'recorded': m[3], 'tool': int(m[4])}
            for m in _CONFIRM_LABEL.finditer(raw)]


def label_confirm_question(confirms):
    lines = [f"slot {c['slot']} (T{c['tool']}): the screen label says {c['live']}, "
             f"Spoolman has {c['recorded']}" for c in confirms]
    return ("Heads-up: " + ('a slot material label differs' if len(lines) == 1
                            else 'some slot material labels differ')
            + " between the printer screen and Spoolman.\n" + "\n".join(lines)
            + "\nNothing is uploaded yet. Print anyway with the loaded spools, or cancel?")


def accept_label_mismatch(job_json, target, confirms):
    """Record a single-use Print anyway for this exact G-code and these listed label differences (#839).

    Mirrors accept_color_mismatch: same per-G-code-SHA binding, consumed
    by the caller before the retry's start runs.
    """
    from ..core import model_job as mj
    job = json.loads(Path(job_json).read_text())
    keys = [mj.label_mismatch_key(c['slot'], c['live'], c['recorded']) for c in confirms]
    prior = mj.accepted_label_mismatches(job, target['gcode_sha256'])  # a later round adds to the earlier ones
    job['label_mismatch_accepted'] = {
        'gcode_sha256': target['gcode_sha256'],
        'slots': sorted(prior | set(keys)),
        'granted_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'via': 'button'}
    mj.write_job(Path(job_json).parent, job)
    return job['label_mismatch_accepted']


def clear_label_acceptance(job_json):
    """Consume the acknowledgement: it never carries over to a later start."""
    from ..core import model_job as mj
    job = json.loads(Path(job_json).read_text())
    if job.pop('label_mismatch_accepted', None) is not None:
        mj.write_job(Path(job_json).parent, job)
