"""The pasta reel: every failure the camera caught this year, set to music.

A second Wrapped video beside the recap. It takes every clip the
delivery-name join (:func:`wrapped_video.resolve_clip_print_number`)
ties to a failed or pasta print of the year, one clip per print, and
plays them in the year's order:

* an opening card ("Your <year> pasta reel");
* per failure, a label card (the print's name, its date and one roast
  line from the local roast corpus, :mod:`print_handy.core.roast`,
  never an AI model) and then a cut of its clip;
* a closing card with the year's totals: failed prints, the hours and
  grams those prints and the pasta prints burned (summed from the
  records that recorded them), and the pasta count only when watchdog
  events were recorded, never a zero for a count that does not exist.

Each cut sits around the failure. When the print's markers
(:mod:`print_handy.runtime.print_markers`) hold a watchdog alert,
pause, cancel or error inside this run of the print, the marker is
mapped into the clip with the duration-proportional mapping and the cut
is centered there; otherwise the cut is the end of the clip, where a
failure usually is. A clip shorter than its cut holds its last frame.

The reel is fitted to the music track (:func:`plan_reel`): cuts shrink
from CUT_MAX_SECONDS toward CUT_MIN_SECONDS until everything fits; when
even minimum cuts do not, the worst failures stay (most recorded pasta
streak, then most print time burned) and the closing card says how
many more there were. A year with no failure on camera has no reel, and
:data:`EMPTY_REASON` says so.

Everything else is the Wrapped video's own machinery: the music
precedence, the slide segment command, the stream-copy concat and mux
in :func:`wrapped_video.finish_video`, the work folder, the timeouts
and the background job runner (:data:`REEL_JOBS`).
"""
from __future__ import annotations

import functools
import logging
import math
import random
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from ..core import print_stats, roast
from ..runtime import print_markers
from . import wrapped_video as video

logger = logging.getLogger(__name__)

OPEN_SECONDS = 4
LABEL_SECONDS = 3
CLOSE_SECONDS = 6
CUT_MAX_SECONDS = 8
CUT_MIN_SECONDS = 3
# The bundled track's length: the fit when no track can be measured.
REEL_TARGET_SECONDS = 127
# A marker belongs to this run of the print when it falls between the
# record's start and its end plus this slack (the shared marker file of
# a reprinted G-code also holds the earlier runs).
MARKER_SLACK_S = 600
FAILURE_EVENTS = (print_markers.CANCEL, print_markers.ERROR, print_markers.PAUSE)
ROAST_ATTEMPTS = 6
ROAST_WRAP = 34
ROAST_ROWS = 4
FALLBACK_ROAST = "The printer had other plans, and it did not share them."
EMPTY_REASON = ("No pasta reel this year: the camera never caught a single failure. "
                "Suspiciously tidy. We are not mad, just impressed.")


def _number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _seconds(record):
    value = (record or {}).get("print_duration_seconds")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value > 0 else None


def _grams(record):
    value = (record or {}).get("filament_used_g")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value > 0 else None


def _epoch(value):
    """Epoch seconds for an ISO timestamp, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.timestamp()


def _failure_sets(wrapped):
    highlights = (wrapped or {}).get("highlights") or {}
    failed = {n for n in (highlights.get("failed_print_numbers") or []) if _number(n) is not None}
    pasta = {n for n in (highlights.get("pasta_print_numbers") or []) if _number(n) is not None}
    return failed, pasta


def _records_by_number(records):
    index = {}
    for record in records or []:
        number = _number((record or {}).get("print_number"))
        if number is not None:
            index.setdefault(number, record)
    return index


def _streaks(events):
    """{print number: the highest recorded spaghetti streak}."""
    streaks = {}
    for event in events or []:
        if str((event or {}).get("verdict") or "").strip().lower() != "spaghetti":
            continue
        number = _number(event.get("print_number"))
        if number is None:
            continue
        streak = event.get("streak")
        streak = int(streak) if isinstance(streak, (int, float)) and streak > 0 else 1
        streaks[number] = max(streaks.get(number, 0), streak)
    return streaks


def reel_entries(clips, wrapped, records, *, events=None):
    """The reel's candidates: one clip per failed or pasta print, finder order.

    A pasta print is labeled pasta (what the camera saw) even when its
    record also says failed. Each entry carries what the label card and
    the fit need: the record, the print time and grams it burned, its
    recorded pasta streak and its place in the year's order.
    """
    failed, pasta = _failure_sets(wrapped)
    by_number = _records_by_number(records)
    streaks = _streaks(events)
    entries, seen = [], set()
    for clip in clips or []:
        entry = video._clip_entry(clip)
        number = entry.get("print_number")
        if number is None or number in seen or number not in failed | pasta:
            continue
        seen.add(number)
        record = by_number.get(number) or {}
        entries.append({"path": entry["path"], "print_number": number,
                        "kind": "pasta" if number in pasta else "failed",
                        "record": record, "seconds": _seconds(record) or 0,
                        "grams": _grams(record) or 0, "streak": streaks.get(number, 0),
                        "order": len(entries)})
    return entries


def reel_seconds(count, cut):
    """The reel's length for ``count`` failures at ``cut`` seconds each."""
    return OPEN_SECONDS + CLOSE_SECONDS + count * (LABEL_SECONDS + cut)


def plan_reel(entries, track_seconds):
    """{kept, dropped, cut}: fit the failures to ``track_seconds``.

    Cuts start at CUT_MAX_SECONDS and shrink (to a tenth of a second)
    until every failure fits; when even CUT_MIN_SECONDS cuts do not
    fit, the worst failures are kept (most recorded pasta streak, then
    most print time burned) at the minimum cut, in the year's order.
    """
    budget = max(0.0, float(track_seconds) - OPEN_SECONDS - CLOSE_SECONDS)
    fit = max(1, int(budget // (LABEL_SECONDS + CUT_MIN_SECONDS)))
    entries = list(entries)
    if len(entries) > fit:
        worst = sorted(entries, key=lambda e: (-e.get("streak", 0), -e.get("seconds", 0),
                                               e.get("order", 0)))[:fit]
        kept = sorted(worst, key=lambda e: e.get("order", 0))
        return {"kept": kept, "dropped": len(entries) - len(kept), "cut": CUT_MIN_SECONDS}
    if not entries:
        return {"kept": [], "dropped": 0, "cut": CUT_MAX_SECONDS}
    room = math.floor((budget / len(entries) - LABEL_SECONDS) * 10) / 10
    cut = min(CUT_MAX_SECONDS, max(CUT_MIN_SECONDS, room))
    return {"kept": entries, "dropped": 0, "cut": cut}


def failure_moment(record, markers, clip_seconds):
    """Where this run's failure falls in its clip, in seconds, or None.

    Only markers inside this run of the print count. A watchdog alert
    wins (that is the moment the camera saw); else the first pause,
    cancel or error. The mapping is :func:`print_markers.marker_video_time`.
    """
    started = _epoch((record or {}).get("started_at"))
    duration = _seconds(record)
    if started is None or duration is None or not clip_seconds:
        return None
    window = [m for m in markers or [] if isinstance(m, dict)
              and isinstance(m.get("wall_s"), (int, float))
              and started - 60 <= m["wall_s"] <= started + duration + MARKER_SLACK_S]
    window.sort(key=lambda m: m["wall_s"])
    chosen = next((m for m in window if m.get("event") == print_markers.WATCHDOG_ALERT), None)
    if chosen is None:
        chosen = next((m for m in window if m.get("event") in FAILURE_EVENTS), None)
    if chosen is None:
        return None
    return print_markers.marker_video_time(chosen, print_duration_s=duration,
                                           video_duration_s=clip_seconds, started_wall=started)


def cut_start(clip_seconds, cut, moment):
    """Where the cut starts in its clip; None means "the end" of a clip
    whose length is unknown. A known moment centers the cut, kept
    inside the clip; no moment takes the end."""
    if not clip_seconds:
        return None
    if clip_seconds <= cut:
        return 0
    latest = clip_seconds - cut
    if moment is None:
        return round(latest, 2)
    return round(min(max(moment - cut / 2, 0), latest), 2)


def _date_text(record, tz):
    started = _epoch((record or {}).get("started_at"))
    if started is None:
        return "DATE UNKNOWN"
    when = datetime.fromtimestamp(started, tz or timezone.utc)
    return f"{when:%b} {when.day}".upper()


def _fits(line):
    return len(textwrap.wrap(line, width=ROAST_WRAP)) <= ROAST_ROWS


def roast_line(entry, year):
    """One local roast line for a failure, the same every time for this print."""
    record = entry.get("record") or {}
    ctx = roast.build_context(
        record.get("title") or record.get("job_key"), duration_s=_seconds(record),
        material=record.get("filament_material"), color=record.get("filament_color"),
        grams=_grams(record), printer=record.get("printer"),
        print_number=entry.get("print_number"))
    status = "spaghetti" if entry.get("kind") == "pasta" else "failed"
    for attempt in range(ROAST_ATTEMPTS):
        rng = random.Random(f"pasta-reel-{year}-{entry.get('print_number')}-{attempt}")
        line = roast.roast(ctx, status=status, rng=rng)
        if line and _fits(line):
            return line
    return FALLBACK_ROAST


def _card(role, eyebrow, hero, line, duration):
    return {"role": role, "eyebrow": eyebrow, "hero": hero, "line": line,
            "duration": duration, "hero_rows": 2, "line_rows": ROAST_ROWS}


def opening_card(year, count):
    return _card("open", "PASTA REEL", f"Your {year} pasta reel",
                 f"{print_stats.plural(count, 'failure')} caught on camera, one soundtrack. "
                 "No refunds.", OPEN_SECONDS)


def label_card(entry, year, tz=None):
    record = entry.get("record") or {}
    title = (roast.scrub_title(record.get("title"))
             or print_stats.display_title(record.get("title"), record.get("job_key"))
             or f"Print {entry['print_number']}")
    kind = "PASTA" if entry.get("kind") == "pasta" else "FAILED"
    return _card("label", f"{kind} · {_date_text(record, tz)}", title,
                 roast_line(entry, year), LABEL_SECONDS)


def closing_card(wrapped, records, dropped):
    """The year's honest totals (the rules the Wrapped page uses).

    Failed is the year's failed records. Hours and grams are summed
    over the failed and pasta prints' records that recorded them, and a
    total with nothing recorded behind it is left out. Pasta is a count
    only when watchdog events were recorded.
    """
    headline = (wrapped or {}).get("headline") or {}
    failed_numbers, pasta_numbers = _failure_sets(wrapped)
    failed = int(headline.get("failed") or 0)
    recorded = headline.get("pasta_state") == "recorded"
    pasta = int(headline.get("pasta") or 0) if recorded else None
    by_number = _records_by_number(records)
    lost = [by_number[n] for n in sorted(failed_numbers | pasta_numbers) if n in by_number]
    hours = sum(_seconds(r) or 0 for r in lost) / 3600
    grams = sum(_grams(r) or 0 for r in lost)
    if failed:
        hero = print_stats.plural(failed, "failed print")
    elif pasta:
        hero = print_stats.plural(pasta, "pasta print")
    else:
        hero = "The damage report"
    bits = []
    burned = [text for text in (print_stats.hours_text(round(hours, 1)) if hours >= 0.05 else "",
                                f"{round(grams):g} g" if grams >= 1 else "") if text]
    if burned:
        bits.append(" and ".join(burned) + " went down with them.")
    if pasta is None:
        bits.append("Pasta: not recorded yet.")
    elif failed and pasta:
        bits.append(f"{print_stats.plural(pasta, 'pasta print')}.")
    if dropped:
        bits.append(f"Plus {dropped} more that did not make the cut.")
    if not bits:
        bits.append("The printer regrets nothing.")
    return _card("close", "THE DAMAGE", hero, " ".join(bits), CLOSE_SECONDS)


def _default_markers(data):
    try:
        from .. import settings as _settings
        root = _settings.state_dir(data) / "print_markers"
    except Exception as exc:
        logger.debug("no state folder for print markers [PH_WRAPPED_REEL]: %s", exc)
        return lambda record: []

    def markers_for(record):
        key = (record or {}).get("gcode_file") or (record or {}).get("job_key")
        return print_markers.markers(key, root=root) if key else []

    return markers_for


def _clip_command(ffmpeg_bin, entry, start, cut, segment):
    """One cut of a failure clip, padded with its last frame to exactly ``cut``."""
    seek = ["-ss", f"{start:g}"] if start is not None else ["-sseof", f"-{cut:g}"]
    return [ffmpeg_bin, "-y", *seek, "-i", entry["path"], "-t", f"{cut:g}",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                   f"fps={video.CARD_FPS},tpad=stop_mode=clone:stop_duration={cut:g}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "-r", str(video.CARD_FPS),
            "-video_track_timescale", str(video.SEGMENT_TIMESCALE), str(segment)]


def build_pasta_reel(year, wrapped, out_dir, *, data=None, ffmpeg=None, runner=None,
                     find_clips=None, export_silent=False, records=None, tz=None,
                     events=None, probe=None, markers_for=None, progress=None):
    """Compile one pasta reel. Returns {ok, path, notes, roles, cards, seconds}.

    ``wrapped`` is the year's Wrapped payload, ``records`` and
    ``events`` the print records and watchdog events behind it. A year
    with no failure clip returns ``empty`` with :data:`EMPTY_REASON`
    and runs nothing. The seams match :func:`wrapped_video.build_wrapped_video`
    (``runner``, ``find_clips``, ``probe``), plus ``markers_for(record)``
    for the print's stored markers.
    """
    year = int(year)
    out_dir = Path(out_dir)
    finder = find_clips or (lambda y: video.default_clip_finder_detailed(
        data, y, records=records, tz=tz))
    try:
        found = finder(year) or []
    except Exception:
        found = []
    entries = reel_entries(found, wrapped, records, events=events)
    if not entries:
        return {"ok": False, "empty": True, "reason": EMPTY_REASON, "path": None,
                "script_path": None, "notes": ["timelapses: no failed or pasta print on camera"]}
    resolved, named = video.resolve_ffmpeg(ffmpeg, data)
    if resolved is None:
        return {"ok": False, "reason": f"ffmpeg is not available ({named!r} not found), "
                                        "so the pasta reel cannot be compiled",
                "path": None, "script_path": None,
                "notes": [f"ffmpeg: unavailable ({named!r} not found)"]}
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = runner or subprocess.run
    markers_for = markers_for or _default_markers(data)
    return video.in_work_folder(out_dir, f".wrapped-reel-{year}-", lambda work: _compile(
        year, wrapped, out_dir, work, entries, records, resolved, runner, data,
        export_silent, tz, probe, markers_for, progress))


def _compile(year, wrapped, out_dir, work, entries, records, resolved, runner, data,
             export_silent, tz, probe, markers_for, progress):
    notes = []
    run = functools.partial(video._run, runner, cwd=work)
    probe = functools.lru_cache(maxsize=None)(probe or video.runner_probe(runner, resolved, work))
    music = video.resolve_music(data)
    track = probe(music["path"]) if music.get("path") is not None and not export_silent else None
    plan = plan_reel(entries, track or REEL_TARGET_SECONDS)
    kept, cut = plan["kept"], plan["cut"]
    notes.append(f"pasta reel: {len(kept)} of {len(entries)} failure clips, "
                 f"{cut:g} s each, fitted to {track or REEL_TARGET_SECONDS:g} s")
    total_steps = 2 * len(kept) + 4
    done = 0

    def step():
        nonlocal done
        done += 1
        video._notify_progress(progress, done, total_steps)

    files = video.TextFiles(work)
    timeline, cards = [], []

    def add(segment, duration, role):
        timeline.append({"path": segment, "duration": duration, "role": role,
                         "start": sum(e["duration"] for e in timeline)})

    def card_segment(card, name):
        segment = work / f"{name}.mp4"
        run(video._slide_command(resolved, card, segment, None, files),
            timeout=video.step_timeout(card["duration"]))
        cards.append(card)
        add(segment, card["duration"], card["role"])
        step()

    try:
        card_segment(opening_card(year, len(entries)), "reel-open")
        for index, entry in enumerate(kept):
            card_segment(label_card(entry, year, tz), f"reel-label-{index:03d}")
            clip_seconds = probe(entry["path"])
            try:
                markers = markers_for(entry["record"])
            except Exception:
                markers = []
            start = cut_start(clip_seconds, cut,
                              failure_moment(entry["record"], markers, clip_seconds))
            segment = work / f"reel-clip-{index:03d}.mp4"
            run(_clip_command(resolved, entry, start, cut, segment),
                timeout=video.step_timeout(cut))
            add(segment, cut, "clip")
            step()
        card_segment(closing_card(wrapped, records, plan["dropped"]), "reel-close")
        final_path = out_dir / f"pasta-reel-{year}.mp4"
        seconds = video.finish_video(run, resolved, work, timeline, final_path, notes=notes,
                                     export_silent=export_silent, music=music, probe=probe,
                                     step=step)
    except Exception as exc:
        notes.append(f"ffmpeg: failed ({type(exc).__name__}: {exc})")
        return {"ok": False, "reason": f"the pasta reel compile failed ({type(exc).__name__}: {exc})",
                "path": None, "script_path": None, "notes": notes}
    return {"ok": True, "path": str(final_path), "script_path": None, "notes": notes,
            "roles": [entry["role"] for entry in timeline], "cards": cards,
            "seconds": seconds, "kept": len(kept), "dropped": plan["dropped"]}


REEL_JOBS = video.VideoJobs()


__all__ = ["CLOSE_SECONDS", "CUT_MAX_SECONDS", "CUT_MIN_SECONDS", "EMPTY_REASON",
           "LABEL_SECONDS", "OPEN_SECONDS", "REEL_JOBS", "REEL_TARGET_SECONDS",
           "build_pasta_reel", "closing_card", "cut_start", "failure_moment",
           "label_card", "opening_card", "plan_reel", "reel_entries", "reel_seconds",
           "roast_line"]
