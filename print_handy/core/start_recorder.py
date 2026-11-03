"""Opt-in recording of a live print start, for the #843 fixtures and the #842 checklist.

Off unless PRINT_HANDY_RECORD_STARTS=1. When on, a start path calls `begin()` immediately before
its start POST (SET_ZCOLOR on the AD5X native route, /printer/print/start on the generic Moonraker
route) and `finish()` when the start settles. Between the two, a background thread polls

    /printer/objects/query?print_stats&extruder&heater_bed&zmod_color&idle_timeout&virtual_sdcard&webhooks

every 2 s (zmod_color only on the native route), plus the gcode store, and appends every response
with a monotonic and a wall timestamp to <state>/start-recordings/<UTC stamp>-<remote stem>.jsonl.
A `.summary.json` beside it holds the outcome once the recording ends.

print-now is a short-lived process and usually returns while the AD5X is still preparing (its
wait is 25 s; native preparation takes minutes). When `finish()` reports an unsettled outcome
(preparing, unconfirmed, unknown), the recording is handed to a detached follower process
(`python -m print_handy.core.start_recorder follow <jsonl>`) that keeps polling the same query until
the printer prints this file, abandons the start, reports error/cancelled, or the 15 min cap.

Safety contract, enforced here rather than at call sites:
  * the recorder only ever sends HTTP GET (its own `_get`; it is never handed a POST helper);
  * every public method swallows its own errors: a recording problem never reaches the start path;
  * polling runs on a daemon thread, so it never delays the caller, and it stops itself (finish,
    an observed settle, or the cap).

`python -m print_handy.core.start_recorder export <jsonl> --fixture preparing|prep_aborted --out DIR`
turns a recording into the #843 fixture file (objects_query.<fixture>.json), with file names scrubbed
the way tests/fixtures/printers/ad5x_zmod/README.md describes.
"""
from __future__ import annotations

import argparse
import atexit
import copy
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import launch_state

logger = logging.getLogger(__name__)

ENV = "PRINT_HANDY_RECORD_STARTS"
INTERVAL_S = 2.0
CAP_S = 15 * 60
GET_TIMEOUT_S = 10.0
NATIVE_OBJECTS = ("print_stats", "extruder", "heater_bed", "zmod_color", "idle_timeout", "virtual_sdcard", "webhooks")
GENERIC_OBJECTS = tuple(o for o in NATIVE_OBJECTS if o != "zmod_color")
STORE_PATH = "/server/gcode_store?count=100"
# Outcomes after which the printer has nothing more to show about this start.
SETTLED = frozenset({"printing", "abandoned", "refused", "error", "cancelled", "failed", "timeout"})
FIXTURES = {"preparing": "printing", "prep_aborted": "abandoned"}  # fixture name -> required outcome
FORMAT_VERSION = 1


def enabled(environ=None):
    """Whether PRINT_HANDY_RECORD_STARTS asks for recordings (1/true/yes/on). Default off."""
    value = (environ if environ is not None else os.environ).get(ENV, "")
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def recordings_dir():
    from .. import settings
    return settings.state_dir() / "start-recordings"


def query_path(native):
    return "/printer/objects/query?" + "&".join(NATIVE_OBJECTS if native else GENERIC_OBJECTS)


def _stamp():
    return {"mono": round(time.monotonic(), 3), "wall": round(time.time(), 3)}


def _stem(remote):
    name = Path(str(remote or "start")).name
    for suffix in (".gcode.3mf", ".gcode", ".3mf"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:80].strip("-.") or "start"


def _get(base_url, path, timeout=GET_TIMEOUT_S):
    """The recorder's only printer request: an HTTP GET returning parsed JSON."""
    req = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _store_mark(entry):
    return (entry.get("message"), entry.get("time")) if isinstance(entry, dict) else None


class _Observer:
    """Classifies the start from the polled statuses alone, like _wait_printing does."""

    def __init__(self, remote, prep_seen=False, stale=None):
        self.remote, self.prep_seen = remote, prep_seen
        # An error/cancelled job already on the printer when polling began (the generic route lets a
        # start follow one) is not this start's outcome; only a terminal state that differs from it,
        # or one reached after the printer left it, is. [state, filename], or None.
        self.stale = list(stale) if stale else None
        self._first = stale is None

    def see(self, status):
        """The settled outcome this status shows, or None while the start is still open."""
        ps = status.get("print_stats") or {}
        state = ps.get("state")
        if state == "printing" and launch_state.same_remote(ps.get("filename"), self.remote):
            return "printing"
        if state in ("error", "cancelled"):
            key = [state, ps.get("filename")]
            if self._first:
                self.stale = key
            self._first = False
            if key != self.stale:
                return state
        else:
            self._first = False
            self.stale = None
        targets = []
        for name in ("extruder", "heater_bed"):
            try:
                targets.append(float((status.get(name) or {}).get("target")))
            except (TypeError, ValueError):
                return None
        if any(t > 0 for t in targets):
            self.prep_seen = True
        elif self.prep_seen and state == "standby":
            return "abandoned"
        return None


class _Null:
    """The recorder when recording is off: every call is a no-op."""
    path = None

    def note(self, kind, **fields):
        pass

    def watch_lease(self, lease):
        return lease

    def finish(self, outcome, **fields):
        pass


NULL = _Null()


class StartRecorder:
    def __init__(self, path, base_url, remote, *, native, header, watermark=None, store_after=None,
                 interval_s=None, cap_s=None, prep_seen=False, spawn=None):
        self.path = Path(path)
        self.summary_path = self.path.with_name(self.path.name[:-len(".jsonl")] + ".summary.json")
        self.base_url, self.remote, self.native = base_url, remote, native
        self.query = query_path(native)
        self.interval_s = INTERVAL_S if interval_s is None else interval_s
        self.cap_s = CAP_S if cap_s is None else cap_s
        self.t0 = time.monotonic()
        self.deadline_wall = time.time() + self.cap_s
        self.observer = _Observer(remote, prep_seen=prep_seen)
        self.zmod_version = header.get("zmod_version")
        self.watermark = watermark
        self._store_after = store_after
        self._spawn = spawn
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False  # no more lines from this process
        self.samples = 0
        self.observed = None
        self.outcome = None
        self.store_lines = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write({"kind": "header", "format": FORMAT_VERSION, "remote": remote, "native": native,
                     "query": self.query, "moonraker_url": base_url, "interval_s": self.interval_s,
                     "cap_s": self.cap_s, "deadline_wall": round(self.deadline_wall, 3),
                     "watermark": list(watermark) if watermark else None, **header})
        self._thread = None

    # Writing -------------------------------------------------------------------------------------
    def _write(self, record, force=False):
        with self._lock:
            if self._closed and not force:
                return False
            line = {**_stamp(), "t_s": round(time.monotonic() - self.t0, 3), **record}
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")
            return True

    def note(self, kind, **fields):
        """One event line (start command, receipt, lease phase, ...). Never raises."""
        try:
            self._write({"kind": kind, **fields})
        except Exception as exc:
            logger.debug("start recorder note failed: %s", exc)

    # Polling -------------------------------------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._run, name="start-recorder", daemon=True)
        self._thread.start()
        return self

    def poll_once(self):
        """One sample of the objects query and the gcode store. Returns the observed outcome, if settled."""
        try:
            response = _get(self.base_url, self.query)
            self.samples += 1
            self._write({"kind": "sample", "seq": self.samples, "ok": True, "response": response})
            status = (response.get("result") or {}).get("status") or {}
            seen = self.observer.see(status)
        except Exception as exc:
            self._write({"kind": "sample", "seq": self.samples + 1, "ok": False,
                         "error": f"{type(exc).__name__}: {exc}"[:300]})
            seen = None
        self._poll_store()
        return seen

    def _poll_store(self):
        if self.watermark is None or self._store_after is None:
            return
        try:
            entries = _get(self.base_url, STORE_PATH)["result"]["gcode_store"]
            if not isinstance(entries, list):
                return
            new = self._store_after(entries, self.watermark)
            if entries:
                self.watermark = (len(entries), _store_mark(entries[-1]))
            if new:
                self.store_lines.extend(e.get("message") for e in new if isinstance(e, dict))
                self._write({"kind": "gcode_store", "entries": new})
        except Exception as exc:
            logger.debug("start recorder store read failed: %s", exc)

    def _read_zmod_version(self):
        """The installed ZMod version, read once on this thread (GET only) when the start path had not."""
        try:
            from .. import firmware_update

            def get_only(method, path):
                if method != "GET":
                    raise RuntimeError("the start recorder only reads")
                return _get(self.base_url, path)
            component = firmware_update.fetch_status(get_only).component("zmod")
            self.note("zmod_version", version=getattr(component, "current", None) if component else None)
        except Exception as exc:
            self.note("zmod_version", version=None, error=f"{type(exc).__name__}: {exc}"[:200])

    def _run(self):
        try:
            if self.native and not self.zmod_version:
                self._read_zmod_version()
            while not self._stop.is_set():
                seen = self.poll_once()
                if not seen and time.time() >= self.deadline_wall:
                    seen = "timeout"
                if seen and not self._stop.is_set():
                    # Polling stops here; the recording itself stays open for the start path's own
                    # outcome line (finish), which then closes it.
                    self.observed = seen
                    self.note("observed", outcome=seen)
                    return
                self._stop.wait(self.interval_s)
        except Exception as exc:  # the thread must never die loudly
            logger.debug("start recorder thread failed: %s", exc)

    # Lease ---------------------------------------------------------------------------------------
    def watch_lease(self, lease):
        """Record this lease's later phase changes (uncertain, released) as they happen."""
        try:
            for name in ("uncertain", "release"):
                real = getattr(lease, name)

                def logged(*args, _real=real, _name=name, **kwargs):
                    self.note("lease", phase="released" if _name == "release" else _name,
                              detail=(str(args[0])[:300] if args else None))
                    return _real(*args, **kwargs)
                setattr(lease, name, logged)
            self.note("lease", phase="submitting", token=getattr(lease, "token", None))
        except Exception as exc:
            logger.debug("start recorder lease watch failed: %s", exc)
        return lease

    # Ending --------------------------------------------------------------------------------------
    def finish(self, outcome, **fields):
        """The start path's own outcome. A settled one ends the recording; an open one hands it to a
        detached follower that keeps polling until the printer settles or the cap. Never raises."""
        try:
            if self.outcome is not None:
                return
            self.outcome = str(outcome)
            self.note("outcome", source="start_path", outcome=self.outcome, **fields)
            if self.outcome in FIXTURES.values() and not self.observed and not self._closed:
                # The start path saw the printer settle between two polls: hand off so the
                # recording still captures the settled frame its outcome claims.
                self._handoff()
                return
            if self.outcome in SETTLED or self.observed or self._closed:
                self._stop.set()
                self._end("start path " + self.outcome)
                return
            self._handoff()
        except Exception as exc:
            logger.debug("start recorder finish failed: %s", exc)

    def _handoff(self):
        with self._lock:
            self._stop.set()
            self._closed = True
        state = {"prep_seen": self.observer.prep_seen, "stale": self.observer.stale,
                 "watermark": list(self.watermark) if self.watermark else None,
                 "samples": self.samples, "store_lines": self.store_lines, "outcome": self.outcome}
        # Written before the spawn: the follower reads this line to continue where this process stopped.
        self._write({"kind": "handoff", "state": state}, force=True)
        try:
            proc = (self._spawn or _spawn_follower)(self.path)
            self._write({"kind": "handoff_spawned", "pid": getattr(proc, "pid", None)}, force=True)
        except Exception as exc:
            self._write({"kind": "handoff_failed", "error": str(exc)[:300], "state": state}, force=True)
            self._summarize("handoff failed")

    def _end(self, reason):
        with self._lock:
            already = self._closed
            self._closed = True
        if already:
            return
        self._write({"kind": "end", "reason": reason}, force=True)
        self._summarize(reason)

    def _summarize(self, reason):
        try:
            write_summary(self.path, reason)
        except Exception as exc:
            logger.debug("start recorder summary failed: %s", exc)

    def _atexit(self):
        if self.outcome is None and not self._closed:
            self.finish("process_exit")


def begin(st, remote, *, command, method, native, delivery=None, zmod_version=None, watermark=None,
          store_after=None, baseline=None, environ=None):
    """A running recorder for one start, or the no-op NULL when recording is off or cannot start.

    Call immediately before the start POST. `watermark` is the #848 gcode-store watermark captured
    for the same POST and `store_after` its reader (model_job._entries_after_watermark)."""
    if not enabled(environ):
        return NULL
    try:
        base_url = str((st or {}).get("moonraker_url") or "").strip()
        if not base_url:
            return NULL
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = recordings_dir() / f"{stamp}-{_stem(remote)}.jsonl"
        if delivery and isinstance(delivery, dict):
            zmod_version = zmod_version or delivery.get("zmod_version")
        header = {"method": method, "command": command,
                  "delivery_format": (delivery or {}).get("format") if isinstance(delivery, dict) else None,
                  "delivery": delivery,
                  "delivery_suffix": ".gcode.3mf" if str(remote).endswith(".gcode.3mf") else Path(str(remote)).suffix,
                  "zmod_version": zmod_version, "display_off": (st or {}).get("display_off"),
                  "printer": (st or {}).get("printer"), "pid": os.getpid()}
        rec = StartRecorder(path, base_url, remote, native=native, header=header, watermark=watermark,
                            store_after=store_after)
        if baseline is not None:
            rec.note("baseline", note="status read by the start path before the upload", status=baseline)
        atexit.register(rec._atexit)
        print(f"START RECORDING: {path}", flush=True)
        return rec.start()
    except Exception as exc:
        logger.warning("start recording could not begin: %s", exc)
        return NULL


# Follower ----------------------------------------------------------------------------------------

def _spawn_follower(path):
    argv = [sys.executable, "-m", "print_handy.core.start_recorder", "follow", str(path)]
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def read_recording(path):
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            try:
                lines.append(json.loads(raw))
            except ValueError:
                continue  # a torn last line from a killed process
    return lines


def follow(path, *, interval_s=None, store_after=None):
    """Continue a handed-off recording in this process until the printer settles or the cap."""
    lines = read_recording(path)
    header = next(line for line in lines if line.get("kind") == "header")
    handoff = next((line for line in reversed(lines) if line.get("kind") == "handoff"), {})
    state = handoff.get("state") or {}
    if store_after is None:
        try:
            from .model_job import _entries_after_watermark as store_after
        except Exception:
            store_after = None
    rec = StartRecorder.__new__(StartRecorder)
    rec.path = Path(path)
    rec.summary_path = rec.path.with_name(rec.path.name[:-len(".jsonl")] + ".summary.json")
    rec.base_url, rec.remote, rec.native = header["moonraker_url"], header["remote"], header["native"]
    rec.query = header["query"]
    rec.interval_s = header.get("interval_s", INTERVAL_S) if interval_s is None else interval_s
    rec.cap_s = header.get("cap_s", CAP_S)
    rec.deadline_wall = float(header.get("deadline_wall") or time.time() + CAP_S)
    # CLOCK_MONOTONIC is system-wide on Linux, so t_s continues the print-now process's timeline.
    rec.t0 = float(header.get("mono") or time.monotonic()) - float(header.get("t_s") or 0)
    rec.observer = _Observer(rec.remote, prep_seen=bool(state.get("prep_seen")), stale=state.get("stale"))
    rec.observer._first = False  # the print-now process already took the first sample
    rec.zmod_version = "read"  # the print-now process already read (or tried to read) it
    rec.watermark = tuple(state["watermark"]) if state.get("watermark") else None
    if rec.watermark and isinstance(rec.watermark[1], list):
        rec.watermark = (rec.watermark[0], tuple(rec.watermark[1]))
    rec._store_after, rec._spawn = store_after, None
    rec._lock, rec._stop, rec._closed = threading.Lock(), threading.Event(), False
    rec.samples = int(state.get("samples") or 0)
    rec.observed, rec.outcome = None, state.get("outcome")
    rec.store_lines = list(state.get("store_lines") or [])
    rec._thread = None
    rec.note("follower", pid=os.getpid())
    rec._run()
    rec._end("follower observed " + str(rec.observed))
    return rec


# Summary -----------------------------------------------------------------------------------------

def summarize(lines):
    header = next((line for line in lines if line.get("kind") == "header"), {})
    samples = [line for line in lines if line.get("kind") == "sample"]
    ok = [line for line in samples if line.get("ok")]
    outcome = next((line for line in reversed(lines) if line.get("kind") == "outcome"), {})
    observed = next((line for line in reversed(lines) if line.get("kind") == "observed"), {})
    version_note = next((line for line in reversed(lines) if line.get("kind") == "zmod_version"), {})
    store = [e.get("message") for line in lines if line.get("kind") == "gcode_store"
             for e in line.get("entries") or [] if isinstance(e, dict)]
    first_heat = first_printing = None
    for line in ok:
        status = ((line.get("response") or {}).get("result") or {}).get("status") or {}
        try:
            heating = any(float((status.get(k) or {}).get("target") or 0) > 0 for k in ("extruder", "heater_bed"))
        except (TypeError, ValueError):
            heating = False
        if heating and first_heat is None:
            first_heat = line.get("t_s")
        if (status.get("print_stats") or {}).get("state") == "printing" and first_printing is None:
            first_printing = line.get("t_s")
    return {
        "recording": None, "remote": header.get("remote"), "method": header.get("method"),
        "command": header.get("command"), "delivery_format": header.get("delivery_format"),
        "delivery_suffix": header.get("delivery_suffix"),
        "zmod_version": header.get("zmod_version") or version_note.get("version"),
        "display_off": header.get("display_off"), "started_wall": header.get("wall"),
        "start_path_outcome": outcome.get("outcome"), "start_path_phase": outcome.get("phase"),
        "observed_outcome": observed.get("outcome"),
        "outcome": observed.get("outcome") or outcome.get("outcome"),
        "receipt": next(({k: line.get(k) for k in ("http_status", "error", "receipt")}
                         for line in lines if line.get("kind") == "receipt"), None),
        "lease_phases": [line.get("phase") for line in lines if line.get("kind") == "lease"],
        "handed_off": (any(line.get("kind") == "handoff" for line in lines)
                       and not any(line.get("kind") == "handoff_failed" for line in lines)),
        "samples": len(samples), "failed_samples": len(samples) - len(ok),
        "first_heater_target_s": first_heat, "first_printing_s": first_printing,
        "duration_s": lines[-1].get("t_s") if lines else None,
        "gcode_store_after_watermark": store,
        "bang_lines": [m for m in store if isinstance(m, str) and m.lstrip().startswith("!!")],
    }


def write_summary(path, reason=None):
    path = Path(path)
    summary = summarize(read_recording(path))
    summary["recording"] = path.name
    summary["ended"] = reason
    out = path.with_name(path.name[:-len(".jsonl")] + ".summary.json")
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


# Export ------------------------------------------------------------------------------------------

_PRIVATE = [  # the scrub check tests/replay/test_fixtures_scrubbed.py enforces on every fixture
    (re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+(?:\.\d+)?\b"), "private IP address"),
    (re.compile(r"(?:api[_-]?key|password|passwd|ssid|(?:access|auth)[_-]?token|serial(?:[_-]?number)?)[\"']?\s*[:=]", re.I),
     "credential or serial assignment"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "uuid"),
    (re.compile(r"\b[0-9A-F]{2}(?::[0-9A-F]{2}){5}\b", re.I), "MAC address"),
]


class _Names:
    """Real file names -> Replay-Part-N_plate_1_<sha256>.gcode, the README's scrub scheme."""

    def __init__(self, first=1):
        self.map, self.next = {}, first

    def __call__(self, name):
        if not name:
            return name
        prefix = ""
        for lead in ("/usr/data/gcodes/", "3mf_print/"):  # kept, as the recorded printing fixture keeps them
            if name.startswith(lead):
                prefix += lead
                name = name[len(lead):]
        suffix = next((s for s in (".gcode.3mf", ".gcode", ".3mf") if name.endswith(s)), "")
        base = name[:-len(suffix)] if suffix else name
        if base not in self.map:
            digest = hashlib.sha256(f"replay-recording-{self.next}".encode()).hexdigest()
            self.map[base] = f"Replay-Part-{self.next}_plate_1_{digest}"
            self.next += 1
        return prefix + self.map[base] + suffix


def _scrub_status(status, names):
    status = copy.deepcopy(status)
    ps = status.get("print_stats")
    if isinstance(ps, dict) and ps.get("filename"):
        ps["filename"] = names(ps["filename"])
    sd = status.get("virtual_sdcard")
    if isinstance(sd, dict) and sd.get("file_path"):
        sd["file_path"] = names(sd["file_path"])
    return status


def _scrub_command(command, names):
    """The start command with its file name replaced: SET_ZCOLOR FILENAME=... or print/start <file>."""
    text = re.sub(r"FILENAME=(\S+)", lambda m: "FILENAME=" + names(m.group(1)), str(command or ""))
    text = re.sub(r"(print/start\s+)(\S+)", lambda m: m.group(1) + names(m.group(2)), text)
    return text or None


def export_fixture(path, fixture, out_dir, *, force=False, first_name=1):
    """objects_query.<fixture>.json from a recording, in the fixture tree's shape.

    Each frame is one polled response exactly as the printer sent it ({"result": {"eventtime",
    "status"}}, the shape of objects_query.printing.json), plus `t_s` seconds since the start POST.
    File names are replaced as the fixture README describes; the result is refused if it still
    holds anything the scrub test forbids."""
    if fixture not in FIXTURES:
        raise SystemExit(f"unknown fixture {fixture!r}; one of {', '.join(FIXTURES)}")
    lines = read_recording(path)
    summary = summarize(lines)
    wanted = FIXTURES[fixture]
    # The polled frames must themselves reach the outcome, not just the start path's own line.
    if summary["observed_outcome"] != wanted and not force:
        raise SystemExit(f"REFUSED: this recording's frames reach {summary['observed_outcome']!r}, not "
                         f"{wanted!r}, so it is not a {fixture} recording. Pass --force to export it anyway.")
    names = _Names(first_name)
    frames = []
    for line in lines:
        if line.get("kind") != "sample" or not line.get("ok"):
            continue
        result = (line.get("response") or {}).get("result") or {}
        frames.append({"t_s": line.get("t_s"),
                       "result": {"eventtime": result.get("eventtime"),
                                  "status": _scrub_status(result.get("status") or {}, names)}})
    if not frames:
        raise SystemExit("REFUSED: the recording holds no readable samples.")
    header = next((line for line in lines if line.get("kind") == "header"), {})
    recorded_on = datetime.fromtimestamp(float(header.get("wall") or 0), timezone.utc).strftime("%Y-%m-%d")
    doc = {"recorded": {"date": recorded_on, "zmod_version": summary["zmod_version"],
                        "display_off": header.get("display_off"), "query": header.get("query"),
                        "interval_s": header.get("interval_s"), "outcome": summary["outcome"],
                        "start_command": _scrub_command(header.get("command"), names),
                        "delivery_suffix": header.get("delivery_suffix"),
                        "first_heater_target_s": summary["first_heater_target_s"],
                        "first_printing_s": summary["first_printing_s"]},
           "frames": frames}
    text = json.dumps(doc, indent=2) + "\n"
    for pattern, what in _PRIVATE:
        found = pattern.search(text)
        if found:
            raise SystemExit(f"REFUSED: the export still holds a {what} ({found.group(0)!r}); scrub it first.")
    out = Path(out_dir) / f"objects_query.{fixture}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m print_handy.core.start_recorder")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("follow", help="continue a handed-off recording (started by print-now)")
    f.add_argument("recording")
    e = sub.add_parser("export", help="write objects_query.<fixture>.json from a recording")
    e.add_argument("recording")
    e.add_argument("--fixture", required=True, choices=sorted(FIXTURES))
    e.add_argument("--out", required=True, help="e.g. tests/fixtures/printers/ad5x_zmod/")
    e.add_argument("--force", action="store_true", help="export even if the outcome does not match the fixture")
    s = sub.add_parser("summary", help="rewrite and print a recording's summary")
    s.add_argument("recording")
    args = parser.parse_args(argv)
    if args.cmd == "follow":
        follow(args.recording)
    elif args.cmd == "export":
        out = export_fixture(args.recording, args.fixture, args.out, force=args.force)
        doc = json.loads(out.read_text())["recorded"]
        print(f"wrote {out}")
        print(f"README row: | `{out.name}` | `/printer/objects/query` ({doc['query'].split('?', 1)[-1]}) "
              f"every {doc['interval_s']:g} s from the start POST | recorded {doc['date']}, "
              f"ZMod {doc['zmod_version'] or '<version>'}, outcome {doc['outcome']} |")
    else:
        out = write_summary(args.recording, "rewritten")
        print(out.read_text())


if __name__ == "__main__":
    main()
