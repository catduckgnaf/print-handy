"""The Wrapped video compile (#956): stat cards + timelapse clips + script.

On demand only. The compile itself is local; the one exception is the
commentary enrichment, which sends the script lines (with their
numbers) to the configured AI route when one is set — the Wrapped
page discloses that next to the button. Cards are generated with ffmpeg's lavfi
``color`` source and ``drawtext``; timelapse clips whose file mtime
falls in the Wrapped year are trimmed and appended when a clip finder
discovers them. There is no reliable single timelapse store on disk —
printer-rendered clips live in the Moonraker timelapse root / the SSH
archive (``[timelapse] archive_dir``), in-container and plan clips live
under the state and cache folders — so the default finder searches the
local candidates it can name (a local ``archive_dir``, the announcement
media cache under ``[paths] cache``, and ``<state>/timelapse_plans``)
and a year with no clips found is a correct cards-only video whose
result names ``timelapses: none found``.

Every missing piece is named in ``notes``: no ffmpeg is a named
refusal, no AI route keeps the deterministic script and names
``ai_commentary: unavailable (<reason>)``, and no timelapses names
that too. No TTS narration is built — out of scope for #956.

``runner``, ``find_clips`` and ``ai_call`` are injectable seams so
tests never run real ffmpeg or the network.

Wrapped v2 (#981): delivered clips carry their print number in the
file name (``<YYYYMMDD-HHMM>-<model>-print-<n>``, from
``timelapse_encode.delivery_name()``), so the finder now parses that
name and joins clips to prints across every location it searches
(archive dir first). The video is presentation, not a best-hits reel:
clip selection reserves slots for failed/pasta prints' clips within
the cap, and a label card immediately before such a clip says what it
is.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

MAX_CLIPS = 6
# Of the MAX_CLIPS slots, up to this many are reserved for clips of
# failed or pasta prints when the name join proves them — the failures
# are part of the year, not edited out of it.
RESERVED_FAILURE_CLIPS = 2
CLIP_SECONDS = 4
CARD_SECONDS = 3
# Every segment (card or clip) is encoded at this one frame rate with
# this one video timebase (#981): the final concat is a stream copy, so
# segments whose frame rates or timebases differ get their durations
# rescaled by the concat and clip segments inflate (two 3-second clips
# at mismatched rates produced a 72-second video where about 60 was
# expected in the #981 prototype).
CARD_FPS = 30
SEGMENT_TIMESCALE = 90000
CARD_WRAP = 46  # chars per drawtext line: fits the 1280px frame at fontsize 40
CARD_LINE_PX = 52


def escape_drawtext(text):
    """Escape one line for an ffmpeg drawtext ``text=`` value."""
    out = []
    for char in str(text):
        if char in "\\':%":
            out.append("\\" + char)
        elif char in "\r\n":
            out.append(" ")
        else:
            out.append(char)
    return "".join(out)


def _default_ffmpeg(data):
    named = str(((data or {}).get("paths") or {}).get("ffmpeg") or "").strip()
    return named or "ffmpeg"


_DELIVERY_NUMBER_RE = re.compile(r"-print-(\d+)$")
_DELIVERY_DATE_RE = re.compile(r"^(\d{4})\d{4}-\d{4}-")


def parse_clip_print_number(name):
    """The print number in a delivery file name, or None.

    Delivery names are ``<YYYYMMDD-HHMM>-<model>-print-<n>`` (the
    model's own text may contain dashes, so only the trailing
    ``-print-<n>`` counts). A bare ``print-<n>`` with nothing before it
    is not a delivery name and does not join.
    """
    stem = Path(str(name)).stem
    match = _DELIVERY_NUMBER_RE.search(stem)
    if not match:
        return None
    return int(match.group(1))


def _delivery_name_year(name):
    """The year in a delivery name's ``<YYYYMMDD-HHMM>`` prefix, or None."""
    match = _DELIVERY_DATE_RE.match(Path(str(name)).stem)
    return int(match.group(1)) if match else None


def _clip_roots(data):
    """The local timelapse candidate roots, archive first (#981)."""
    roots = []
    archive_dir = str(((data or {}).get("timelapse") or {}).get("archive_dir") or "").strip()
    if archive_dir and Path(archive_dir).is_dir():
        roots.append(Path(archive_dir))
    cache = str(((data or {}).get("paths") or {}).get("cache") or "").strip()
    if cache:
        roots.append(Path(cache) / "moonraker-announcements")
    try:
        from .. import settings as _settings
        roots.append(_settings.state_dir(data) / "timelapse_plans")
    except Exception:
        pass
    return roots


def default_clip_finder_detailed(data, year):
    """Clip dicts {path, print_number, name_year} for the Wrapped year.

    Every existing finder location is searched, archive dir first, and
    each file's delivery name is parsed for its print number. A clip
    belongs to the year when its name-date says so (the delivery name
    carries the print's own start date, which mtime — a copy/archive
    date — can get wrong), or when its mtime falls in the year.
    Results are in finder order (archive first, then mtime within a
    root's matches as found sorted by mtime per root scan below).
    """
    found = []
    seen = set()
    for root in _clip_roots(data):
        if not root.is_dir():
            continue
        paths = []
        for path in root.rglob("*.mp4"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            paths.append((mtime, path))
        for mtime, path in sorted(paths, key=lambda item: item[0]):
            key = str(path)
            if key in seen:
                continue
            name_year = _delivery_name_year(path.name)
            if name_year is not None:
                if name_year != int(year):
                    continue
            elif mtime.year != int(year):
                continue
            seen.add(key)
            found.append({"path": str(path),
                          "print_number": parse_clip_print_number(path.name),
                          "name_year": name_year})
    return found


def default_clip_finder(data, year):
    """MP4 paths under the local timelapse candidates for ``year``."""
    return [Path(entry["path"])
            for entry in default_clip_finder_detailed(data, year)][:MAX_CLIPS]


def _clip_entry(clip):
    """Normalize one finder result (Path/str or detailed dict)."""
    if isinstance(clip, dict):
        return {"path": str(clip.get("path") or ""),
                "print_number": clip.get("print_number")}
    return {"path": str(clip), "print_number": parse_clip_print_number(clip)}


def clip_label(entry, wrapped):
    """The honest label for one clip, or None when it needs none.

    A clip joined to a pasta print is labeled pasta first (that is
    what the camera saw); a clip joined to a failed print is labeled
    failed. Unjoined clips and successful prints' clips get no label.
    """
    number = entry.get("print_number")
    if number is None:
        return None
    highlights = (wrapped or {}).get("highlights") or {}
    if number in (highlights.get("pasta_print_numbers") or []):
        return "This one turned into pasta"
    if number in (highlights.get("failed_print_numbers") or []):
        return "This one failed"
    return None


def select_clips(clips, wrapped, cap=MAX_CLIPS):
    """Choose the video's clips: the year's clips, failures included.

    Not a best-hits selection: candidates keep finder order, but up to
    ``RESERVED_FAILURE_CLIPS`` slots are reserved for clips the name
    join ties to a failed or pasta print — if the year's first ``cap``
    clips are all successes and a failure clip sits just past the cap,
    the latest success clips give up their slots so the failure is in
    the video. The chosen set is returned in finder order.
    """
    entries = [_clip_entry(clip) for clip in (clips or [])]
    if len(entries) <= cap:
        return entries
    chosen_idx = list(range(cap))
    failure_idx = [i for i in range(cap, len(entries))
                   if clip_label(entries[i], wrapped) is not None]
    reserve = min(RESERVED_FAILURE_CLIPS, len(failure_idx))
    if reserve:
        chosen_idx = chosen_idx[:cap - reserve] + failure_idx[:reserve]
        chosen_idx.sort()
    return [entries[i] for i in chosen_idx]


def _ai_enrich(script, data, ai_call):
    """(script, note): the AI rewrite, or the original script + why not."""
    if ai_call is not None:
        try:
            answer = ai_call(list(script))
        except Exception as exc:
            return script, f"ai_commentary: unavailable ({type(exc).__name__}: {exc})"
        lines = _lines_from_answer(answer)
        return (lines, None) if lines else (script, "ai_commentary: unavailable (empty answer)")
    try:
        from .. import ai_routes
    except Exception as exc:  # pragma: no cover - import guard
        return script, f"ai_commentary: unavailable ({type(exc).__name__}: {exc})"
    if data is None or not ai_routes.configured("chat", cfg=data):
        return script, "ai_commentary: unavailable (no AI route configured)"
    prompt = ("Rewrite these Print Handy Wrapped commentary lines in a roastier "
              "Spotify Wrapped voice. Keep every number exactly as given, keep one "
              "line per line, and return only the lines:\n" + "\n".join(script))
    try:
        answer, _served = ai_routes.call(
            "chat", [{"role": "user", "content": prompt}], cfg=data, max_tokens=800)
    except Exception as exc:
        return script, f"ai_commentary: unavailable ({type(exc).__name__}: {exc})"
    lines = _lines_from_answer(answer)
    return (lines, None) if lines else (script, "ai_commentary: unavailable (empty answer)")


def _lines_from_answer(answer):
    if isinstance(answer, (list, tuple)):
        return [str(line).strip() for line in answer if str(line).strip()]
    if isinstance(answer, str):
        return [line.strip() for line in answer.splitlines() if line.strip()]
    return []


def _run(runner, command):
    result = runner(command, capture_output=True, text=True)
    code = getattr(result, "returncode", 0)
    if code:
        raise RuntimeError(f"ffmpeg exited {code}")


def _card_lines(text):
    """Wrap one card's text so every drawtext line fits inside the frame."""
    lines = []
    for chunk in str(text).split("\n"):
        lines.extend(textwrap.wrap(chunk, width=CARD_WRAP) or [""])
    return lines[:8]


def _card_filter(text):
    """A chain of drawtext filters, one per wrapped line, block-centered."""
    lines = _card_lines(text)
    middle = (len(lines) - 1) / 2
    filters = []
    for index, line in enumerate(lines):
        offset = round((index - middle) * CARD_LINE_PX)
        y = f"(h-text_h)/2{offset:+d}" if offset else "(h-text_h)/2"
        filters.append(
            f"drawtext=text='{escape_drawtext(line)}':"
            f"fontcolor=white:fontsize=40:x=(w-text_w)/2:y={y}")
    return ",".join(filters)


def _card_texts(wrapped):
    """The card sequence, data-led (#981): headline numbers, the
    separate failed/stopped counts, the most-reprinted print, the top
    color and material, then the commentary lines. Every card is
    gated on its number being present in the payload. This builder is
    the seam a later presentation rework swaps; the pacing contract
    (segment frame rate/timebase) lives in the segment commands, not
    here."""
    headline = wrapped.get("headline") or {}
    highlights = wrapped.get("highlights") or {}
    texts = [f"Print Handy Wrapped {wrapped.get('year')}"]
    if headline.get("total_prints"):
        texts.append(f"{headline['total_prints']} prints")
    if headline.get("total_hours"):
        texts.append(f"{headline['total_hours']:g} hours printing")
    if headline.get("total_filament_used_g"):
        texts.append(f"{headline['total_filament_used_kg']:g} kg of filament")
    if headline.get("success_rate") is not None:
        texts.append(f"Success rate {round(headline['success_rate'] * 100):g}%")
    if headline.get("failed"):
        texts.append(f"{headline['failed']} failed print"
                     f"{'s' if headline['failed'] != 1 else ''}")
    stopped = headline.get("stopped", headline.get("canceled"))
    if stopped:
        texts.append(f"{stopped} stopped print{'s' if stopped != 1 else ''}")
    most_reprinted = highlights.get("most_reprinted")
    if most_reprinted and most_reprinted.get("count"):
        texts.append(f"{most_reprinted.get('title') or 'One print'} printed "
                     f"{most_reprinted['count']} times")
    if highlights.get("top_color"):
        texts.append(f"Top color {highlights['top_color']}")
    if highlights.get("top_material"):
        texts.append(f"Top material {highlights['top_material']}")
    archetype = wrapped.get("archetype") or highlights.get("archetype")
    if archetype and archetype.get("name"):
        # The personality card sits mid-sequence, never last: the
        # commentary (ending on the closing line) follows it.
        texts.append(f"Your printing personality: {archetype['name']}")
    texts.extend(str(line) for line in (wrapped.get("script") or []))
    return texts


def build_wrapped_video(year, wrapped, out_dir, *, data=None, ffmpeg=None,
                        runner=None, find_clips=None, ai_call=None, use_ai=True):
    """Compile one Wrapped video. Returns {ok, path, script_path, notes}.

    ``wrapped`` is a :func:`print_handy.core.print_stats.build_wrapped`
    payload. The script sidecar (``wrapped-<year>-script.json``) is
    written even when ffmpeg is missing, so the commentary is never
    lost with the refusal.
    """
    year = int(year)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    script = [str(line) for line in (wrapped.get("script") or [])]
    if use_ai:
        script, note = _ai_enrich(script, data, ai_call)
        if note:
            notes.append(note)
    else:
        notes.append("ai_commentary: unavailable (wrapped_ai_video is off)")
    script_path = out_dir / f"wrapped-{year}-script.json"
    script_path.write_text(json.dumps(
        {"year": year, "script": script, "stats": wrapped.get("headline") or {},
         "highlights": wrapped.get("highlights") or {}},
        indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    ffmpeg = str(ffmpeg or _default_ffmpeg(data))
    resolved = shutil.which(ffmpeg) if "/" not in ffmpeg else (ffmpeg if Path(ffmpeg).is_file() else None)
    if resolved is None:
        notes.append(f"ffmpeg: unavailable ({ffmpeg!r} not found)")
        return {"ok": False, "reason": f"ffmpeg is not available ({ffmpeg!r} not found), "
                                        "so the Wrapped video cannot be compiled",
                "path": None, "script_path": str(script_path), "notes": notes}
    runner = runner or subprocess.run
    finder = find_clips or (lambda y: default_clip_finder_detailed(data, y))
    try:
        found = finder(year) or []
    except Exception:
        found = []
    # #981 v2: selection is never a best-hits reel — select_clips
    # reserves slots for failed/pasta prints' clips within the cap.
    clips = select_clips(found, wrapped)
    labeled = [entry for entry in clips if clip_label(entry, wrapped)]
    if not clips:
        notes.append("timelapses: none found")
    else:
        # #981: these are clips from this year, in the order the finder
        # returned them — never a best-hits or highlights selection.
        notes.append(f"timelapses: {len(clips)} clips from this year")
        if labeled:
            notes.append(f"timelapses: {len(labeled)} of them are failed or "
                         "pasta prints, labeled in the video")
    work = out_dir / f".wrapped-{year}-parts"
    work.mkdir(parents=True, exist_ok=True)
    segments = []
    try:
        for index, text in enumerate(_card_texts({**wrapped, "script": script})):
            segment = work / f"card-{index:02d}.mp4"
            _run(runner, [resolved, "-y", "-f", "lavfi", "-i",
                          f"color=c=0x12110F:s=1280x720:d={CARD_SECONDS}:rate={CARD_FPS}",
                          "-vf", f"fps={CARD_FPS}," + _card_filter(text),
                          "-c:v", "libx264", "-pix_fmt", "yuv420p",
                          "-r", str(CARD_FPS),
                          "-video_track_timescale", str(SEGMENT_TIMESCALE),
                          str(segment)])
            segments.append(segment)
        for index, entry in enumerate(clips):
            label = clip_label(entry, wrapped)
            if label:
                # The label card sits immediately before its clip, so a
                # failure/pasta clip is never mistaken for a success.
                segment = work / f"card-label-{index:02d}.mp4"
                _run(runner, [resolved, "-y", "-f", "lavfi", "-i",
                              f"color=c=0x12110F:s=1280x720:d={CARD_SECONDS}:rate={CARD_FPS}",
                              "-vf", f"fps={CARD_FPS}," + _card_filter(label),
                              "-c:v", "libx264", "-pix_fmt", "yuv420p",
                              "-r", str(CARD_FPS),
                              "-video_track_timescale", str(SEGMENT_TIMESCALE),
                              str(segment)])
                segments.append(segment)
            segment = work / f"clip-{index:02d}.mp4"
            _run(runner, [resolved, "-y", "-i", entry["path"], "-t", str(CLIP_SECONDS),
                          "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                                 "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
                                 f"fps={CARD_FPS}",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                          "-r", str(CARD_FPS),
                          "-video_track_timescale", str(SEGMENT_TIMESCALE),
                          str(segment)])
            segments.append(segment)
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{segment.name}'\n" for segment in segments),
                           encoding="utf-8")
        target = out_dir / f"wrapped-{year}.mp4"
        _run(runner, [resolved, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                      "-c", "copy", str(target)])
    except Exception as exc:
        notes.append(f"ffmpeg: failed ({type(exc).__name__}: {exc})")
        return {"ok": False, "reason": f"the Wrapped video compile failed ({type(exc).__name__}: {exc})",
                "path": None, "script_path": str(script_path), "notes": notes}
    shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "path": str(target), "script_path": str(script_path), "notes": notes}


__all__ = ["CARD_FPS", "CARD_SECONDS", "CLIP_SECONDS", "MAX_CLIPS",
           "RESERVED_FAILURE_CLIPS", "SEGMENT_TIMESCALE", "build_wrapped_video",
           "clip_label", "default_clip_finder", "default_clip_finder_detailed",
           "escape_drawtext", "parse_clip_print_number", "select_clips"]
