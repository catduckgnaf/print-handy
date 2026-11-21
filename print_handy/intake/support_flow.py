"""The supports decision: whether a model needs supports, the question and the re-slice labels.

Moved verbatim from ``print_handy.core.prepared_intake`` (#1323). The old names are still
importable from there for one release.
"""


RESLICE_SUPPORTS_LABEL = 'Re-slice with supports'
PRINT_NO_SUPPORTS_LABEL = 'Print without supports'
EXTEND_LABEL = 'Extend underside to plate'
SINK_LABEL = 'Flatten (sink) to plate'
REORIENT_LABEL = 'Reorient (lay flat)'


def repair_actions(clearance, *, turned_job, reorient_ok=False):
    """The deterministic geometry repairs a low-clearance report allows, with their lines.

    One rule for the Ready menu and the post-slice gate (no AI involved,
    #1373): extend only where the underside faces the plate, extend and sink
    never on a turned job (they edit the authored mesh along its own Z),
    reorient only where the caller says its turn would reach the slice.
    Returns (actions, lines): actions are {'kind', 'label', 'finding'}.
    """
    from ..core import support_clearance

    actions, lines = [], []
    if not (clearance and support_clearance.risky(clearance)):
        return actions, lines
    for f in clearance.get('findings') or []:
        for fix in f.get('fixes') or []:
            if fix.get('fix') == 'extend_underside' and f.get('over') == 'plate' \
                    and not turned_job and not any(a['kind'] == 'extend' for a in actions):
                actions.append({'kind': 'extend', 'label': EXTEND_LABEL, 'finding': f})
                if fix.get('added_volume_mm3') is not None:
                    lines.append(f"- Extend underside to plate adds about "
                                 f"{fix['added_volume_mm3']:.0f} mm3 of material below the underside.")
            elif fix.get('fix') == 'sink' and not turned_job \
                    and not any(a['kind'] == 'sink' for a in actions):
                actions.append({'kind': 'sink', 'label': SINK_LABEL, 'finding': f})
                if fix.get('removed_volume_mm3') is not None:
                    lines.append(f"- Flatten (sink) to plate removes about "
                                 f"{fix['removed_volume_mm3']:.0f} mm3 that would end up below the plate.")
            elif fix.get('fix') == 'reorient' and reorient_ok \
                    and not any(a['kind'] == 'reorient' for a in actions):
                actions.append({'kind': 'reorient', 'label': REORIENT_LABEL, 'finding': f})
    return actions, lines


def gap_repairs(actions):
    """The extend/sink actions: the ones that close a measured low gap."""
    return [a for a in actions if a['kind'] in ('extend', 'sink')]


def low_gap_text(actions):
    """One line naming the measured gap and the repair that closes it, or ''."""
    repairs = gap_repairs(actions)
    if not repairs:
        return ''
    f = repairs[0]['finding']
    gap = (f.get('clearance_mm') or {}).get('min')
    gap_text = f"{gap:.3f}".rstrip('0').rstrip('.') + ' mm' if isinstance(gap, (int, float)) else 'a low'
    return (f"Measured low gap: an underside sits {gap_text} above the plate over "
            f"{f.get('area_mm2', 0):.0f} mm2. Supports cannot reach it; only a geometry repair "
            "closes it (a repair works on a copy and every check runs again).")


def supports_recommended(finding, clearance):
    """True unless the only evidence is a low clearance supports cannot reach.

    A slicer floating-region finding is something supports fix; a risky
    clearance report with no such finding is a gap too low for supports,
    so the with-supports option is labeled not recommended and is not
    offered first.
    """
    from ..core import support_clearance

    return bool(finding) or not support_clearance.risky(clearance)


def with_supports_line(reason, estimate, finding, clearance, gap_repair=False):
    """The with-supports line of the Ready question.

    Supports are recommended only when they can help. A risky clearance with
    no slicer finding means the only evidence is a gap supports cannot reach,
    so recommending them would contradict the reason given.
    """
    if gap_repair and finding:
        return (f"- Slice and print with supports ({reason or 'floating regions found'}; supports help "
                f"those regions only, they cannot close the measured gap): {estimate}")
    if not supports_recommended(finding, clearance) or gap_repair:
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
