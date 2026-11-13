"""Line-anchored lookups over one G-code text in a single scan (#1153 item 7).

Offline verification asks a G-code about a dozen line-anchored questions: the
first ``; nozzle_diameter = ...`` line, the first ``START_PRINT`` line, whether
any ``END_PRINT`` line exists, where the ``;LAYER_CHANGE`` markers are. Asked
one ``re.search(pattern, text, re.M)`` at a time, each question walks the whole
file, and the configuration block an Orca file appends after its toolpath is at
the very end, so most of them read every byte.

``GcodeLines`` walks the text once. One alternation of the declared line
anchors finds every line that starts with any of them; a question is then
answered by trying its own unchanged pattern only at the candidate lines for
its anchor, in file order. That is the same answer ``re.search`` (or
``re.findall``, ``re.split``) gives, because every match of such a pattern
starts at a line start where its anchor matches, and ``pattern.match(text,
pos)`` matches at ``pos`` exactly as a search reaching ``pos`` would (``^``
and ``\\b`` still look at the character before ``pos``).

An anchor is a literal line prefix (a ``str``) or an ``Anchor``: a regular
expression fragment for patterns that do not start with a fixed literal
(``;\\s*hermes_tool_slots``). An ``Anchor`` joins the scan only as a
lookahead, so it never consumes text and can never hide the next line from
the scan. An anchor that was not declared before the scan, or a literal the
alternation cannot represent safely (empty, or containing a newline), falls
back to a plain whole-text call: slower, never different.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

__all__ = ["Anchor", "GcodeLines"]


@dataclass(frozen=True)
class Anchor:
    """A regex fragment every match of a question starts with, right at a line start."""

    fragment: str


class GcodeLines:
    """One G-code text, scanned once for every declared line anchor."""

    def __init__(self, text, anchors=()):
        self.text = text
        self._declared = list(dict.fromkeys(a for a in anchors if isinstance(a, (str, Anchor))))
        self._positions = None
        self.scans = 0

    @staticmethod
    def _scannable(anchor):
        return isinstance(anchor, Anchor) or (bool(anchor) and "\n" not in anchor)

    def _candidates(self, anchor):
        """Line-start offsets where ``anchor`` matches, or None when it must be answered directly."""
        if self._positions is None:
            if anchor not in self._declared:
                self._declared.append(anchor)
            self._scan()
        return self._positions.get(anchor)

    def _scan(self):
        literals = [a for a in self._declared if isinstance(a, str) and self._scannable(a)]
        fragments = [a for a in self._declared if isinstance(a, Anchor)]
        positions = {a: [] for a in literals + fragments}
        # The literals are one capturing group, longest first, so the engine
        # reports the longest literal the line starts with; the literals the
        # line also starts with are exactly that one's prefixes, worked out
        # once here. An Anchor joins as a zero-width lookahead and is tested
        # at every hit, whichever branch the engine took.
        ordered = sorted(literals, key=len, reverse=True)
        adds = {lit: [positions[other].append for other in literals if lit.startswith(other)] for lit in literals}
        branches = [f"({'|'.join(re.escape(a) for a in ordered)})"] if ordered else []
        branches += [f"(?={a.fragment})" for a in fragments]
        if branches:
            text = self.text
            lookaheads = [(re.compile(a.fragment).match, positions[a].append) for a in fragments]
            alternation = f"(?:{'|'.join(branches)})"
            # "^" under re.M is position 0 or just after a "\n". A pattern led by a literal "\n"
            # lets the engine jump from newline to newline instead of trying "^" at every
            # character (1.1 s to about 0.2 s on a 25 MB G-code, #1392). Matches cannot hide a
            # line: a literal holds no newline and a lookahead consumes nothing.
            first = re.compile(alternation).match(text, 0)
            hits = itertools.chain(
                () if first is None else ((0, first),),
                ((match.start() + 1, match) for match in re.finditer("\n" + alternation, text)))
            for pos, match in hits:
                literal = match.group(1) if ordered else None
                if literal is not None:
                    for add in adds[literal]:
                        add(pos)
                for test, add in lookaheads:
                    if test(text, pos) is not None:
                        add(pos)
        self._positions = positions
        self.scans += 1

    def search(self, pattern, anchor):
        """``re.search(pattern, text, re.M)`` for a pattern whose every match starts where ``anchor`` matches."""
        compiled = re.compile(pattern, re.M)
        candidates = self._candidates(anchor)
        if candidates is None:
            return compiled.search(self.text)
        match_at, text = compiled.match, self.text
        for pos in candidates:
            found = match_at(text, pos)
            if found is not None:
                return found
        return None

    def findall(self, pattern, anchor):
        """``re.findall(pattern, text, re.M)`` for a pattern whose every match starts where ``anchor``
        matches and ends on that same line (so no two matches can overlap)."""
        compiled = re.compile(pattern, re.M)
        candidates = self._candidates(anchor)
        if candidates is None:
            return compiled.findall(self.text)
        match_at, text, groups = compiled.match, self.text, compiled.groups
        found = (match_at(text, pos) for pos in candidates)
        return [m.groups() if groups > 1 else m.group(groups) for m in found if m is not None]

    def config_value(self, key):
        """The first ``; key = value`` line's value, stripped; None when absent."""
        found = self.search(rf"^; {re.escape(key)} = (.+)$", f"; {key} = ")
        return found.group(1).strip() if found else None

    def split(self, prefix, limit):
        """``re.split('^' + re.escape(prefix), text, flags=re.M)``: its first ``limit`` pieces and its length."""
        candidates = self._candidates(prefix)
        if candidates is None:
            pieces = re.split("^" + re.escape(prefix), self.text, flags=re.M)
            return pieces[:limit], len(pieces)
        text, width = self.text, len(prefix)
        starts = [0] + [pos + width for pos in candidates]
        ends = candidates + [len(text)]
        pieces = [text[start:end] for start, end in zip(starts[:limit], ends[:limit])]
        return pieces, len(candidates) + 1
