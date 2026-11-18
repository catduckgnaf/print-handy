"""Filament color swatches for Web UI pages.

Spoolman stores a filament's color as bare ``RRGGBB`` (no ``#``), and the
slice records use ``#RRGGBB``. A bare code dropped into a CSS
``background`` is invalid, so the browser paints nothing and the swatch
looks empty. Every page that shows a filament color goes through
:func:`swatch` instead, which reads any hex form the rest of Print Handy
reads and always writes a valid ``#RRGGBB``. A value that is not a hex
color (a name, empty, unknown) gets no swatch at all rather than a
misleading empty one.
"""
from __future__ import annotations

import html

from print_handy.core import filament_color


def css_hex(value):
    """``#RRGGBB`` for any hex form (first entry of a list), else None."""
    hex_value = filament_color.first_hex(value)
    return f"#{hex_value}" if hex_value else None


def swatch(value, *, size="small", empty=False):
    """A round color swatch for ``value``, or "" when it is not a color.

    ``size`` is ``"small"`` (inline, beside text) or ``"large"`` (the
    dashboard slot discs). ``empty=True`` draws the dashed empty-slot disc
    instead of returning "" when there is no color. The swatch is
    decorative (``aria-hidden``): the color name or code always sits
    beside it as text.
    """
    color = css_hex(value)
    kind = "disc small" if size == "small" else "disc"
    if color is None:
        if not empty:
            return ""
        return f'<span class="{kind} empty" aria-hidden="true"></span>'
    return (f'<span class="{kind}" style="background: {html.escape(color)}" '
            f'data-color="{html.escape(color)}" aria-hidden="true"></span>')


def labeled(value, text):
    """The swatch for ``value`` followed by ``text`` (escaped), kept on one line."""
    mark = swatch(value)
    label = html.escape(str(text))
    if not mark:
        return label
    return f'<span class="swatch-label">{mark}{label}</span>'


__all__ = ["css_hex", "labeled", "swatch"]
