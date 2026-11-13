"""native_startup reads only START_PRINT and its neighbors (#1392), with the old answers.

The old valid/normalize built commands(text) for the whole file (about 1.2 s on a
25 MB G-code, twice per slice). The frozen copies below are the old code; the new
code must agree with them on every input, including the exception messages.
"""

import random
import re

from print_handy.printers.flashforge.zmod import native_startup as ns

NUM = ns.NUM


def _old_valid(text, bed, hotend):
    cmd = ns.commands(text)
    if len(cmd) < 3:
        return False
    starts = [i for i, line in enumerate(cmd) if line.startswith('START_PRINT ')]
    if len(starts) != 1:
        return False
    start = starts[0]
    if start >= 2 and cmd[start - 2].startswith('M190 ') and ns._benign_preamble(cmd[:start - 2]):
        heater_cmd = cmd[start - 2:start]
    elif ns._benign_preamble(cmd[:start]):
        heater_cmd = cmd[start + 1:start + 3]
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
        values = re.findall(r'\b' + key + r'=(' + NUM + r')(?=\s|$)', cmd[start])
        if len(values) != 1 or float(values[0]) != float(target):
            return False
    return ns._heater_run_consistent(cmd[start + 1:], bed, hotend)


def _old_normalize(text):
    cmd = ns.commands(text)
    starts = [i for i, line in enumerate(cmd) if line.startswith('START_PRINT ')]
    if not starts:
        return text
    if len(starts) != 1:
        raise RuntimeError('native startup requires exactly one START_PRINT')
    start = starts[0]
    temps = []
    for key in ('BED_TEMP', 'EXTRUDER_TEMP'):
        values = re.findall(r'\b' + key + r'=(' + NUM + r')(?=\s|$)', cmd[start])
        if len(values) != 1 or float(values[0]) <= 0:
            raise RuntimeError('native startup missing or conflicting ' + key)
        temps.append(values[0])
    if not ns._heater_run_consistent(cmd, *temps) or not ns._heater_run_consistent(cmd[start + 1:], *temps):
        raise RuntimeError('conflicting or ambiguous native startup heater command')
    if _old_valid(text, *temps):
        return text
    if not ns._benign_preamble(cmd[:start]) or (len(cmd) > start + 1 and re.match(r'M(?:104|109|140|190)\b', cmd[start + 1])):
        raise RuntimeError('native startup requires contiguous recognized header order')
    lines = text.splitlines(keepends=True)
    index = next(i for i, line in enumerate(lines)
                 if line.split(';', 1)[0].strip().startswith('START_PRINT '))
    lines[index:index] = [f'M190 S{temps[0]}\n', f'M104 S{temps[1]} T0\n']
    result = ''.join(lines)
    if not _old_valid(result, *temps):
        raise RuntimeError('native startup normalization failed')
    return result


LINES = ["M73 P0 R10", "M106 S0", "M106 P2 S0", "M190 S60", "M104 S220 T0", "M104 S220", "M140 S60",
         "M109 S220", "M104 S220 T1", "M190 S70", "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220",
         "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60", "START_PRINT BED_TEMP=60", "START_PRINT",
         "START_PRINT BED_TEMP=0 EXTRUDER_TEMP=220", "  START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220 ; c",
         "; START_PRINT BED_TEMP=60 EXTRUDER_TEMP=220", "START_PRINTX BED_TEMP=60", "G28", "T1", "G1 X1 Y1",
         "; comment", "", "   ", "  M190 S60 ; c", "M104 S220 T0;c", "M190 S60.0", "M190 S.5"]
SEPARATORS = ["\n", "\n", "\n", "\r\n", "\r", "\x0b"]


def _cases(count=3000, seed=1392):
    rng = random.Random(seed)
    heaters = [[], ["M190 S60", "M104 S220 T0"], ["M190 S70", "M104 S220 T0"], ["M104 S220 T0", "M190 S60"],
               ["M190 S60"], ["M190 S60", "M104 S220"], ["M140 S60", "M109 S220"]]
    starts = [line for line in LINES if "START_PRINT" in line]
    for _ in range(count):
        if rng.random() < 0.5:
            n = rng.randint(0, 9)
            lines = [rng.choice(LINES) for _ in range(n)]
        else:  # the shape a slicer writes, with one part varied at a time
            lines = (rng.sample(["M73 P0 R10", "M106 S0", "M106 P2 S0", "; c"], rng.randint(0, 2))
                     + rng.choice(heaters) + [rng.choice(starts)] + rng.choice(heaters)
                     + rng.sample(["T1", "G1 X1 Y1", "M104 S0", "", "; c"], rng.randint(0, 3)))
        yield "".join(line + rng.choice(SEPARATORS) for line in lines)
    canonical = "M73 P0 R10\nM106 S0\nM190 S60\nM104 S220 T0\nSTART_PRINT BED_TEMP=60 EXTRUDER_TEMP=220\nT1\n"
    yield canonical
    yield canonical.replace("M190 S60\nM104 S220 T0\n", "")
    yield canonical + "G1 X1 Y1 E1\n" * 2000 + "M104 S0\n"


def _outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except RuntimeError as exc:
        return ("error", str(exc))


def test_valid_and_normalize_give_the_old_answers():
    checked = 0
    for text in _cases():
        for bed, hotend in ((60, 220), (70, 220), ("60", "220")):
            assert ns.valid(text, bed, hotend) == _old_valid(text, bed, hotend), repr(text)
        assert _outcome(ns.normalize, text) == _outcome(_old_normalize, text), repr(text)
        checked += 1
    assert checked > 3000


def test_the_fuzz_reaches_every_outcome():
    """The cases above are only worth something if they hit valid, normalized and refused files."""
    outcomes = {_outcome(_old_normalize, text)[0] for text in _cases()}
    valid = sum(_old_valid(text, 60, 220) for text in _cases())
    changed = sum(1 for text in _cases() if _outcome(_old_normalize, text) not in (("ok", text),)
                  and _outcome(_old_normalize, text)[0] == "ok")
    assert outcomes == {"ok", "error"}
    assert valid > 50 and changed > 50, (valid, changed)
