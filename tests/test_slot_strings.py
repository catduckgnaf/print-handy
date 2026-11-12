"""User-visible "slot N" text never prints a zero-based index (#1332).

#1020 turned on lookalike numbers in different bases: a 1-based IFS
channel beside a 0-based lane index. #1025 added ``slot_display`` and the
one conversion pair (``lane_index0`` / ``slot_from_lane_index0``) and
left the older "slot N" strings for a sweep. The sweep (#1332) found
every older string already interpolating a 1-based value, moved the
listener posts that printed a bare number with no base onto
``slot_display``, and kept the conversational spool and intake text as
is ("Slot 2 (Silk PLA)" already reads in the user's base, and
``slot_display`` names the IFS, which is wrong on other families).

This guard keeps it that way: an f-string that says "slot {x}" must not
interpolate a value named like a zero-based index unless it converts it.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLOT_WORD = re.compile(r"\b[Ss]lot #?$")
# Names that carry a zero-based index in this code base.
ZERO_BASED = {"lane", "lane_index", "lane_index0", "index", "index0", "idx", "i",
              "tool", "tool_index", "ti", "t_index", "channel_index", "extruder"}
CONVERTERS = {"slot_from_lane_index0", "slot_display"}


def _names(node):
    """Names the value is made of. A subscript key is not the value:
    ``mapping[extruder]`` is the slot looked up by an extruder."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr} | _names(node.value)
    if isinstance(node, ast.Subscript):
        return _names(node.value)
    found = set()
    for child in ast.iter_child_nodes(node):
        found |= _names(child)
    return found


def _converted(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Add) and \
                any(isinstance(side, ast.Constant) and side.value == 1 for side in (sub.left, sub.right)):
            return True
        if isinstance(sub, ast.Call) and (_names(sub.func) & CONVERTERS):
            return True
    return False


def offenders():
    found = []
    for path in sorted((ROOT / "print_handy").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            parts = node.values
            for before, value in zip(parts, parts[1:]):
                if not (isinstance(before, ast.Constant) and isinstance(before.value, str)
                        and isinstance(value, ast.FormattedValue)):
                    continue
                if not SLOT_WORD.search(before.value):
                    continue
                expr = value.value
                if _names(expr) & ZERO_BASED and not _converted(expr):
                    rel = path.relative_to(ROOT).as_posix()
                    found.append(f"{rel}:{node.lineno}: slot {{{ast.unparse(expr)}}}")
    return found


def test_no_slot_text_interpolates_a_zero_based_index():
    assert offenders() == []


def test_the_guard_catches_a_zero_based_lane():
    # The guard itself: the #1020 shape is flagged, the converted form is not.
    sample = ast.parse('a = f"slot {lane}"\nb = f"slot {index + 1}"\nc = f"slot {slot}"\n'
                       'd = f"slot {mapping[extruder]}"\ne = f"slot {row.lane}"')
    flagged = []
    for node in ast.walk(sample):
        if isinstance(node, ast.JoinedStr):
            expr = node.values[1].value
            flagged.append(bool(_names(expr) & ZERO_BASED and not _converted(expr)))
    assert flagged == [True, False, False, False, True]


def test_the_listener_posts_name_the_slot_base():
    from print_handy.hermes import moonraker_event_listener as mel

    assert "on slot 3 (IFS channel 3)" in mel.native_error_text("Ghost.gcode", ("E0102", 3))
