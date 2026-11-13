"""Native AD5X startup contract shared by finalization and preflight.

ZMod FAQ prescribes M190 S<bed>, M104 S<hotend> T0 before start
for single_extruder_multi_material. T0 here addresses the one heater,
not the first logical filament; subsequent T1 selection is preserved.
"""
import itertools
import re

NUM = r'(?:\d+(?:\.\d*)?|\.\d+)'


def commands(text):
    # One partition per line; slicer files are mostly comments and run to 1M+ lines.
    out = []
    append = out.append
    for line in text.splitlines():
        line = line.partition(';')[0].strip()
        if line:
            append(line)
    return out


def _heater_run_consistent(cmd, bed, hotend):
    """Check immediate declarations without accepting later layer heaters."""
    for line in cmd:
        if not re.match(r'M(?:104|109|140|190)\b', line):
            break
        match = re.fullmatch(r'M(104|109|140|190) S(' + NUM + r')(?: T(\d+))?', line)
        if not match:
            return False
        target = bed if match.group(1) in ('140', '190') else hotend
        if float(match.group(2)) != float(target) or match.group(3) not in (None, '0'):
            return False
    return True


def _benign_preamble(cmd):
    """Only the observed Orca progress and fan-off declarations may precede startup.

    No generic M-code skipping: extra parameters, tool selections, motion,
    extrusion, heater commands and nonzero fan speeds remain forbidden.
    """
    return all(re.fullmatch(r'M73 P0 R\d+|M106 S0|M106 P2 S0', line)
               for line in cmd)


def _command_lines(lines):
    for line in lines:
        line = line.partition(';')[0].strip()
        if line:
            yield line


class _Startup:
    """The START_PRINT command of a G-code and the commands around it, as commands(text) reads them.

    Built without commands(text) for the whole file: that list of every command line cost
    about 1.2 s on a 25 MB G-code, and the rules below only read the commands before
    START_PRINT, START_PRINT itself and the first few after it (#1392). ``starts`` is the
    number of START_PRINT commands; ``before``, ``start`` and ``after()`` exist when it is 1.
    """

    def __init__(self, text):
        self.lines = text.splitlines()
        # A START_PRINT command line contains the text START_PRINT: test that first.
        hits = [i for i, line in enumerate(self.lines)
                if 'START_PRINT' in line and line.partition(';')[0].strip().startswith('START_PRINT ')]
        self.starts = len(hits)
        if self.starts == 1:
            self._line = hits[0]
            self.before = list(_command_lines(self.lines[:self._line]))
            self.start = self.lines[self._line].partition(';')[0].strip()

    def after(self, count=None):
        """The commands after START_PRINT, lazily (the first ``count`` as a list when given)."""
        rest = _command_lines(itertools.islice(self.lines, self._line + 1, None))
        return rest if count is None else list(itertools.islice(rest, count))


def valid(text, bed, hotend):
    return _valid(_Startup(text), bed, hotend)


def _valid(view, bed, hotend):
    if view.starts != 1:
        # commands(text) has fewer than 3 commands, or not exactly one START_PRINT: invalid either way.
        return False
    if len(view.before) + 1 + len(view.after(2)) < 3:
        return False
    start = len(view.before)
    # Allow only the observed benign slicer preamble BEFORE the contiguous
    # header. It cannot split a heater pair from START_PRINT.
    if start >= 2 and view.before[start - 2].startswith('M190 ') and _benign_preamble(view.before[:start - 2]):
        heater_cmd = view.before[start - 2:start]
    elif _benign_preamble(view.before):
        heater_cmd = view.after(2)
    else:
        return False
    if len(heater_cmd) != 2:
        return False
    expected = [('M190', float(bed), None), ('M104', float(hotend), 0)]
    for line, (code, temp, tool) in zip(heater_cmd, expected):
        pattern = code + r' S(' + NUM + r')' + (r' T0' if tool == 0 else '')
        match = re.fullmatch(pattern, line)
        if not match or float(match.group(1)) != temp:
            return False
    for key, target in [('BED_TEMP', bed), ('EXTRUDER_TEMP', hotend)]:
        values = re.findall(r'\b' + key + r'=(' + NUM + r')(?=\s|$)', view.start)
        if len(values) != 1 or float(values[0]) != float(target):
            return False
    # A valid leading pair must never mask conflicting post-start declarations.
    return _heater_run_consistent(view.after(), bed, hotend)


def normalize(text):
    """Insert missing heaters adjacent to START_PRINT after a strict preamble."""
    view = _Startup(text)
    if not view.starts:
        return text
    if view.starts != 1:
        raise RuntimeError('native startup requires exactly one START_PRINT')
    temps = []
    for key in ('BED_TEMP', 'EXTRUDER_TEMP'):
        values = re.findall(r'\b' + key + r'=(' + NUM + r')(?=\s|$)', view.start)
        if len(values) != 1 or float(values[0]) <= 0:
            raise RuntimeError('native startup missing or conflicting ' + key)
        temps.append(values[0])
    whole = itertools.chain(view.before, [view.start], view.after())
    if not _heater_run_consistent(whole, *temps) or not _heater_run_consistent(view.after(), *temps):
        raise RuntimeError('conflicting or ambiguous native startup heater command')
    if _valid(view, *temps):
        return text
    # Never bless a misplaced/partial existing header by adding another pair.
    following = view.after(1)
    if not _benign_preamble(view.before) or (following and re.match(r'M(?:104|109|140|190)\b', following[0])):
        raise RuntimeError('native startup requires contiguous recognized header order')
    lines = text.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines)
                 if line.split(';', 1)[0].strip().startswith('START_PRINT '))
    lines[index:index] = [f'M190 S{temps[0]}\n', f'M104 S{temps[1]} T0\n']
    result = ''.join(lines)
    if not valid(result, *temps):
        raise RuntimeError('native startup normalization failed')
    return result
