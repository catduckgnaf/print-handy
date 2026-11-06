"""The Wrapped video compile (#956): stat cards + timelapse clips + script.

On demand only, local only. Cards are generated with ffmpeg's lavfi
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
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MAX_CLIPS = 6
CLIP_SECONDS = 4
CARD_SECONDS = 3


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


def default_clip_finder(data, year):
    """MP4s under the local timelapse candidates whose mtime is in ``year``."""
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
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.mp4"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if mtime.year == int(year):
                found.append(path)
    return sorted(found, key=lambda p: p.stat().st_mtime)[:MAX_CLIPS]


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


def _card_texts(wrapped):
    headline = wrapped.get("headline") or {}
    texts = [f"Print Handy Wrapped {wrapped.get('year')}"]
    if headline.get("total_prints"):
        texts.append(f"{headline['total_prints']} prints")
    if headline.get("total_hours"):
        texts.append(f"{headline['total_hours']:g} hours printing")
    if headline.get("total_filament_used_g"):
        texts.append(f"{headline['total_filament_used_kg']:g} kg of filament")
    if headline.get("success_rate") is not None:
        texts.append(f"Success rate {round(headline['success_rate'] * 100):g}%")
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
    finder = find_clips or (lambda y: default_clip_finder(data, y))
    try:
        clips = [Path(c) for c in (finder(year) or [])][:MAX_CLIPS]
    except Exception:
        clips = []
    if not clips:
        notes.append("timelapses: none found")
    work = out_dir / f".wrapped-{year}-parts"
    work.mkdir(parents=True, exist_ok=True)
    segments = []
    try:
        for index, text in enumerate(_card_texts({**wrapped, "script": script})):
            segment = work / f"card-{index:02d}.mp4"
            _run(runner, [resolved, "-y", "-f", "lavfi", "-i",
                          f"color=c=0x12110F:s=1280x720:d={CARD_SECONDS}",
                          "-vf", f"drawtext=text='{escape_drawtext(text)}':"
                                 "fontcolor=white:fontsize=40:"
                                 "x=(w-text_w)/2:y=(h-text_h)/2",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p", str(segment)])
            segments.append(segment)
        for index, clip in enumerate(clips):
            segment = work / f"clip-{index:02d}.mp4"
            _run(runner, [resolved, "-y", "-i", str(clip), "-t", str(CLIP_SECONDS),
                          "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                                 "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(segment)])
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


__all__ = ["MAX_CLIPS", "build_wrapped_video", "default_clip_finder", "escape_drawtext"]
