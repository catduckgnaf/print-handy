"""The Wrapped video compile (#956): stat cards + timelapse clips + script.

On demand only, and entirely local: the commentary is the Wrapped
payload's own deterministic script, never rewritten by an AI model,
and nothing is sent anywhere. Slides are generated with
ffmpeg (lavfi ``color`` sources, frame grabs from the year's clips,
and ``drawtext``); timelapse clips whose file mtime
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
refusal, and no timelapses names that too.

``runner`` and ``find_clips`` are injectable seams so tests never run
real ffmpeg.

Wrapped v2 (#981): delivered clips carry their print number in the
file name (``<YYYYMMDD-HHMM>-<model>-print-<n>``, from
``timelapse_encode.delivery_name()``), so the finder now parses that
name and joins clips to prints across every location it searches
(archive dir first). The video is presentation, not a best-hits reel:
clip selection reserves slots for failed/pasta prints' clips within
the cap, and a label card immediately before such a clip says what it
is.

Step 4 (#981): the presentation is the spec'd slide arc (see
:func:`build_slides`) over operator-image backgrounds, with a music
bed (an operator file, or a locally synthesized lavfi pad — no audio
assets are ever bundled) whose failures slide gets a slow minor pad
and a descending sad sting, and optional narration by local Piper
(``[timelapse] wrapped_tts_engine``, the binary under ``[paths]
piper`` with its voice model at ``[paths] piper_model``). Narration
never uses an AI model or a cloud service.
``export_silent`` produces a video with no audio inputs at all.
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
# Slide presentation (#981 step 4): 5-7 seconds per slide, the finale
# share card held longest, one full-bleed footage moment.
HOOK_SECONDS = 5
SLIDE_SECONDS = 6
SHORT_SECONDS = 5
FINALE_SECONDS = 9
FOOTAGE_SECONDS = 6
# Background frames: at least this many bytes is a full-bleed frame;
# a smaller extracted JPEG is treated as near-uniform (the documented
# extreme-frame signal) and rendered as an inset instead.
BG_FULL_MIN_BYTES = 4096
BG_MIN_BYTES = 1
# Audio levels, pinned by the tests: narration primary, bed under it.
BED_VOLUME = 0.16
BED_DUCKED_VOLUME = 0.10
BED_FAILURE_VOLUME = 0.04
SAD_VOLUME = 0.22
STING_VOLUME = 0.30
NARRATION_VOLUME = 1.0


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


_DELIVERY_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})-")


def _record_number(record):
    number = (record or {}).get("print_number")
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def resolve_clip_print_number(name, records, tz=None):
    """The CURRENT print number a delivered clip belongs to, or None.

    The name join alone breaks after a duplicate-number repair
    (#1017): a renumbered print's record carries its new number (the
    old one kept as ``print_number_was``), while its already-delivered
    clips still carry the old number — so a name-only join hands the
    reprint's clip to the original print. Records settle it:

    * no record claims the name's number (as its number or its
      ``print_number_was``): the parsed number stands;
    * exactly one record claims it: that record's current number;
    * several claim it (the original kept the number, a reprint was
      renumbered off it): the clip's own start stamp
      (``<YYYYMMDD-HHMM>``, the print's start in the display timezone)
      picks the record that started then; no stamp match falls back
      to the parsed number rather than guessing.
    """
    number = parse_clip_print_number(name)
    if number is None:
        return None
    claims = []
    for record in records or []:
        current = _record_number(record)
        if current is None:
            continue
        was = record.get("print_number_was")
        if current == number or (isinstance(was, int) and not isinstance(was, bool)
                                 and was == number):
            claims.append(record)
    if not claims:
        return number
    if len(claims) == 1:
        return _record_number(claims[0])
    stamp = _DELIVERY_STAMP_RE.match(Path(str(name)).stem)
    if stamp:
        prefix = "".join(stamp.groups())
        from datetime import datetime as _datetime
        for record in claims:
            started = str(record.get("started_at") or "")
            try:
                when = _datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                continue
            if tz is not None and when.tzinfo is not None:
                when = when.astimezone(tz)
            if when.strftime("%Y%m%d%H%M") == prefix:
                return _record_number(record)
    return number


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


def default_clip_finder_detailed(data, year, records=None, tz=None):
    """Clip dicts {path, print_number, name_year} for the Wrapped year.

    Every existing finder location is searched, archive dir first, and
    each file's delivery name is parsed for its print number. A clip
    belongs to the year when its name-date says so (the delivery name
    carries the print's own start date, which mtime — a copy/archive
    date — can get wrong), or when its mtime falls in the year.
    Results are in finder order (archive first, then mtime within a
    root's matches as found sorted by mtime per root scan below).

    When ``records`` (the year's print records) are supplied, each
    clip's number is resolved through them
    (:func:`resolve_clip_print_number`), so a clip delivered before a
    duplicate-number repair still joins to its own print.
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
                          "print_number": (resolve_clip_print_number(path.name, records, tz)
                                           if records is not None
                                           else parse_clip_print_number(path.name)),
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


def _words(text, cap):
    return " ".join(str(text or "").split()[:cap])


def _slide(role, eyebrow, hero, line, duration):
    """One slide: three type levels only, at most 20 words total."""
    hero = _words(hero, 8)
    line = _words(line, 12)
    total = len((eyebrow + " " + hero + " " + line).split())
    while total > 20 and line:
        line = " ".join(line.split()[:-1])
        total = len((eyebrow + " " + hero + " " + line).split())
    return {"role": role, "eyebrow": eyebrow, "hero": hero, "line": line,
            "duration": duration,
            "spoken": (hero + ". " + line).strip(". ")}


def build_slides(wrapped):
    """The slide arc (#981 design spec, comment 6702521843).

    Hook, volume, top items, one commentary slide, personality,
    failures (one slide, mid-late, never last), and the finale share
    card held longest. 11-14 slides for a full year; every slide after
    the hook and finale is data-gated, so a thin year is honestly
    shorter, never padded with invented numbers. The full-bleed
    timelapse moment is a segment, not a slide (see timeline_roles).
    """
    headline = wrapped.get("headline") or {}
    highlights = wrapped.get("highlights") or {}
    year = wrapped.get("year")
    slides = [_slide("hook", "WRAPPED", f"Print Handy Wrapped {year}",
                     "Your year in plastic, counted honestly.", HOOK_SECONDS)]
    if headline.get("total_prints"):
        slides.append(_slide("volume", "VOLUME", f"{headline['total_prints']} prints",
                             "Every print you started this year.", SLIDE_SECONDS))
    if headline.get("total_hours"):
        slides.append(_slide("volume", "VOLUME", f"{headline['total_hours']:g} hours printing",
                             "Time the printer spent making things.", SLIDE_SECONDS))
    if headline.get("total_filament_used_g"):
        kg = headline.get("total_filament_used_kg")
        if kg is None:
            kg = headline["total_filament_used_g"] / 1000.0
        slides.append(_slide("volume", "VOLUME", f"{kg:g} kg of filament",
                             "Plastic turned into finished objects.", SLIDE_SECONDS))
    if headline.get("success_rate") is not None:
        slides.append(_slide("success", "OUTCOMES",
                             f"Success rate {round(headline['success_rate'] * 100):g}%",
                             "Finished prints out of prints decided.", SHORT_SECONDS))
    longest = highlights.get("longest_print") or {}
    if longest.get("title"):
        hours = (longest.get("seconds") or 0) / 3600.0
        slides.append(_slide("top", "TOP PRINT", _words(longest["title"], 6),
                             f"Your longest print, {hours:.1f} hours on the bed.",
                             SLIDE_SECONDS))
    most_reprinted = highlights.get("most_reprinted") or {}
    if most_reprinted.get("count"):
        title = _words(most_reprinted.get("title") or "One print", 4)
        slides.append(_slide("top", "TOP PRINT",
                             f"{title} printed {most_reprinted['count']} times",
                             "Your most reprinted print this year.", SLIDE_SECONDS))
    if highlights.get("top_color"):
        slides.append(_slide("top", "TOP COLOR", f"Top color {highlights['top_color']}",
                             "The color you reached for most.", SHORT_SECONDS))
    if highlights.get("top_material"):
        slides.append(_slide("top", "TOP MATERIAL",
                             f"Top material {highlights['top_material']}",
                             "The material you printed most.", SHORT_SECONDS))
    if highlights.get("busiest_month"):
        slides.append(_slide("top", "BUSIEST MONTH",
                             f"Busiest month {highlights['busiest_month']}",
                             f"{highlights.get('busiest_month_prints', 0)} prints in your biggest month.",
                             SHORT_SECONDS))
    script = [str(line) for line in (wrapped.get("script") or []) if str(line).strip()]
    if script:
        # The first commentary line, chunked into slides of at most
        # twelve words so no word is lost and no slide breaks the
        # word cap; at most two commentary slides, so the arc stays
        # inside its slide budget for a full year.
        words = script[0].split()
        for start in range(0, min(len(words), 24), 12):
            chunk = words[start:start + 12]
            slides.append(_slide("commentary", "COMMENTARY",
                                 " ".join(chunk[:6]), " ".join(chunk[6:]),
                                 SLIDE_SECONDS))
    archetype = wrapped.get("archetype") or highlights.get("archetype") or {}
    if archetype.get("name"):
        slides.append(_slide("personality", "YOUR PRINTING PERSONALITY",
                             archetype["name"],
                             _words(archetype.get("fact") or
                                    "The personality your year earned you.", 12),
                             SLIDE_SECONDS))
    failed = headline.get("failed") or 0
    stopped = headline.get("stopped", headline.get("canceled")) or 0
    pasta = headline.get("pasta")
    if failed or stopped or pasta:
        hero = (f"{failed} failed print{'s' if failed != 1 else ''}" if failed
                else f"{stopped} stopped print{'s' if stopped != 1 else ''}" if stopped
                else f"{pasta} pasta print{'s' if pasta != 1 else ''}")
        bits = []
        if failed and stopped:
            bits.append(f"Plus {stopped} stopped print{'s' if stopped != 1 else ''}.")
        if pasta:
            bits.append(f"{pasta} turned into pasta.")
        bits.append("The printer maketh spaghetti.")
        slides.append(_slide("failures", "FAILURES", hero, " ".join(bits), SLIDE_SECONDS))
    summary_bits = []
    if headline.get("total_prints"):
        summary_bits.append(f"{headline['total_prints']} prints")
    if headline.get("total_hours"):
        summary_bits.append(f"{headline['total_hours']:g} hours")
    if headline.get("total_filament_used_g"):
        kg = headline.get("total_filament_used_kg")
        if kg is None:
            kg = headline["total_filament_used_g"] / 1000.0
        summary_bits.append(f"{kg:g} kg")
    slides.append(_slide("finale", "YOUR YEAR, WRAPPED",
                         archetype.get("name") or f"That's {year}",
                         " · ".join(summary_bits) if summary_bits
                         else "Thanks for printing with Print Handy.",
                         FINALE_SECONDS))
    return slides


def timeline_roles(wrapped, clips=0):
    """Segment roles in order: slides, with the full-bleed footage
    moment inserted before the personality slide (or before the
    failures/finale when there is no personality slide) when the year
    has at least one clip."""
    roles = [slide["role"] for slide in build_slides(wrapped)]
    if clips:
        for anchor in ("personality", "failures", "finale"):
            if anchor in roles:
                roles.insert(roles.index(anchor), "footage")
                break
    return roles


def _card_texts(wrapped):
    """Compatibility seam: the slide hero texts, in arc order.

    The presentation builder is :func:`build_slides`; this keeps the
    old card-text shape available to anything that consumed it.
    """
    return [slide["hero"] for slide in build_slides(wrapped)]


def _slide_filter(slide):
    """Three type levels: eyebrow 28px, hero 64px, explanation 36px.

    All text sits in the central vertical-safe band of the 16:9 frame
    (hero wrapped narrow and centered), so a 9:16 center cut can be
    derived from the same composition later.
    """
    filters = [
        f"drawtext=text='{escape_drawtext(slide['eyebrow'])}':fontcolor=white:"
        "fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2-130"]
    hero_lines = textwrap.wrap(slide["hero"], width=18) or [""]
    for index, line in enumerate(hero_lines[:3]):
        filters.append(
            f"drawtext=text='{escape_drawtext(line)}':fontcolor=white:"
            f"fontsize=64:x=(w-text_w)/2:y=(h-text_h)/2{-50 + index * 76:+d}")
    line_lines = textwrap.wrap(slide["line"], width=34) or []
    for index, line in enumerate(line_lines[:2]):
        filters.append(
            f"drawtext=text='{escape_drawtext(line)}':fontcolor=white:"
            f"fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2{130 + index * 46:+d}")
    return ",".join(filters)


def _extract_frames(runner, ffmpeg_bin, clips, work, count):
    """One frame per slide from the year's clips, or {} per slide index.

    Returns {slide_index: (path, size)} for frames that exist on disk
    after the grab; a failed grab simply has no entry.
    """
    frames = {}
    if not clips:
        return frames
    for index in range(count):
        clip = clips[index % len(clips)]
        frame = work / f"bg-{index:02d}.jpg"
        try:
            _run(runner, [ffmpeg_bin, "-y", "-ss", "1", "-i", clip["path"],
                          "-frames:v", "1", str(frame)])
        except Exception:
            continue
        if frame.is_file():
            frames[index] = (frame, frame.stat().st_size)
    return frames


def _slide_command(ffmpeg_bin, slide, segment, frame):
    """The ffmpeg command for one slide segment.

    Background rule (documented in docs/WEBUI.md): a frame of at
    least BG_FULL_MIN_BYTES renders full-bleed, blurred, under a
    black overlay of 0.62 plus a stronger bottom scrim (never below
    about 54% effective black behind text). A smaller frame — a
    near-uniform frame compresses to almost nothing, which is the
    extreme-frame signal — renders as an inset over the solid
    background. No frame at all renders the solid style.
    """
    duration = slide["duration"]
    base_video = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(CARD_FPS),
                  "-video_track_timescale", str(SEGMENT_TIMESCALE)]
    if frame is not None and frame[1] >= BG_FULL_MIN_BYTES:
        filt = ("scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
                "gblur=sigma=10,"
                "drawbox=x=0:y=0:w=iw:h=ih:color=black@0.62:t=fill,"
                "drawbox=x=0:y=ih*0.55:w=iw:h=ih*0.45:color=black@0.35:t=fill,"
                f"fps={CARD_FPS}," + _slide_filter(slide))
        return [ffmpeg_bin, "-y", "-loop", "1", "-i", str(frame[0]),
                "-t", str(duration), "-vf", filt, *base_video, str(segment)]
    if frame is not None:
        filt = (f"[1:v]scale=720:-1[inset];[0:v][inset]overlay=(W-w)/2:30[bg];"
                f"[bg]fps={CARD_FPS}," + _slide_filter(slide) + "[out]")
        return [ffmpeg_bin, "-y", "-f", "lavfi", "-i",
                f"color=c=0x12110F:s=1280x720:d={duration}:rate={CARD_FPS}",
                "-loop", "1", "-i", str(frame[0]),
                "-filter_complex", filt, "-map", "[out]", *base_video, str(segment)]
    return [ffmpeg_bin, "-y", "-f", "lavfi", "-i",
            f"color=c=0x12110F:s=1280x720:d={duration}:rate={CARD_FPS}",
            "-vf", f"fps={CARD_FPS}," + _slide_filter(slide),
            *base_video, str(segment)]


def piper_command(binary, model, out_path):
    """The Piper invocation for one line: text arrives on stdin."""
    return [str(binary), "--model", str(model), "--output_file", str(out_path)]


def _piper_synthesize(binary, model, text, out_path):
    """One real Piper run (the default synthesizer; tests inject a seam)."""
    result = subprocess.run(piper_command(binary, model, out_path),
                            input=str(text), capture_output=True, text=True)
    if getattr(result, "returncode", 0):
        raise RuntimeError(f"piper exited {result.returncode}")
    return out_path


def _resolve_binary(named):
    named = str(named or "").strip()
    if not named:
        return None, named
    if "/" not in named:
        return shutil.which(named), named
    return (named if Path(named).is_file() else None), named


def _narration_lines(slides):
    return [(index, slide["spoken"]) for index, slide in enumerate(slides)
            if slide.get("spoken")]


def _prepare_narration(slides, data, work, *, tts_call):
    """(files, note): {slide_index: audio path} for the chosen engine.

    Engine none returns ({}, None) — today's behavior. Every failure
    is a named note and an empty mapping: the video is still produced,
    without narration.
    """
    engine = str(((data or {}).get("timelapse") or {}).get("wrapped_tts_engine")
                 or "none").strip().lower()
    if engine == "none":
        return {}, None
    lines = _narration_lines(slides)
    out_dir = work / "narration"
    out_dir.mkdir(parents=True, exist_ok=True)
    if engine == "piper":
        if tts_call is None:
            binary, named = _resolve_binary(
                ((data or {}).get("paths") or {}).get("piper") or "piper")
            if binary is None:
                return {}, f"narration: piper binary not found at {named}"
            model = str(((data or {}).get("paths") or {}).get("piper_model") or "").strip()
            if not model or not Path(model).is_file():
                return {}, f"narration: piper model not found at {model or '(not set)'}"
            tts_call = lambda text, out: _piper_synthesize(binary, model, text, out)
        files = {}
        try:
            for index, text in lines:
                target = out_dir / f"line-{index:02d}.wav"
                tts_call(text, target)
                if Path(target).is_file():
                    files[index] = Path(target)
        except Exception as exc:
            return {}, f"narration: piper unavailable ({type(exc).__name__}: {exc})"
        return files, f"narration: piper narration for {len(files)} slides"
    return {}, f"narration: unknown engine {engine!r}; video has no narration"


# Synthesized beds: local ffmpeg lavfi chord pads, no audio assets, no
# network. The upbeat bed is a major pad; the failures beat is a slow
# minor pad plus a descending sting whose pitch falls as it plays.
UPBEAT_EXPR = "0.16*sin(2*PI*261.63*t)+0.14*sin(2*PI*329.63*t)+0.14*sin(2*PI*392.00*t)"
SAD_EXPR = "0.20*sin(2*PI*220.00*t)+0.16*sin(2*PI*261.63*t)+0.16*sin(2*PI*329.63*t)"
STING_EXPR = "0.30*sin(2*PI*(392.00-60*t)*t)"
STING_SECONDS = 3


def _vol(value):
    """A pinned audio level as it appears in the mux command."""
    if value >= 1:
        return f"{value:g}" + (".0" if value == int(value) else "")
    return f"{value:.2f}"


def _audio_command(ffmpeg_bin, silent, target, *, total, timeline, narration,
                   music_file, music_note_prefix=None):
    """(command, notes): one ffmpeg mux of the music bed + narration.

    Levels are pinned here and in the tests: narration 1.0 primary,
    bed 0.16 (0.10 ducked under narration), bed 0.04 under the
    failures slide while the minor pad plays at 0.22 and the sad
    sting at 0.30, then the bed returns for the finale.
    """
    notes = []
    inputs = [ffmpeg_bin, "-y", "-i", str(silent)]
    filters = []
    labels = []
    if music_file is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music_file)]
        notes.append(f"music: operator file {music_file}")
    else:
        inputs += ["-f", "lavfi", "-i", f"aevalsrc={UPBEAT_EXPR}:d={total}"]
        if music_note_prefix:
            notes.append(music_note_prefix)
        notes.append("music: synthesized bed (generated locally, no audio assets)")
    bed_volume = BED_DUCKED_VOLUME if narration else BED_VOLUME
    filters.append(f"[1:a]atrim=0:{total},volume={_vol(bed_volume)}[bed0]")
    bed_label = "bed0"
    failure = next((entry for entry in timeline if entry.get("role") == "failures"), None)
    next_index = 2
    if failure is not None:
        start, end = failure["start"], failure["start"] + failure["duration"]
        filters.append(
            f"[bed0]volume={_vol(BED_FAILURE_VOLUME)}:enable='between(t,{start},{end})'[bed]")
        bed_label = "bed"
        inputs += ["-f", "lavfi", "-i",
                   f"aevalsrc={SAD_EXPR}:d={failure['duration']}"]
        filters.append(f"[{next_index}:a]adelay={int(start * 1000)}:all=1,"
                       f"volume={_vol(SAD_VOLUME)}[sad]")
        labels.append("sad")
        next_index += 1
        inputs += ["-f", "lavfi", "-i", f"aevalsrc={STING_EXPR}:d={STING_SECONDS}"]
        filters.append(f"[{next_index}:a]adelay={int(start * 1000)}:all=1,"
                       f"volume={_vol(STING_VOLUME)}[sting]")
        labels.append("sting")
        next_index += 1
    starts = {entry.get("slide_index"): entry for entry in timeline}
    narr_labels = []
    for slide_index in sorted(narration):
        entry = starts.get(slide_index)
        if entry is None:
            continue
        inputs += ["-i", str(narration[slide_index])]
        label = f"n{slide_index}"
        filters.append(f"[{next_index}:a]adelay={int(entry['start'] * 1000)}:all=1,"
                       f"volume={_vol(NARRATION_VOLUME)}[{label}]")
        narr_labels.append(label)
        next_index += 1
    mix_labels = [bed_label, *labels, *narr_labels]
    filters.append("".join(f"[{label}]" for label in mix_labels)
                   + f"amix=inputs={len(mix_labels)}:duration=first:normalize=0,"
                   f"atrim=0:{total}[aout]")
    command = [*inputs, "-filter_complex", ";".join(filters),
               "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac",
               "-t", str(total), str(target)]
    return command, notes


def build_wrapped_video(year, wrapped, out_dir, *, data=None, ffmpeg=None,
                        runner=None, find_clips=None,
                        export_silent=False, tts_call=None,
                        records=None, tz=None):
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
    finder = find_clips or (lambda y: default_clip_finder_detailed(data, y, records=records, tz=tz))
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
    slides = build_slides({**wrapped, "script": script})
    work = out_dir / f".wrapped-{year}-parts"
    work.mkdir(parents=True, exist_ok=True)
    frames = _extract_frames(runner, resolved, clips, work, len(slides))
    if not frames:
        notes.append("backgrounds: no operator images found")
    segments = []
    timeline = []

    def add_segment(path, duration, role, slide_index=None):
        timeline.append({"path": path, "duration": duration, "role": role,
                         "slide_index": slide_index,
                         "start": sum(e["duration"] for e in timeline)})
        segments.append(path)

    try:
        footage_done = False
        clip_iter = iter(clips)
        footage_entry = next(clip_iter, None)
        rest_entries = list(clip_iter)

        def build_footage():
            segment = work / "clip-footage.mp4"
            _run(runner, [resolved, "-y", "-stream_loop", "-1", "-i",
                          footage_entry["path"], "-t", str(FOOTAGE_SECONDS),
                          "-vf", "scale=1280:720:force_original_aspect_ratio=increase,"
                                 "crop=1280:720,"
                                 "drawbox=x=0:y=ih*0.7:w=iw:h=ih*0.3:color=black@0.55:t=fill,"
                                 f"fps={CARD_FPS},"
                                 "drawtext=text='Your year in motion':fontcolor=white:"
                                 "fontsize=36:x=(w-text_w)/2:y=h-90",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                          "-r", str(CARD_FPS),
                          "-video_track_timescale", str(SEGMENT_TIMESCALE),
                          str(segment)])
            add_segment(segment, FOOTAGE_SECONDS, "footage")

        for index, slide in enumerate(slides):
            if slide["role"] == "finale":
                continue
            if (not footage_done and footage_entry is not None
                    and slide["role"] in ("personality", "failures")):
                build_footage()
                footage_done = True
            segment = work / f"slide-{index:02d}.mp4"
            _run(runner, _slide_command(resolved, slide, segment, frames.get(index)))
            add_segment(segment, slide["duration"], slide["role"], slide_index=index)
        if not footage_done and footage_entry is not None:
            build_footage()
            footage_done = True
        for offset, entry in enumerate(rest_entries):
            label = clip_label(entry, wrapped)
            if label:
                # The label card sits immediately before its clip, so a
                # failure/pasta clip is never mistaken for a success.
                segment = work / f"card-label-{offset:02d}.mp4"
                _run(runner, [resolved, "-y", "-f", "lavfi", "-i",
                              f"color=c=0x12110F:s=1280x720:d={CARD_SECONDS}:rate={CARD_FPS}",
                              "-vf", f"fps={CARD_FPS}," + _card_filter(label),
                              "-c:v", "libx264", "-pix_fmt", "yuv420p",
                              "-r", str(CARD_FPS),
                              "-video_track_timescale", str(SEGMENT_TIMESCALE),
                              str(segment)])
                add_segment(segment, CARD_SECONDS, "label")
            segment = work / f"clip-{offset:02d}.mp4"
            _run(runner, [resolved, "-y", "-stream_loop", "-1", "-i", entry["path"],
                          "-t", str(CLIP_SECONDS),
                          "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                                 "pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
                                 f"fps={CARD_FPS}",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an",
                          "-r", str(CARD_FPS),
                          "-video_track_timescale", str(SEGMENT_TIMESCALE),
                          str(segment)])
            add_segment(segment, CLIP_SECONDS, "clip")
        finale_index = len(slides) - 1
        finale = slides[finale_index]
        segment = work / f"slide-{finale_index:02d}.mp4"
        _run(runner, _slide_command(resolved, finale, segment,
                                    frames.get(finale_index)))
        add_segment(segment, finale["duration"], "finale", slide_index=finale_index)
        listing = work / "concat.txt"
        listing.write_text("".join(f"file '{segment.name}'\n" for segment in segments),
                           encoding="utf-8")
        target = out_dir / f"wrapped-{year}.mp4"
        silent = work / "silent.mp4"
        _run(runner, [resolved, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                      "-c", "copy", str(silent)])
        total = sum(entry["duration"] for entry in timeline)
        if export_silent:
            notes.append("audio: silent export (no music, no narration)")
            shutil.copyfile(silent, target)
        else:
            narration, narration_note = _prepare_narration(
                slides, data, work, tts_call=tts_call)
            if narration_note:
                notes.append(narration_note)
            music_setting = str(((data or {}).get("timelapse") or {}).get(
                "wrapped_music_file") or "").strip()
            music_file = None
            music_prefix = None
            if music_setting:
                if Path(music_setting).is_file():
                    music_file = Path(music_setting)
                else:
                    music_prefix = (f"music: operator file not found at {music_setting}; "
                                    "using synthesized bed")
            command, audio_notes = _audio_command(
                resolved, silent, target, total=total, timeline=timeline,
                narration=narration, music_file=music_file,
                music_note_prefix=music_prefix)
            notes.extend(audio_notes)
            try:
                _run(runner, command)
            except Exception as exc:
                notes.append(f"audio: failed ({type(exc).__name__}: {exc}); silent video kept")
                shutil.copyfile(silent, target)
    except Exception as exc:
        notes.append(f"ffmpeg: failed ({type(exc).__name__}: {exc})")
        return {"ok": False, "reason": f"the Wrapped video compile failed ({type(exc).__name__}: {exc})",
                "path": None, "script_path": str(script_path), "notes": notes}
    shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "path": str(target), "script_path": str(script_path), "notes": notes}


__all__ = ["BED_DUCKED_VOLUME", "BED_FAILURE_VOLUME", "BED_VOLUME", "BG_FULL_MIN_BYTES",
           "CARD_FPS", "CARD_SECONDS", "CLIP_SECONDS", "FINALE_SECONDS", "FOOTAGE_SECONDS",
           "HOOK_SECONDS", "MAX_CLIPS", "NARRATION_VOLUME", "RESERVED_FAILURE_CLIPS",
           "SAD_VOLUME", "SEGMENT_TIMESCALE", "SHORT_SECONDS", "SLIDE_SECONDS",
           "STING_VOLUME", "build_slides", "build_wrapped_video",
           "clip_label", "default_clip_finder", "default_clip_finder_detailed",
           "escape_drawtext", "parse_clip_print_number", "piper_command",
           "resolve_clip_print_number", "select_clips", "timeline_roles"]
