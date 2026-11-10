"""The Wrapped static share export (#981, sharing phase).

An operator presses one button on the Wrapped page and gets a
self-contained bundle folder under the wrapped outputs
(``<records vault>/wrapped/exports/<folder>/``) that any free static
host (GitHub Pages, Cloudflare Pages, any static file host) can serve
unchanged: Print Handy uploads nothing, holds no operator tokens, and
performs no deploys — hosting is the operator's own free account.

The bundle is the SCROLLING PAGE (catduck's verdict, #981 comment
6702782262), in the same section order as the Web UI Wrapped page:
hero, by the numbers, colors/materials, time patterns, repeats,
personality, pasta/failures/stops, commentary, and the finale share
card. ``index.html`` is standalone (inline CSS, no external assets, no
Web UI server), rendered from the same
:func:`print_handy.core.print_stats.build_wrapped` payload as the Web
UI page. Beside it ride the summary share card as PNGs (Pillow, a
runtime dependency, renders them — the step-1 share card fields:
prints, hours, kg, success rate, top color, archetype), a
``manifest.json`` describing the export, and — when the year's
compiled ``wrapped-<year>.mp4`` already exists in the wrapped
outputs — the video file itself, embedded in the page with a
``<video>`` element in its own section.

What a bundle contains is chosen before the export and shown on the
Share panel: the year's numbers (prints, hours, kilograms, success rate,
top color and material, busiest month and hour, outcome counts, the
personality), the commentary lines, and, each one optional, the print
titles that appear on the page and in the commentary, the printer name,
and the compiled video. The video is camera footage (frames from the
printer's camera), so it is off unless asked for. A title is never a file
path: a title that is one is reduced to the file's base name. Leaving
titles or printer names out also drops every commentary line that
mentions them.

Hygiene: the renderers read only the known wrapped payload fields
(``year``, ``headline``, ``highlights``, ``stats``, ``script``,
``archetype``), so nothing else that rides along in a payload dict —
settings, tokens, paths — can reach a bundle file.

Visibility modes:

- ``public``: the plain bundle, in a ``wrapped-<year>`` folder.
- ``link-only``: the bundle under an unguessable URL-safe slug folder
  (``secrets.token_urlsafe``), a ``noindex`` meta tag on the page, and
  a ``robots.txt`` disallowing everything. This is obscurity, not
  privacy — anyone with the link can open it — and the manifest, the
  share panel and the docs all say so.
- ``private``: NOT AVAILABLE in this phase. The StatiCrypt pattern
  needs AES-GCM, and the rule is AES-GCM only through an established
  library: the repo's dependency set carries none (no
  ``cryptography``, no PyNaCl, no PyCryptodome), and no dependency is
  added here on the build lane's own authority. The exporter refuses
  by name (:data:`PRIVATE_UNAVAILABLE_REASON`) and writes nothing, so
  no plaintext bundle can ever be mistaken for a private one. When an
  AES-GCM dependency is approved, private exports will encrypt the
  payload with PBKDF2-HMAC-SHA-256 + AES-256-GCM (WebCrypto-compatible
  parameters) and will exclude the video — a plaintext MP4 never
  ships beside an encrypted page.

The exports registry (``exports.json`` beside the wrapped outputs)
records what was exported and where the operator said it was going,
one appended entry per export; a corrupt registry fails soft with a
named note and exports still work.
"""
from __future__ import annotations

import copy
import html
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..core.print_stats import hours_text, plural

VISIBILITIES = ("public", "link-only", "private")
# Link-only exports each get a new unguessable folder; older ones for the
# same year are removed past this many so they do not pile up.
MAX_LINK_ONLY_EXPORTS = 3
VIDEO_FOOTAGE_NOTE = ("The video contains camera footage: frames from your "
                      "printer's camera, which can show the room, the desk "
                      "and anyone in view. Watch it before you share it.")
CARD_LANDSCAPE_SIZE = (1200, 630)
CARD_VERTICAL_SIZE = (1080, 1350)
REGISTRY_NAME = "exports.json"
GENERATOR_NOTE = ("Produced locally by Print Handy: this bundle was "
                  "generated on the operator's own machine and uploaded "
                  "by the operator, if at all.")
PRIVATE_UNAVAILABLE_REASON = (
    "private exports are not available yet: encrypting the payload needs "
    "AES-GCM from an established library, and Print Handy does not "
    "currently depend on one (the 'cryptography' package is not in the "
    "dependency set). No bundle was written. Public and link-only "
    "exports work; private exports arrive once an AES-GCM dependency "
    "is approved.")


def private_available() -> bool:
    """Whether an established AES-GCM library is importable.

    Checked honestly at call time: today ``cryptography`` is not a
    dependency, so this is False and private exports refuse by name.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception:
        return False
    return True


def make_slug() -> str:
    """An unguessable, URL-safe folder name for link-only exports."""
    return secrets.token_urlsafe(24)


def _esc(value):
    return html.escape(str(value), quote=True)


def _rate(rate):
    return "unknown" if rate is None else f"{round(rate * 100):g}%"


def _title_strings(payload):
    """Every print title (and job key) the payload carries."""
    found = set()

    def walk(value, key=None):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str) and value.strip():
            if key in ("title", "titles", "job_key", "job_title"):
                found.add(value.strip())

    walk(payload.get("highlights") or {})
    walk(payload.get("stats") or {})
    return found


def _printer_strings(payload):
    found = set()
    highlights = payload.get("highlights") or {}
    if highlights.get("top_printer"):
        found.add(str(highlights["top_printer"]))
    breakdowns = (payload.get("stats") or {}).get("breakdowns") or {}
    for table in ("by_printer", "by_printer_profile"):
        found.update(str(name) for name in (breakdowns.get(table) or {}) if name != "unknown")
    return found


def _mentions(line, names):
    return any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", line) for name in names)


def preview(payload):
    """What an export would carry that a viewer might not expect.

    ``titles`` and ``printers`` are the exact names that would appear on
    the page and in the commentary, so the Share panel can show them
    before anything is written.
    """
    payload = payload or {}
    return {"titles": sorted(_title_strings(payload)),
            "printers": sorted(_printer_strings(payload))}


def redact_payload(payload, *, include_titles=True, include_printers=True):
    """A copy of the wrapped payload with the chosen parts removed.

    Commentary lines that mention a removed title or printer are dropped
    (they are generated text, so a name cannot be cut out of one cleanly),
    the fields that held the names are emptied, and the records-level
    enrichment (source links, filament selections, stop sidecars) never
    leaves the machine in any mode.
    """
    out = copy.deepcopy(payload or {})
    titles = _title_strings(out) if not include_titles else set()
    printers = _printer_strings(out) if not include_printers else set()
    banned = titles | printers
    if banned:
        out["script"] = [line for line in (out.get("script") or [])
                         if not _mentions(str(line), banned)]

    def scrub(value):
        if isinstance(value, dict):
            for key in list(value):
                if key in ("enrichments", "stop_detail", "stop_detail_by_record", "per_print"):
                    value.pop(key)
                elif not include_titles and key in ("title", "job_key", "job_title"):
                    value[key] = None
                elif not include_titles and key == "titles":
                    value[key] = []
                elif not include_printers and key in ("top_printer", "printer"):
                    value[key] = None
                elif not include_printers and key in ("by_printer", "by_printer_profile"):
                    value.pop(key)
                else:
                    scrub(value[key])
        elif isinstance(value, list):
            for item in value:
                scrub(item)

    scrub(out)
    return out


def _payload_parts(payload):
    """The known wrapped payload fields, and nothing else."""
    payload = payload or {}
    stats = payload.get("stats") or {}
    return {
        "year": payload.get("year"),
        "headline": payload.get("headline") or {},
        "highlights": payload.get("highlights") or {},
        "stats": stats,
        "breakdowns": stats.get("breakdowns") or {},
        "outcomes": stats.get("outcomes") or {},
        "script": [str(line) for line in (payload.get("script") or [])],
        "archetype": (payload.get("archetype")
                      or (payload.get("highlights") or {}).get("archetype") or {}),
    }


def _bars(items):
    items = [(str(label), float(value)) for label, value in items]
    peak = max((value for _label, value in items), default=0.0) or 1.0
    rows = "".join(
        f'<div class="bar-row"><span class="bar-label">{_esc(label)}</span>'
        f'<span class="bar-track"><span class="bar-fill" '
        f'style="width:{value / peak * 100:.1f}%"></span></span>'
        f'<span class="bar-value">{value:g}</span></div>'
        for label, value in items)
    return f'<div class="bars">{rows}</div>' if rows else "<p>(nothing recorded)</p>"


_PAGE_CSS = """
body{background:#12110f;color:#f2efe9;font-family:system-ui,sans-serif;
margin:0 auto;max-width:44rem;padding:1.5rem 1rem 3rem;line-height:1.5}
h1{font-size:2.2rem;margin:.2rem 0} h2{margin-top:2.2rem}
.eyebrow{text-transform:uppercase;letter-spacing:.12em;color:#c9a86a;font-size:.8rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.8rem}
.card{background:#1e1c19;border-radius:.8rem;padding:1rem}
.card h2{margin:.2rem 0;font-size:1.8rem}
.bar-row{display:flex;align-items:center;gap:.6rem;margin:.25rem 0}
.bar-label{flex:0 0 9rem;text-align:right}
.bar-track{flex:1;background:#2c2925;border-radius:.3rem;height:1rem}
.bar-fill{display:block;background:#7a9e7e;height:1rem;border-radius:.3rem}
.bar-value{flex:0 0 3rem}
video{width:100%;border-radius:.8rem;background:#000}
.share-card{border:2px solid #c9a86a;text-align:center;padding:2rem 1rem}
""".strip()


def render_share_page(payload, *, visibility="public", video_name=None):
    """The standalone scrolling Wrapped page for one export bundle.

    Same section order as the Web UI Wrapped page. Data comes only
    from the wrapped payload fields named in :func:`_payload_parts`.
    """
    parts = _payload_parts(payload)
    year = parts["year"]
    headline = parts["headline"]
    highlights = parts["highlights"]
    breakdowns = parts["breakdowns"]
    outcomes = parts["outcomes"]
    archetype = parts["archetype"]
    robots = ('<meta name="robots" content="noindex, nofollow">\n'
              if visibility == "link-only" else "")
    sections = [
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<title>Print Handy Wrapped {year}</title>\n{robots}"
        f"<style>{_PAGE_CSS}</style>\n</head>\n<body>",
        f'<section class="wrapped-hero"><p class="eyebrow">Print Handy Wrapped</p>'
        f"<h1>Your {year} in printing</h1>"
        f"<p>{plural(headline.get('total_prints', 0), 'print')}. Scroll the year.</p></section>",
    ]
    cards = "".join(
        f'<section class="card"><p class="eyebrow">{_esc(label)}</p>'
        f"<h2>{_esc(value)}</h2></section>"
        for label, value in (
            ("Prints", headline.get("total_prints", 0)),
            ("Hours printing", f"{headline.get('total_hours', 0):g}"),
            ("Filament used", f"{headline.get('total_filament_used_kg', 0):g} kg"),
            ("Success rate", _rate(headline.get("success_rate")))))
    sections.append(f"<h2>By the numbers</h2><div class=\"cards\">{cards}</div>")
    facts = []
    for label, key in (("Top color", "top_color"), ("Top material", "top_material"),
                       ("Busiest printer (by prints)", "top_printer"), ("Top vendor", "top_vendor")):
        if highlights.get(key):
            facts.append(f"<li>{label}: {_esc(highlights[key])}</li>")
    sections.append(
        f"<h2>Colors and materials</h2><ul>{''.join(facts)}</ul>"
        + _bars([(label, row["count"]) for label, row in
                 list((breakdowns.get("by_color") or {}).items())[:5]])
        + _bars([(label, row["count"]) for label, row in
                 list((breakdowns.get("by_material") or {}).items())[:5]]))
    time_facts = []
    if highlights.get("busiest_month"):
        time_facts.append(f"<li>Busiest month: {_esc(highlights['busiest_month'])} "
                          f"({plural(highlights.get('busiest_month_prints', 0), 'print')})</li>")
    if highlights.get("peak_weekday"):
        time_facts.append(f"<li>Busiest weekday: {_esc(highlights['peak_weekday'])}</li>")
    if highlights.get("peak_hour") is not None:
        time_facts.append(f"<li>Busiest hour: {highlights['peak_hour']:02d}:00</li>")
    time_facts.append(f"<li>Late-night prints (midnight to 5 a.m. local): "
                      f"{highlights.get('night_prints', 0)}</li>")
    monthly = (parts["stats"].get("monthly") or {}).items()
    by_hour = parts["stats"].get("by_hour") or {}
    sections.append(
        f"<h2>Time patterns</h2><ul>{''.join(time_facts)}</ul>"
        + _bars(list(monthly))
        + _bars([(f"{hour:02d}:00", by_hour.get(f"{hour:02d}", 0))
                 for hour in range(24)]))
    most_reprinted = highlights.get("most_reprinted")
    repeat_text = ("(no print was repeated this year)"
                   if not most_reprinted else
                   f"Most reprinted: {_esc(most_reprinted.get('title') or 'untitled')} "
                   f"— printed {most_reprinted.get('count', 0)} times. "
                   f"Reprints this year: {highlights.get('reprint_count', 0)}.")
    sections.append(f"<h2>Repeats</h2><p>{repeat_text}</p>")
    if archetype.get("name"):
        sections.append(
            f"<h2>Your printing personality</h2>"
            f"<p><strong>{_esc(archetype['name'])}</strong> — "
            f"{_esc(archetype.get('fact') or '')}</p>")
    if headline.get("pasta_state") == "recorded" or outcomes.get("pasta_state") == "recorded":
        pasta_text = (f"Pasta: {headline.get('pasta')} prints turned into pasta "
                      f"({outcomes.get('pasta_events', 0)} spaghetti events), counted "
                      "from when watchdog event recording began "
                      f"({outcomes.get('pasta_recording_started_at') or 'unknown'}); "
                      "events from before recording began were never recorded. ")
    else:
        pasta_text = ("Pasta: not recorded yet — the camera watchdog's detections "
                      "are not persisted, so no pasta count exists until event "
                      "recording begins. ")
    sections.append(
        "<h2>Pasta, failures and stops</h2><p>"
        f"Failed prints: {highlights.get('failed', headline.get('failed', 0))}. "
        f"Stopped prints: {headline.get('stopped', headline.get('canceled', 0))}. "
        + pasta_text
        + f"Risk-flagged prints: {highlights.get('risk_flagged', 0)}. "
        f"Worst failure run: {highlights.get('worst_failure_streak', 0)}.</p>")
    if video_name:
        sections.append(
            "<h2>Your Wrapped video</h2>"
            f'<video controls preload="metadata" src="{_esc(video_name)}"></video>')
    lines = "".join(f'<p class="card">{_esc(line)}</p>' for line in parts["script"])
    sections.append(f"<h2>Commentary</h2>{lines}")
    if archetype.get("name"):
        sections.append(
            '<section class="card share-card"><p class="eyebrow">'
            f"Print Handy Wrapped {year}</p>"
            f"<h2>{_esc(archetype['name'])}</h2>"
            f"<p>{plural(headline.get('total_prints', 0), 'print')} · "
            f"{hours_text(headline.get('total_hours', 0))} · "
            f"{headline.get('total_filament_used_kg', 0):g} kg · "
            f"success rate {_rate(headline.get('success_rate'))} · "
            f"top color {_esc(highlights.get('top_color') or 'unknown')}</p></section>")
    sections.append("</body>\n</html>\n")
    return "\n".join(sections)


def _font(size, bold=False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for candidate in (f"/usr/share/fonts/truetype/dejavu/{name}", name):
        try:
            return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    return ImageFont.load_default(size=size)


def render_share_card_png(payload, path, *, vertical=False):
    """One summary share card PNG, rendered with Pillow.

    The step-1 share card fields: prints, hours, kg, success rate,
    top color, and the archetype. Landscape 1200x630 by default;
    ``vertical`` renders the 1080x1350 cut.
    """
    from PIL import Image, ImageDraw
    parts = _payload_parts(payload)
    headline, highlights, archetype = (parts["headline"], parts["highlights"],
                                       parts["archetype"])
    width, height = CARD_VERTICAL_SIZE if vertical else CARD_LANDSCAPE_SIZE
    image = Image.new("RGB", (width, height), (18, 17, 15))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 14], fill=(201, 168, 106))
    margin = 72
    y = 64
    draw.text((margin, y), f"PRINT HANDY WRAPPED {parts['year']}",
              font=_font(34, bold=True), fill=(201, 168, 106))
    y += 84
    if archetype.get("name"):
        draw.text((margin, y), str(archetype["name"]),
                  font=_font(76 if not vertical else 84, bold=True),
                  fill=(242, 239, 233))
        y += 110
    stats_lines = [
        plural(headline.get('total_prints', 0), 'print'),
        f"{hours_text(headline.get('total_hours', 0))} printing",
        f"{headline.get('total_filament_used_kg', 0):g} kg of filament",
        f"Success rate {_rate(headline.get('success_rate'))}",
        f"Top color {highlights.get('top_color') or 'unknown'}",
    ]
    step = 64 if not vertical else 84
    for line in stats_lines:
        draw.text((margin, y), line, font=_font(44), fill=(242, 239, 233))
        y += step
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path


def load_exports(outputs_dir):
    """(entries, note): the exports registry, failing soft.

    A missing registry is an empty list with no note; a corrupt one
    is an empty list plus a note naming the file — exports still work.
    """
    registry = Path(outputs_dir) / REGISTRY_NAME
    if not registry.is_file():
        return [], None
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("registry is not a list")
    except Exception as exc:
        return [], (f"exports registry {REGISTRY_NAME} could not be read "
                    f"({type(exc).__name__}); past exports are not listed, "
                    "but new exports still work.")
    return [entry for entry in data if isinstance(entry, dict)], None


def _write_registry(outputs_dir, entries, note):
    registry = Path(outputs_dir) / REGISTRY_NAME
    try:
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    except OSError as exc:
        return (note or f"exports registry {REGISTRY_NAME} could not be "
                f"written ({type(exc).__name__}); the export itself succeeded.")
    return note


_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{16,}")


def _record_export(outputs_dir, entry):
    """Record one export; returns a note string or None.

    A re-export of the same folder replaces its entry instead of listing
    it twice. Link-only exports of a year past MAX_LINK_ONLY_EXPORTS
    lose their oldest folders (and entries), so repeated exports do not
    pile up copies on disk.
    """
    entries, note = load_exports(outputs_dir)
    entries = [e for e in entries if e.get("folder") != entry["folder"]]
    entries.append(entry)
    if entry["visibility"] == "link-only":
        same = [e for e in entries if e.get("visibility") == "link-only"
                and e.get("year") == entry["year"]]
        same.sort(key=lambda e: str(e.get("exported_at") or ""))
        for old in same[:-MAX_LINK_ONLY_EXPORTS]:
            folder = str(old.get("folder") or "")
            if _SLUG_RE.fullmatch(folder):
                shutil.rmtree(Path(outputs_dir) / "exports" / folder, ignore_errors=True)
            entries.remove(old)
    return _write_registry(outputs_dir, entries, note)


def _place_video(source, target):
    """The video into the bundle: a hard link when the disk allows (no
    second full copy of a large file), else a copy."""
    target = Path(target)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def export_bundle(payload, outputs_dir, *, visibility="link-only",
                  destination_note="", passphrase=None, now=None,
                  include_titles=True, include_printers=True, include_video=False):
    """Write one share bundle. Returns a result dict.

    ``outputs_dir`` is the wrapped outputs folder (the video for the
    year, when one exists, lives directly inside it); bundles land in
    ``outputs_dir / "exports" / <folder>`` and the registry in
    ``outputs_dir / exports.json``. ``include_titles``,
    ``include_printers`` and ``include_video`` choose what rides along
    (see the module docstring); the video is camera footage and is left
    out unless asked for. Files from an earlier export of the same
    folder that this one no longer references are removed. ``passphrase``
    is accepted for the private mode's future shape and is unused while
    private exports are unavailable: it is never stored anywhere.
    """
    if visibility not in VISIBILITIES:
        return {"ok": False, "reason": f"unknown visibility {visibility!r}: "
                                       "choose public, link-only or private. "
                                       "Nothing was exported."}
    if visibility == "private":
        return {"ok": False, "reason": PRIVATE_UNAVAILABLE_REASON}
    payload = redact_payload(payload, include_titles=include_titles,
                             include_printers=include_printers)
    parts = _payload_parts(payload)
    year = parts["year"]
    if not isinstance(year, int) or isinstance(year, bool):
        return {"ok": False, "reason": "the wrapped payload carries no year. "
                                       "Nothing was exported."}
    outputs_dir = Path(outputs_dir)
    folder_name = f"wrapped-{year}" if visibility == "public" else make_slug()
    folder = outputs_dir / "exports" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    exported_at = (now or datetime.now(timezone.utc)).isoformat()
    notes = []
    video_name = f"wrapped-{year}.mp4"
    video_source = outputs_dir / video_name
    # The precompiled video can contain titles and printer names in both
    # its slides and narration. Until it can be rebuilt from the redacted
    # payload, never attach it when either identifying category is excluded.
    video_included = (bool(include_video) and include_titles and include_printers
                      and video_source.is_file())
    if video_included:
        _place_video(video_source, folder / video_name)
        notes.append(VIDEO_FOOTAGE_NOTE)
    elif include_video and not (include_titles and include_printers):
        notes.append("the compiled video was left out because it may contain "
                     "titles or printer names excluded from this export.")
    elif include_video:
        notes.append(f"no {video_name} in the wrapped outputs, so no video "
                     "was included; compile the Wrapped video first if you "
                     "want one in the bundle.")
    else:
        notes.append("the video (camera footage) was left out of this export.")
    if not include_titles:
        notes.append("print titles were left out, with every commentary line "
                     "that named one.")
    if not include_printers:
        notes.append("printer names were left out, with every commentary line "
                     "that named one.")
    if visibility == "link-only":
        notes.append("link-only is obscurity, not privacy: anyone with the "
                     "link can open this page. The folder name is unguessable "
                     "and the page asks search engines not to index it, but "
                     "there is no access control.")
    page = render_share_page(payload, visibility=visibility,
                             video_name=video_name if video_included else None)
    (folder / "index.html").write_text(page, encoding="utf-8")
    render_share_card_png(payload, folder / "share-card.png")
    render_share_card_png(payload, folder / "share-card-vertical.png", vertical=True)
    files = [{"name": "index.html", "kind": "page"},
             {"name": "share-card.png", "kind": "share-card"},
             {"name": "share-card-vertical.png", "kind": "share-card"}]
    if visibility == "link-only":
        (folder / "robots.txt").write_text("User-agent: *\nDisallow: /\n",
                                           encoding="utf-8")
        files.append({"name": "robots.txt", "kind": "robots"})
    if video_included:
        files.append({"name": video_name, "kind": "video"})
    files.append({"name": "manifest.json", "kind": "manifest"})
    manifest = {"year": year, "visibility": visibility,
                "exported_at": exported_at, "video_included": video_included,
                "titles_included": bool(include_titles),
                "printer_names_included": bool(include_printers),
                "generator": GENERATOR_NOTE, "files": files, "notes": notes}
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    referenced = {entry["name"] for entry in files}
    for leftover in folder.iterdir():
        if leftover.is_file() and leftover.name not in referenced:
            leftover.unlink(missing_ok=True)
    entry = {"folder": folder_name, "year": year, "visibility": visibility,
             "destination": str(destination_note or ""),
             "exported_at": exported_at, "video_included": video_included,
             "titles_included": bool(include_titles),
             "printer_names_included": bool(include_printers)}
    registry_note = _record_export(outputs_dir, entry)
    return {"ok": True, "path": str(folder), "folder": folder_name,
            "year": year, "visibility": visibility,
            "video_included": video_included, "manifest": manifest,
            "registry_note": registry_note}


__all__ = ["CARD_LANDSCAPE_SIZE", "CARD_VERTICAL_SIZE", "GENERATOR_NOTE",
           "MAX_LINK_ONLY_EXPORTS", "PRIVATE_UNAVAILABLE_REASON", "REGISTRY_NAME",
           "VIDEO_FOOTAGE_NOTE", "VISIBILITIES",
           "export_bundle", "load_exports", "make_slug", "preview", "private_available",
           "redact_payload", "render_share_card_png", "render_share_page"]
