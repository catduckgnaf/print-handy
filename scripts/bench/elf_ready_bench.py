#!/usr/bin/env python3
"""Copies chosen -> Ready benchmark for one MakerWorld 3MF (#1779).

Drives the real ``prepared_intake.prepare_link`` flow: the real background slice
(``BackgroundSlice`` with ``jobs_slice.slice_job`` and the real Orca CLI), the
speculative with-supports variant, the support coverage check and the Ready
question composition. Nothing else is real: the site download, the printer
hardware record and every question answer are canned, and no network is
reachable (sockets other than local ones raise). It never uploads or starts a
print: the run ends when the Ready question is posted and is answered with its
Cancel button.

The measured span starts when the copies answer is delivered and ends when the
Ready question reaches the (fake) chat, the same span Discord reports as
"copies chosen -> Ready". ``stages`` carries what TIMING.jsonl recorded for
each slice (the intake's own and the ``variant_supports`` one), so a report
answers: how long did each slice take, how much of that was Orca
(``slicer_s``) against Print Handy's own work (``python_s``, with
``prepare_s`` = inventory + geometry + other prepare work), was the variant
launched, was it identical to the first slice, and what did coverage cost.

Run it where Orca is installed (the test host container, see the docs in
``scripts/bench/README.md``)::

    python scripts/bench/elf_ready_bench.py --source SRC.3mf --job-record JOB.json \\
        --out report.json [--copies 2] [--workdir DIR]

``JOB.json`` is the archived record of a real intake: it supplies the model
inventory, the printer hardware record (service URLs removed) and the color
decision. Exit code 0 means a report with a Ready time was written.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as S

REPORT_VERSION = 1


def _block_network():
    """Offline by construction: only unix sockets and loopback may connect."""
    real_connect = socket.socket.connect

    def guarded(self, address):
        if self.family == getattr(socket, 'AF_UNIX', None):
            return real_connect(self, address)
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ('127.0.0.1', '::1', 'localhost'):
            raise OSError(f'benchmark is offline: refused connection to {host}')
        return real_connect(self, address)

    socket.socket.connect = guarded
    real_getaddrinfo = socket.getaddrinfo

    def no_dns(host, *a, **k):
        if host in ('127.0.0.1', '::1', 'localhost', None):
            return real_getaddrinfo(host, *a, **k)
        raise OSError(f'benchmark is offline: refused name lookup for {host}')
    socket.getaddrinfo = no_dns


def _fake_moonraker(job):
    """A loopback Moonraker that answers the slice-time printer check from the archived record.

    Serves only what ``printer_state.check`` reads: the Klipper config (nozzle, pressure
    advance), the saved variables (screenless calibration authority matching the record)
    and the active spool. Anything else gets an empty result. Returns (server, url).
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hw = job['hardware']
    pa = (hw.get('pressure_advance') or {}).get('value')
    extruder = {'nozzle_diameter': str(hw['nozzle']['diameter_mm'])}
    if pa is not None:
        extruder['pressure_advance'] = str(pa)
    variables = {'display_off': 1, 'load_zoffset': 1, 'gcode_offsets': {'z': hw.get('z_offset_mm')},
                 'use_trash_on_print': 0, 'print_leveling': 0}
    spool_id = (hw.get('spool') or {}).get('spoolman_id')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path
            if 'configfile' in path:
                result = {'status': {'configfile': {'settings': {'extruder': extruder, 'probe': {}}}}}
            elif 'save_variables' in path:
                result = {'status': {'save_variables': {'variables': variables}}}
            elif path.startswith('/server/spoolman/spool_id'):
                result = {'spool_id': spool_id}
            else:
                result = {'status': {}}
            body = json.dumps({'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET  # noqa: N815

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_address[1]}'


def _hardware_record(job, moonraker_url):
    """The archived hardware record with its service URLs on loopback: the fake Moonraker,
    and a closed port for Spoolman (refused at once; the slice path treats it as advisory)."""
    hw = json.loads(json.dumps(job['hardware']))
    hw['moonraker_url'] = moonraker_url
    hw['spoolman_url'] = 'http://127.0.0.1:9'
    return hw


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_VOLATILE = re.compile(r'^; (?:generated by|HEADER_BLOCK|.*(?:time|date)\b.*)', re.I)


def _normalized_sha(path):
    """G-code digest without the lines that differ on every run (timestamps, generator header)."""
    keep = [ln for ln in Path(path).read_text(errors='replace').splitlines() if not _VOLATILE.match(ln)]
    return hashlib.sha256('\n'.join(keep).encode()).hexdigest()


def _gcode_support_flag(path):
    for ln in Path(path).read_text(errors='replace').splitlines():
        if ln.startswith('; enable_support'):
            return ln.split('=', 1)[1].strip()
    return None


def _read_timing(job_folder):
    rows = []
    path = Path(job_folder) / 'TIMING.jsonl'
    if path.is_file():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def slice_rows(timing_rows):
    """One record per slice that finished: the slicer/Python split and its prepare breakdown.

    The intake's own slice has no ``variant`` key; the with-supports slice rows carry
    ``variant: supports`` (BackgroundSlice merges them in).
    """
    out = {}
    for row in timing_rows:
        key = row.get('variant') or 'first'
        rec = out.setdefault(key, {})
        stage = row.get('stage')
        if stage in ('slice_start', 'inventory_done', 'orca_begin', 'slice_done', 'verify_begin', 'verify_done'):
            rec.setdefault(stage, row['t'])
        elif stage == 'orca_finalize_done':
            rec['orca_finalize_done'] = row['t']
            rec['slicer_s'] = row.get('slicer_s')
            rec['python_s'] = row.get('python_s')
        elif stage == 'python_overhead':
            sub = row.get('subphases_s') or {}
            rec['python_subphases_s'] = sub
            rec['prepare_s'] = round(sum(sub.get(k, 0.0) for k in ('inventory_s', 'geometry_s', 'prepare_other_s')), 3)
    for rec in out.values():
        t0, t1 = rec.get('slice_start'), rec.get('slice_done')
        rec['wall_s'] = round(t1 - t0, 3) if t0 and t1 else None
        a, b = rec.get('verify_begin'), rec.get('verify_done')
        rec['verify_s'] = round(b - a, 3) if a and b else None
        for k in ('slice_start', 'inventory_done', 'orca_begin', 'orca_finalize_done', 'slice_done',
                  'verify_begin', 'verify_done'):
            rec.pop(k, None)
    return out


class Recorder:
    """The fake chat: records every question and post with a monotonic time, answers by rule."""

    def __init__(self, clock, copies, slot, color_word):
        self.clock = clock
        self.copies = copies
        self.slot = slot
        self.color_word = color_word.lower()
        self.questions = []
        self.sent = []
        self.t_copies_answered = None
        self.t_ready = None
        self.ready_text = None
        self.snapshot = None  # callable, set by run_bench: called when the Ready question arrives

    def _pick(self, question, choices):
        low = [str(c).lower() for c in choices]
        q = question.lower()
        if q.startswith('ready to slice'):
            for i, c in enumerate(low):
                if 'cancel' in c or 'stop' in c or 'nothing' in c:
                    return i
            raise RuntimeError(f'Ready question has no cancel button: {choices}')
        if 'how many copies' in q:
            word = {1: 'one copy', 2: 'two copies'}.get(self.copies)
            for i, c in enumerate(low):
                if (word and c.startswith(word)) or c.startswith(f'max ({self.copies} '):
                    return i
            raise RuntimeError(f'no button for {self.copies} copies: {choices}')
        for i, c in enumerate(low):
            if self.color_word in c and (f'slot {self.slot}' in c or f'toolhead {self.slot}' in c):
                return i
        for i, c in enumerate(low):
            if self.color_word in c:
                return i
        return 0

    async def send_clarify(self, **kw):
        from print_handy.core import prompts as core_prompts
        now = self.clock()
        question, choices = kw['question'], list(kw.get('choices') or [])
        self.questions.append({'t': now, 'question': question[:400], 'choices': [str(c) for c in choices]})
        if [str(c).lower() for c in choices] == ['stop']:
            return S(success=True)  # a status prompt: nobody taps Stop, the step finishing closes it
        if question.lower().startswith('ready to slice'):
            self.t_ready, self.ready_text = now, question
            if self.snapshot is not None:
                self.snapshot()  # before Cancel: cancelling removes the sliced files
        pick = self._pick(question, choices) if choices else 0
        if 'how many copies' in question.lower():
            self.t_copies_answered = self.clock()
        core_prompts.resolve_gateway_clarify(kw['clarify_id'], choices[pick] if choices else '')
        return S(success=True)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({'t': self.clock(), 'content': str(content)[:300]})
        return S(success=True)


def run_bench(source, job_record, copies, workdir, timeout_s=1500):
    """One intake from link to the Ready question. Returns the report dict."""
    job = json.loads(Path(job_record).read_text())
    # The printer model must be set before print_handy.settings loads (the archived job's own model).
    os.environ.setdefault('PRINT_HANDY__PRINTER__MODEL', job.get('printer_model') or 'flashforge.ad5x')
    from print_handy import settings  # noqa: F401  (import before patching the modules below)
    from print_handy.core import background_slice, prepared_intake, printer_state
    from print_handy.jobs import audit as jobs_audit  # noqa: F401
    from print_handy.jobs import common as jobs_common
    from print_handy.jobs import sources as jobs_sources

    workdir = Path(workdir)
    archive = workdir / 'archive'
    receipts = workdir / 'receipts'
    archive.mkdir(parents=True, exist_ok=True)
    selection = (job['color_decisions'].get('selection') or {})
    slot, color = selection.get('slot'), selection.get('color') or 'red'
    if not isinstance(slot, int):
        raise SystemExit(f'color decision slot must be an integer, got {slot!r}')

    fake_mr, mr_url = _fake_moonraker(job)
    hardware = _hardware_record(job, mr_url)
    record_root = workdir / 'hardware-state'
    printer_state.STATE_ROOT = str(record_root)
    # An env override, not just STATE_ROOT: the slice runs in worker processes that only inherit the environment.
    record = record_root / 'printer_hardware.json'
    os.environ[printer_state.HARDWARE_ENV] = str(record)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(hardware))
    printer_state.save = lambda state, printer_id=None: None  # never write a hardware record
    jobs_common.ARCHIVE = archive

    inventory = {k: job[k] for k in ('site', 'model_id', 'title', 'slug', 'license', 'summary', 'sources') if k in job}
    jobs_sources.maker_inventory = lambda url: json.loads(json.dumps(inventory))

    def fake_download(_job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / Path(source).name
        shutil.copyfile(source, out)
        return out
    jobs_sources.maker_download = fake_download

    clock = time.monotonic
    chat = Recorder(clock, copies, slot, color)
    event = S(source=S(chat_id='bench', thread_id=None, user_id='bench'), message_id='bench-1', text=job['url'])
    # Keep Orca's work off a Flatpak/other host command: use the configured one unchanged.
    started = clock()
    wall0 = time.time()
    state = json.loads(json.dumps(hardware))
    snap = workdir / 'ready-snapshot'

    def snapshot():
        """Copy the job folder as it stands at Ready: Cancel then deletes the sliced files."""
        for p in archive.iterdir():
            if (p / 'JOB.json').is_file():
                shutil.copytree(p, snap, ignore=shutil.ignore_patterns('source'), dirs_exist_ok=True)
                return
    chat.snapshot = snapshot
    outcome = {}

    async def go():
        try:
            outcome['result'] = await asyncio.wait_for(
                prepared_intake.prepare_link(event, chat, state, receipts, timeout=timeout_s), timeout_s)
        finally:
            # Background slices and prompt waiters outlive prepare_link; cancel them so the loop can close.
            me = asyncio.current_task()
            for task in asyncio.all_tasks():
                if task is not me:
                    task.cancel()
            await asyncio.sleep(0)
    error = None
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(go())
    except BaseException as exc:  # noqa: BLE001 - the report records how the run ended
        import traceback
        error = f'{type(exc).__name__}: {exc}'
        outcome['traceback'] = traceback.format_exc()[-1500:]
    ended = clock()
    fake_mr.shutdown()
    try:
        loop.close()
    except Exception:  # noqa: BLE001
        pass

    folders = [p for p in archive.iterdir() if (p / 'JOB.json').is_file()]
    folder = snap if snap.is_dir() else (folders[0] if folders else None)
    timing = _read_timing(folder) if folder else []
    slices = slice_rows(timing)

    gcodes = {}
    if folder:
        for g in sorted(folder.rglob('*.gcode')):
            rel = str(g.relative_to(folder))
            gcodes[rel] = {'sha256': _sha(g), 'normalized_sha256': _normalized_sha(g),
                           'enable_support': _gcode_support_flag(g), 'bytes': g.stat().st_size}
    cov = [r for r in timing if r.get('stage') == 'preview_support_coverage_done']
    first_gcodes = {k: v for k, v in gcodes.items() if not k.startswith(('variant_supports', 'adopted_supports'))}
    variant_gcodes = {k: v for k, v in gcodes.items() if k.startswith(('variant_supports', 'adopted_supports'))}
    identical = None
    if first_gcodes and variant_gcodes:
        a = {v['normalized_sha256'] for v in first_gcodes.values()}
        b = {v['normalized_sha256'] for v in variant_gcodes.values()}
        identical = bool(a & b) and a == b
    variants = {
        'launched': 'supports' in slices or bool(variant_gcodes),
        'why_not_launched': None,
        'supports_variant_identical_to_first': identical,
        'first_enable_support': sorted({v['enable_support'] for v in first_gcodes.values() if v['enable_support']}),
        'variant_enable_support': sorted({v['enable_support'] for v in variant_gcodes.values() if v['enable_support']}),
    }
    copies_to_ready = None
    if not variants['launched'] and folder:
        # Say why: the slice record's own support facts (the designer's setting kept, no floating regions).
        try:
            rec = json.loads((folder / 'JOB.json').read_text())
            prov = rec.get('provenance') or {}
            setting = ((prov.get('conversion_provenance') or {}).get('settings') or {}).get('enable_support') or {}
            variants['why_not_launched'] = {
                'slice_warnings_support_needed': (prov.get('slice_warnings') or {}).get('support_needed'),
                'enable_support_selected_reason': setting.get('selected_reason'),
                'enable_support_selected_value': setting.get('selected_value'),
                'support_decision': rec.get('support_decision'),
            }
        except (OSError, ValueError):
            pass
    if chat.t_copies_answered is not None and chat.t_ready is not None:
        copies_to_ready = round(chat.t_ready - chat.t_copies_answered, 3)
    report = {
        'report_version': REPORT_VERSION,
        'source': str(source),
        'source_sha256': _sha(source),
        'copies': copies,
        'selection': selection,
        'head': os.environ.get('BENCH_HEAD'),
        'host_cpus': os.cpu_count(),
        'started_epoch': wall0,
        'error': error,
        'prepare_result': json.loads(json.dumps(outcome.get('result'), default=str)),
        'traceback': outcome.get('traceback'),
        'sent': chat.sent,
        'reached_ready': chat.t_ready is not None,
        'copies_to_ready_s': copies_to_ready,
        'link_to_ready_s': round(chat.t_ready - started, 3) if chat.t_ready is not None else None,
        'run_total_s': round(ended - started, 3),
        'slices': slices,
        'variants': variants,
        'coverage': ({'elapsed_s': cov[-1].get('elapsed_s'), 'checked': cov[-1].get('checked')} if cov else None),
        'gcodes': gcodes,
        'questions': [{**q, 't': round(q['t'] - started, 3)} for q in chat.questions],
        'timing_stages': [{k: v for k, v in r.items() if k != 'pid'} for r in timing],
        'ready_text': chat.ready_text,
    }
    if background_slice._RUNNERS:
        background_slice._RUNNERS.clear()
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--source', required=True, help='the 3MF the intake would download')
    ap.add_argument('--job-record', required=True, help='archived JOB.json of the real intake')
    ap.add_argument('--out', required=True, help='JSON report path')
    ap.add_argument('--copies', type=int, default=2)
    ap.add_argument('--workdir', help='scratch folder (default: a fresh temp dir, deleted afterwards)')
    ap.add_argument('--keep', action='store_true', help='keep the scratch folder')
    ap.add_argument('--timeout', type=int, default=1500)
    args = ap.parse_args(argv)
    _block_network()
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix='elf-ready-bench-'))
    work.mkdir(parents=True, exist_ok=True)
    try:
        report = run_bench(args.source, args.job_record, args.copies, work, args.timeout)
    finally:
        if not args.workdir and not args.keep:
            pass  # removed below, after the report is written
    Path(args.out).write_text(json.dumps(report, indent=2) + '\n')
    summary = {k: report[k] for k in ('reached_ready', 'copies_to_ready_s', 'link_to_ready_s', 'error')}
    print(json.dumps(summary))
    if not args.workdir and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if report['reached_ready'] and report['copies_to_ready_s'] is not None else 1


if __name__ == '__main__':
    sys.exit(main())
