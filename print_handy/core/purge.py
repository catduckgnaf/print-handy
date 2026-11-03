"""Where a ZMod printer's color-change purge goes, decided once per printer (#832).

On a ZMod AD5X the printer often purges into its own chute, so a slicer prime
tower is a second, wasted purge and a footprint that makes models miss the
one-plate fit. This module owns the whole decision:

* ``refresh`` reads the printer (read-only, short timeouts, never raises) for
  ``use_trash_on_print`` (ZMod's purge mode), ``allowed_tool_count`` and the
  enabled ZMod plugins, and caches the answer in the printer's state folder.
* ``resolve`` turns the settings ([purge], [zmod]) plus that detection into one
  ``PurgePlan`` that the fit check, the Orca profile, the slice recipe and the
  launch guard all read, so they cannot disagree.
* ``require_launch_safe`` is the start-time check: a towerless G-code on a
  printer that positively reports nopoop (``use_trash_on_print = 0``) would
  purge nowhere and mix colors, so that one case is refused.

Nothing here writes to a printer. A printer that is not a ZMod printer, or whose
mode cannot be read, keeps today's behavior: a prime tower, as before.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass

from .. import printers, settings

CACHE_NAME = 'zmod_detection.json'
# ZMod's use_trash_on_print values (printer_state.PURGE_MODES) by plan mode.
_VALUE_MODE = {0: 'prime_tower', 1: 'printer_chute', 2: 'slicer_chute'}
MODE_VALUE = {'prime_tower': 0, 'printer_chute': 1, 'slicer_chute': 2}
# Plugins that change color-change behavior and cannot run together (QuickSwap's README).
CONFLICTS = (('quickswap', 'bambufy'), ('quickswap', 'lesswaste'))
IFS_JACKER = 'ifs_jacker'
DETECT_TIMEOUT_S = 3

# Fallback when plugins.cfg cannot be read: objects Klipper reports for each plugin.
_EXACT_OBJECTS = {
    'quickswap': {'_qs_a_change_filament', 'qs_ifs_calibration_speed'},
    IFS_JACKER: {'ifs_jacker', 'ifs_jacker_check_presence'},
}
_SUBSTRING_OBJECTS = {'bambufy': 'bambufy', 'lesswaste': 'lesswaste', 'nopoop': 'nopoop'}
_INCLUDE = re.compile(r'^\s*\[include\s+plugins/([^/\s\]]+)/', re.M)


@dataclass(frozen=True)
class PurgePlan:
    """The effective purge decision for one printer.

    mode: ``printer_chute``, ``prime_tower``, ``slicer_chute`` or ``unknown``
    (not a ZMod printer, or the mode could not be read: the long-standing
    prime-tower slice). prime_tower: whether the slice carries a tower and the
    fit check reserves room for one. refusal: a plain message when this plan
    cannot be sliced, else None.
    """
    mode: str
    prime_tower: bool
    flush_multiplier: float | None
    plugins: tuple
    source: str
    zmod: bool = False
    refusal: str | None = None

    @property
    def towerless(self):
        return not self.prime_tower

    def recipe(self):
        """The fields that change the G-code, for the slice recipe hash."""
        if not self.zmod:
            return None
        return {'mode': self.mode, 'prime_tower': self.prime_tower, 'flush_multiplier': self.flush_multiplier}

    def as_dict(self):
        return {'mode': self.mode, 'prime_tower': self.prime_tower, 'flush_multiplier': self.flush_multiplier,
                'plugins': list(self.plugins), 'source': self.source}


# ---------------------------------------------------------------- parsing

def parse_plugins_cfg(text):
    """Enabled plugin names from ZMod's mod_data/plugins.cfg ``[include plugins/<name>/...]`` lines.

    Commented-out includes (``#[include ...]``) do not match: the pattern needs the
    bracket at the start of the line.
    """
    seen = []
    for name in _INCLUDE.findall(text or ''):
        name = name.lower()
        if name not in seen:
            seen.append(name)
    return seen


def plugins_from_objects(objects):
    """Known plugins whose macros or objects appear in Moonraker's /printer/objects/list."""
    names = {str(o).lower().replace('gcode_macro ', '', 1).strip() for o in objects or ()}
    found = []
    for plugin, exact in _EXACT_OBJECTS.items():
        if names & exact:
            found.append(plugin)
    for plugin, needle in _SUBSTRING_OBJECTS.items():
        if any(needle in name for name in names):
            found.append(plugin)
    return found


def conflicts(plugins):
    """Plain notes for plugins that should not be enabled together (a note only, never a refusal)."""
    enabled = set(plugins or ())
    return [f'{a} and {b} are both enabled; they are not compatible with each other'
            for a, b in CONFLICTS if a in enabled and b in enabled]


# ---------------------------------------------------------------- transport (read-only)

def _fetch(url, timeout):
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode('utf-8', 'replace')


def _query(base, path, timeout):
    return json.loads(_fetch(base + path, timeout))


# ---------------------------------------------------------------- cache

def printer_id_for(state=None, data=None):
    """The roster id a hardware record belongs to: its own id, else its Moonraker URL's entry, else the default."""
    state = state or {}
    if isinstance(state.get('printer_id'), str) and state['printer_id']:
        return state['printer_id']
    url = str(state.get('moonraker_url') or '').rstrip('/')
    try:
        if url:
            for entry in settings.printers(data):
                if str(entry.get('moonraker_url') or '').rstrip('/') == url:
                    return entry['id']
        return settings.default_printer_id(data)
    except Exception:
        return None


def _cache_path(printer_id, data=None):
    return settings.printer_state_dir(printer_id, data) / CACHE_NAME


def read_cache(printer_id, data=None):
    """The cached detection for a printer, or {} when none was ever taken."""
    try:
        record = json.loads(_cache_path(printer_id, data).read_text())
        return record if isinstance(record, dict) else {}
    except Exception:
        return {}


def _write_cache(printer_id, record, data=None):
    try:
        path = _cache_path(printer_id, data)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
        tmp.replace(path)
    except OSError:
        pass  # a cache that cannot be written only costs the next call a refresh


def refresh(state, adapter=None, printer_id=None, data=None, timeout=DETECT_TIMEOUT_S):
    """Read the printer once, read-only, and cache what it says. Never raises, never writes to the printer.

    Returns the detection record, or the previous one when nothing could be read
    (``record['error']`` then says why). A printer that is not a ZMod printer
    returns {} and caches nothing.
    """
    adapter = adapter or printers.current()
    if not getattr(adapter, 'has_native_screen', False):
        return {}
    base = str((state or {}).get('moonraker_url') or '').rstrip('/')
    printer_id = printer_id or printer_id_for(state, data)
    previous = read_cache(printer_id, data) if printer_id else {}
    if not base:
        return {**previous, 'error': 'no Moonraker URL is configured'}
    record = {'checked_at': time.time()}
    try:
        status = _query(base, '/printer/objects/query?save_variables', timeout)['result']['status']
    except Exception as exc:
        return {**previous, 'error': f'could not read save_variables: {exc}'}
    variables = (status.get('save_variables') or {}).get('variables') or {}
    from .printer_state import purge_mode
    record['use_trash_on_print'] = purge_mode(variables)
    count = variables.get('allowed_tool_count')
    record['allowed_tool_count'] = count if type(count) is int else None
    plugins, how = None, None
    try:
        plugins = parse_plugins_cfg(_fetch(base + '/server/files/config/mod_data/plugins.cfg', timeout))
        how = 'plugins.cfg'
    except Exception:
        try:
            plugins = plugins_from_objects(_query(base, '/printer/objects/list', timeout)['result']['objects'])
            how = 'objects list'
        except Exception:
            pass
    record['plugins'], record['plugins_source'] = plugins, how
    if printer_id:
        _write_cache(printer_id, record, data)
    return record


def refresh_best_effort(state, adapter=None, printer_id=None, data=None):
    """``refresh`` for intake: any failure leaves today's behavior in place."""
    try:
        return refresh(state, adapter, printer_id, data)
    except Exception:
        return {}


# ---------------------------------------------------------------- the decision

def _refusal_for(mode, prime_tower):
    if mode == 'slicer_chute':
        return ('ZMod USE_TRASH_ON_PRINT=2 (slicer-controlled poop) needs ZMod\'s own Orca profile '
                'change-filament G-code, which Print Handy does not slice with. Set '
                'SAVE_ZMOD_DATA USE_TRASH_ON_PRINT=1 (printer chute) or 0 (Nopoop, prime tower), '
                'or set purge.mode, then slice again. Nothing was sliced.')
    if mode == 'prime_tower' and not prime_tower:
        return ('The slicer is set to purge into the prime tower (purge.mode prime_tower or '
                'USE_TRASH_ON_PRINT=0) but purge.prime_tower is off, so nothing would purge and '
                'colors would mix. Turn the tower on or choose another purge.mode. Nothing was sliced.')
    return None


def resolve(printer_id=None, data=None, *, state=None, adapter=None, live=False):
    """The one effective purge plan for a printer.

    ``live=True`` (slicing) reads use_trash_on_print from the printer now; an
    unreadable printer is ``unknown`` and keeps the prime-tower slice. The default
    uses the cached detection and never touches the network (fit checks, recipes).
    An explicit ``purge.mode`` in settings is honored whether or not detection
    works. Not a ZMod printer: ``zmod=False`` and today's behavior.
    """
    data = data or settings.current()
    pid = printer_id or printer_id_for(state, data)
    if adapter is None:
        try:
            if pid and pid != settings.default_printer_id(data):
                adapter = printers.get(settings.by_id(pid, data)['model'], settings.view_for(pid, data))
            else:
                adapter = printers.current()
        except Exception:
            adapter = printers.current()
    purge_cfg = data.get('purge') or {}
    multiplier = purge_cfg.get('flush_multiplier')
    multiplier = float(multiplier) if multiplier is not None else None
    if not getattr(adapter, 'has_native_screen', False):
        return PurgePlan('unknown', True, None, (), 'not a ZMod printer; prime tower as before')
    cache = read_cache(pid, data) if pid else {}
    configured = (data.get('zmod') or {}).get('plugins', 'auto')
    plugins = (tuple(str(p).lower() for p in configured) if isinstance(configured, list)
               else tuple(cache.get('plugins') or ()))
    value = None
    if live:
        from .printer_state import read_purge_mode
        value = read_purge_mode(state)
    else:
        value = cache.get('use_trash_on_print')
    mode_cfg = purge_cfg.get('mode', 'auto')
    if mode_cfg != 'auto':
        mode, source = mode_cfg, f'purge.mode = {mode_cfg} (settings)'
    elif value in _VALUE_MODE:
        mode, source = _VALUE_MODE[value], f'printer reports use_trash_on_print={value}'
    else:
        mode, source = 'unknown', 'printer purge mode not read; keeping the prime tower slice'
    tower_cfg = purge_cfg.get('prime_tower', 'auto')
    if tower_cfg == 'on':
        tower = True
    elif tower_cfg == 'off':
        tower = False
    else:
        tower = mode != 'printer_chute'
    return PurgePlan(mode, tower, multiplier, plugins, source, zmod=True, refusal=_refusal_for(mode, tower))


def tower_mm(nozzle_dia, plan=None):
    """The prime-tower width a fit or placement decision should reserve: 0 when the plan has no tower."""
    from .color_geometry import effective_tower_mm
    if plan is not None and plan.zmod and plan.towerless:
        return 0.0
    return effective_tower_mm(nozzle_dia)


# ---------------------------------------------------------------- guards

_TOWERLESS_GCODE = re.compile(r'^; enable_prime_tower = 0\s*$', re.M)


def gcode_is_towerless(text):
    """True when the sliced G-code's recorded Orca config turned the prime tower off."""
    return bool(_TOWERLESS_GCODE.search(text or ''))


def require_launch_safe(gcode_text, state, say=print):
    """Refuse (SystemExit) a towerless multicolor G-code the printer would not purge.

    Only a printer that positively reports use_trash_on_print = 0 (nopoop: the
    slicer must purge into a tower) is refused. Mode 1 passes silently; an
    unreadable or other mode passes with an informational line. Read-only.
    """
    if not gcode_is_towerless(gcode_text):
        return
    from .printer_state import read_purge_mode
    live = read_purge_mode(state)
    if live == 0:
        raise SystemExit(
            'REFUSED: this G-code was sliced without a prime tower, for the printer to purge into its '
            'chute (use_trash_on_print=1), but the printer reports use_trash_on_print=0 (Nopoop), where '
            'only a prime tower purges. Nothing would purge and colors would mix. Set USE_TRASH_ON_PRINT=1 '
            'on the printer or slice again. Nothing uploaded or started.')
    if live != 1:
        say('NOTE: this G-code has no prime tower (the printer is expected to purge into its chute) and '
            'the printer\'s use_trash_on_print could not be confirmed as 1; the print went ahead.')


def require_jacker_capacity(manifest, adapter, state=None, data=None):
    """Refuse a job that needs more colors than the 4 slots Print Handy models, on a printer with an IFS Jacker.

    The IFS Jacker joins two IFS units; 8-slot support (slot questions, the hardware
    record, Spoolman slots, ZMod's color_limit) is a follow-up. Only a detected
    Jacker changes anything; every other printer keeps its behavior.
    """
    if not getattr(adapter, 'has_native_screen', False):
        return
    pid = printer_id_for(state, data)
    cache = read_cache(pid, data) if pid else {}
    if IFS_JACKER not in (cache.get('plugins') or ()):
        return
    from .source_colors import distinct_source_colours
    needed = len(distinct_source_colours(manifest))
    modeled = int(getattr(adapter, 'slot_count', 0) or 4)
    if needed > modeled:
        raise printers.MultiColorUnsupported(
            f'This printer has an IFS Jacker, but Print Handy models {modeled} IFS slots and this source '
            f'uses {needed} colors. Support for IFS Jacker\'s extra slots is a follow-up. Reduce the source '
            f'to {modeled} colors. Nothing was sliced.')


# ---------------------------------------------------------------- reporting

def status_row(printer_id, data=None, adapter=None):
    """One printer's purge facts for the doctor and the Web UI (cached detection only), or None."""
    try:
        plan = resolve(printer_id, data, adapter=adapter)
    except Exception:
        return None
    if not plan.zmod:
        return None
    cache = read_cache(printer_id, data)
    row = {'mode': plan.mode, 'prime_tower': plan.prime_tower, 'flush_multiplier': plan.flush_multiplier,
           'source': plan.source, 'plugins': list(plan.plugins),
           'use_trash_on_print': cache.get('use_trash_on_print'),
           'allowed_tool_count': cache.get('allowed_tool_count'),
           'plugins_source': cache.get('plugins_source'),
           'checked_at': cache.get('checked_at'),
           'notes': conflicts(plan.plugins)}
    if not cache:
        row['notes'].append('purge settings were not read from the printer yet; keeping the prime tower slice')
    elif cache.get('plugins') is None:
        row['notes'].append('the enabled plugins could not be read')
    if IFS_JACKER in plan.plugins:
        row['notes'].append('IFS Jacker detected; Print Handy models 4 IFS slots (more is a follow-up)')
    if plan.refusal:
        row['notes'].append(plan.refusal)
    return row


def describe(row):
    """A one-line plain summary of status_row()."""
    tower = 'no prime tower' if not row['prime_tower'] else 'prime tower'
    plugins = ', '.join(row['plugins']) or 'none'
    text = f"{row['mode'].replace('_', ' ')}, {tower} ({row['source']}); plugins: {plugins}"
    if row['flush_multiplier'] is not None:
        text += f"; flush multiplier {row['flush_multiplier']:g}"
    if row['notes']:
        text += '; ' + '; '.join(row['notes'])
    return text
