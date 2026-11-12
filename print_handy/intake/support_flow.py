"""The supports decision: whether a model needs supports, the question and the re-slice labels.

Moved verbatim from ``print_handy.core.prepared_intake`` (#1323). The old names are still
importable from there for one release.
"""


RESLICE_SUPPORTS_LABEL = 'Re-slice with supports'
PRINT_NO_SUPPORTS_LABEL = 'Print without supports'


def with_supports_line(reason, estimate, finding, clearance):
    """The with-supports line of the Ready question.

    Supports are recommended only when they can help. A risky clearance with
    no slicer finding means the only evidence is a gap supports cannot reach,
    so recommending them would contradict the reason given.
    """
    from ..core import support_clearance

    if not finding and support_clearance.risky(clearance):
        return f"- Slice and print with supports (not recommended: {reason}): {estimate}"
    return f"- Slice and print with supports (recommended: {reason or 'floating regions found'}): {estimate}"


def supports_decision(flagged, finding, clearance):
    """(show, reason, inputs) for the supports question (#1020 F).

    ``show`` is True only on evidence: a slicer floating-region finding,
    a risky clearance report, or the slice's own flagged bit (which the
    background slice derives from those same two sources). A flat model
    — no finding, not flagged, clearance clean — never shows the menu.

    ``reason`` names the evidence **with its numbers** (how many
    floating regions; the smallest clearance in mm), because "supports
    are recommended" without the measurement is not a decision the
    operator can judge. ``inputs`` is the raw decision record: it is
    logged at composition and stored on the intake record with the
    clearance report and the finding, so a shown (or hidden) menu can
    be audited later.
    """
    from ..core import support_clearance

    risky = support_clearance.risky(clearance)
    reasons = []
    if finding:
        objects = finding.get('objects') or []
        texts = finding.get('texts') or []
        count = len(objects) or len(texts)
        if count == 1:
            reasons.append("the slicer found 1 floating region that needs support")
        elif count:
            reasons.append(f"the slicer found {count} floating regions that need support")
        else:
            reasons.append("the slicer found floating regions that need support")
    if risky:
        gaps = []
        for f in (clearance or {}).get('findings') or []:
            window = f.get('clearance_mm') or {}
            if isinstance(window.get('min'), (int, float)):
                gaps.append(window['min'])
        if gaps:
            reasons.append(f"an underside clears what is below it by only "
                           f"{support_clearance._fmt(min(gaps))} mm — too low for supports to reach")
        else:
            reasons.append("a low-clearance underside leaves no room for supports to reach")
    if not reasons and flagged:
        reasons.append("the slice was flagged for supports")
    show = bool(flagged or finding or risky)
    inputs = {'flagged': bool(flagged), 'finding': bool(finding),
              'clearance_risky': bool(risky),
              'clearance_codes': support_clearance.codes(clearance) if clearance else []}
    return show, '; '.join(reasons), inputs
