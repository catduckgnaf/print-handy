"""One background slice per intake, started while the confirm question is still on screen.

The slice is bound to the complete slice recipe (slice_recipe.py). A later call with a different
binding cancels the running slice and discards its output. Cancelling stops the slicer worker's
process group (SIGTERM, then SIGKILL after a grace period) and waits until it is gone, so nothing
writes after the cleanup. Nothing here uploads or starts a print.
"""
import asyncio
import contextlib
import copy
import json
import os
import shutil
import logging
import tempfile
import time
from pathlib import Path

from . import owned_worker, slice_recipe

logger = logging.getLogger(__name__)

# The JOB.json fields a slice commits (the set model_job's decision changes
# retire into artifact_history, plus the derived-plate and check records).
# discard() restores exactly these to their pre-run values: removing the
# output files while leaving JOB.json claiming that G-code — verified,
# bound and timestamped — left a discarded slice trusted by a later resume
# or print-next (P2-24).
SLICE_JOB_FIELDS = ('gcode', 'gcode_verified', 'gcode_full_verified_sha256', 'provenance', 'artifact_binding', 'sliced_at',
                    'slice_config', 'slice_recipe', 'derived_plates', 'refused_plates',
                    'multipart_plan', 'offline_check', 'plate_merge', 'merged', 'tool_mapping',
                    'reused_slice')


def _read_job(job_path):
    try:
        data = json.loads(Path(job_path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class BackgroundSlice:
    """Holds the one in-flight slice for an intake folder."""

    def __init__(self):
        self.task = None
        self.binding = None
        self.job_path = None
        self.work = None
        self.before = None  # what the staging folder held before this run, or None if it did not exist
        self.job_before = None  # the job's slice fields before this run, restored by discard()
        self.result = None
        self.error = None
        self.finished_at = None
        self.job_no_supports = None
        self.job_with_supports = None
        self.supports_result = None
        self.supports_binding = None
        self.supports_task = None
        self.supports_error = None
        self.supports_coverage = None
        self.supports_coverage_identity = None
        self.variant = None  # isolated folder of a concurrent supports slice (#1779)
        self.flagged = False
        self.finding = None
        self.clearance = None
        self.started = asyncio.Event()
        self.first_slice_done = asyncio.Event()
        # Set once the first slice's supports verdict (flagged, finding,
        # clearance) is known and any speculative supports slice has
        # started (#1373): first_slice_done fires before that.
        self.flag_decided = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()  # open unless a test holds the slice until the question is on screen

    def matches(self, binding):
        return self.binding == binding and (self.task is not None or self.result is not None)

    def running(self):
        return (self.task is not None and not self.task.done()) or (
            self.supports_task is not None and not self.supports_task.done()
        )

    async def start(self, job_path, binding, slice_func):
        """Start a slice for this binding, replacing any slice bound to something else."""
        if self.matches(binding) and self.error is None:
            return
        await self.discard()
        self.binding = binding
        self.job_path = str(job_path)
        self.work = Path(job_path).parent / 'sliced'
        self.before = _entries(self.work) if self.work.exists() else None
        job = _read_job(job_path)
        self.job_before = ({key: job[key] for key in SLICE_JOB_FIELDS if key in job}
                           if job is not None else None)
        self.work.mkdir(parents=True, exist_ok=True)
        self.result = None
        self.error = None
        self.finished_at = None
        self.job_no_supports = None
        self.job_with_supports = None
        self.supports_result = None
        self.supports_binding = None
        self.supports_task = None
        self.supports_error = None
        self.supports_coverage = None
        self.supports_coverage_identity = None
        self.variant = None
        self.flagged = False
        self.finding = None
        self.clearance = None
        self.first_slice_done = asyncio.Event()
        self.flag_decided = asyncio.Event()
        self.task = asyncio.get_running_loop().create_task(self._run(slice_func))

    def _variant_dir(self):
        return Path(self.job_path).parent / 'variant_supports'

    def _prepare_variant(self, job_data):
        """A private job folder (own JOB.json, own sliced/) for the with-supports slice (#1779).

        Returns (variant JOB.json path, the job with the speculative decision, its binding).
        The supports slice writes only here, so it can run beside the first slice; take_supports
        adopts the result into the intake folder.
        """
        from ..gcode import artifact as gcode_artifact
        sup_job = copy.deepcopy(job_data)
        sup_job['support_decision'] = {'supports': True, 'by': 'speculative_intake'}
        folder = self._variant_dir()
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        self.variant = folder
        target = folder / 'JOB.json'
        target.write_text(json.dumps(sup_job, indent=2) + "\n")
        src_sha = (sup_job.get('color_manifest') or {}).get('source_sha256') or (
            sup_job.get('downloaded_file') and Path(sup_job['downloaded_file']).is_file() and
            gcode_artifact.file_sha256(sup_job['downloaded_file']))
        return target, sup_job, slice_binding(sup_job, src_sha)

    def _start_supports(self, slice_func, job_path, sup_job, sup_binding, copy_to_main=True):
        """Create the with-supports slice task; ``job_path`` is the JOB.json it slices."""
        import inspect
        from . import slice_recipe
        self.supports_binding = sup_binding
        kw = {}
        sig = inspect.signature(slice_func)
        if any(p.kind == p.VAR_KEYWORD or p.name == 'recipe_sha256' for p in sig.parameters.values()):
            kw['recipe_sha256'] = slice_recipe.binding_sha256(sup_binding)

        async def slice_and_check():
            try:
                self.supports_result = await slice_func(str(job_path), Path(job_path).parent / 'sliced', self, **kw)
                self.job_with_supports = _read_job(job_path)
            finally:
                self._merge_variant_timing(job_path)
            if self.job_with_supports is not None:
                await self._verify_supports_coverage()

        self.supports_task = asyncio.ensure_future(slice_and_check())
        return self.supports_task

    def _merge_variant_timing(self, job_path):
        """Append the isolated slice's TIMING.jsonl lines to the intake's, tagged by variant."""
        if Path(job_path).parent == Path(self.job_path).parent:
            return
        try:
            lines = (Path(job_path).parent / 'TIMING.jsonl').read_text().splitlines()
            with (Path(self.job_path).parent / 'TIMING.jsonl').open('a') as out:
                for line in lines:
                    row = json.loads(line)
                    row['variant'] = 'supports'
                    out.write(json.dumps(row) + "\n")
        except (OSError, ValueError):
            logger.debug('variant timing was not merged', exc_info=True)

    async def _run(self, slice_func):
        self.started.set()
        await self.release.wait()
        # The with-supports slice starts beside the first slice when the geometry report already
        # flags the model and this host may run two slicers (#1779). It works in a private folder.
        concurrent = None
        try:
            concurrent = self._maybe_start_concurrent(slice_func)
        except Exception:  # noqa: BLE001 - the sequential path below still runs the slice
            logger.warning('concurrent supports slice not started; slicing in turn', exc_info=True)
        # discard() cancels this task. Pass the cancel on to the slice and wait until it has
        # settled: a child process is stopped and reaped (owned_worker), a thread (tests) cannot
        # be stopped and is waited for. Otherwise a write could land after discard deletes it.
        work = asyncio.ensure_future(slice_func(self.job_path, self.work, self))
        try:
            self.result = await asyncio.shield(work)
        except asyncio.CancelledError:
            work.cancel()
            await owned_worker.settle(work)
            await self._stop_concurrent(concurrent)
            raise
        except Exception as exc:  # the caller decides whether a failed early slice is fatal
            self.error = exc
            await self._stop_concurrent(concurrent)
            self.flag_decided.set()
            return
        finally:
            self.finished_at = time.time()
            self.first_slice_done.set()

        job_data = _read_job(self.job_path)
        if job_data is None:
            await self._stop_concurrent(concurrent)
            self.flag_decided.set()
            return
        self.job_no_supports = copy.deepcopy(job_data)

        # Check if flagged by Orca warnings or support clearance
        from . import slice_warnings, support_clearance
        finding = slice_warnings.support_finding(job_data)
        clearance = job_data.get('support_clearance')
        if clearance is None and job_data.get('downloaded_file'):
            # Off the event loop: a large mesh would stall buttons and heartbeats (#1373 review).
            clearance = await asyncio.to_thread(
                support_clearance.safe_analyze, job_data.get('downloaded_file'),
                first_layer_mm=support_clearance.job_first_layer_mm(job_data))

        is_flagged = bool(finding) or support_clearance.risky(clearance)
        self.flagged = is_flagged
        self.finding = finding
        self.clearance = clearance
        self.first_slice_done.set()
        if not is_flagged:
            await self._stop_concurrent(concurrent)
            self.flag_decided.set()
            return

        try:
            if concurrent is not None:
                self.flag_decided.set()
                try:
                    await asyncio.shield(concurrent)
                    if self.supports_result is not None:
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - redo it in turn on the first slice's own record
                    logger.warning('concurrent supports slice failed; slicing it again in turn', exc_info=True)
                    self._reset_supports()
            if self.job_path is not None and self.supports_task is None:
                sup_path, sup_job, sup_binding = self._prepare_variant(job_data) if concurrent is not None \
                    else (None, None, None)
                if sup_path is None:  # the long-standing path: the same folder and JOB.json, in turn
                    from ..gcode import artifact as gcode_artifact
                    sup_job = copy.deepcopy(job_data)
                    sup_job['support_decision'] = {'supports': True, 'by': 'speculative_intake'}
                    Path(self.job_path).write_text(json.dumps(sup_job, indent=2) + "\n")
                    src_sha = (sup_job.get('color_manifest') or {}).get('source_sha256') or (
                        sup_job.get('downloaded_file') and Path(sup_job['downloaded_file']).is_file() and
                        gcode_artifact.file_sha256(sup_job['downloaded_file']))
                    sup_binding, sup_path = slice_binding(sup_job, src_sha), self.job_path
                sup_work = self._start_supports(slice_func, sup_path, sup_job, sup_binding)
                self.flag_decided.set()
                await asyncio.shield(sup_work)
        except asyncio.CancelledError:
            if self.supports_task and not self.supports_task.done():
                self.supports_task.cancel()
                await owned_worker.settle(self.supports_task)
            raise
        except Exception as exc:
            self.supports_error = exc
        finally:
            self.flag_decided.set()
            self._write_slice_job(self.job_no_supports)

    def _maybe_start_concurrent(self, slice_func):
        from . import preview_parallel, support_clearance
        if not preview_parallel.enabled():
            return None
        job_data = _read_job(self.job_path)
        if job_data is None or not support_clearance.risky(job_data.get('support_clearance')):
            return None  # only an Orca warning could flag it: known after the first slice
        sup_path, sup_job, sup_binding = self._prepare_variant(job_data)
        return self._start_supports(slice_func, sup_path, sup_job, sup_binding)

    def _reset_supports(self):
        self.supports_task = None
        self.supports_result = None
        self.supports_binding = None
        self.job_with_supports = None
        self.supports_coverage = None
        self.supports_coverage_identity = None
        shutil.rmtree(self.variant, ignore_errors=True) if self.variant else None
        self.variant = None

    async def _stop_concurrent(self, task):
        if task is None:
            return
        if task.done():
            # Already finished: nothing to cancel, but its private folder must not outlive the
            # first slice's verdict, and a failure is consumed and logged here.
            if not task.cancelled() and task.exception() is not None:
                logger.debug('finished concurrent supports slice had failed', exc_info=task.exception())
        else:
            task.cancel()
            try:
                await owned_worker.settle(task)
            except asyncio.CancelledError:
                logger.debug('concurrent supports slice cancelled')
            except Exception:  # noqa: BLE001 - the stopped slice's failure is logged, then reset below
                logger.debug('stopped concurrent supports slice raised', exc_info=True)
        self._reset_supports()

    def _adopt_variant(self):
        """Move an isolated supports slice into the intake folder and rebase its recorded paths."""
        folder, self.variant = self.variant, None
        if folder is None:
            return
        if self.job_with_supports is None:  # the supports slice failed: leave no partial outputs
            shutil.rmtree(folder, ignore_errors=True)
            return
        old_root = str(folder)
        # Never publish into the first slice's namespace. Both slices can emit
        # the same filename, and the supports review binds to the supports bytes.
        new_root = tempfile.mkdtemp(prefix='adopted_supports_', dir=Path(self.job_path).parent)
        moved = []

        def rebase(value):
            if isinstance(value, dict):
                return {k: rebase(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rebase(v) for v in value]
            if isinstance(value, tuple):
                return tuple(rebase(v) for v in value)
            if isinstance(value, Path):
                return Path(rebase(str(value)))
            if isinstance(value, str) and value.startswith(old_root + os.sep):
                moved.append((value, new_root + value[len(old_root):]))
                return moved[-1][1]
            return value

        job = rebase(self.job_with_supports)
        result = rebase(self.supports_result)
        for src, dest in dict.fromkeys(moved):
            if Path(src).is_file():
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dest)
        self.job_with_supports = job
        self.supports_result = result
        shutil.rmtree(folder, ignore_errors=True)

    async def _verify_supports_coverage(self):
        """Verify preview G-code in the bounded worker; keep failures explicitly unverified."""
        from . import coverage_reuse, support_clearance
        from ..intake.worker import _heavy
        from ..jobs import store as jobs_store

        started = time.monotonic()
        job = self.job_with_supports or {}
        paths = self.supports_result if isinstance(self.supports_result, (list, tuple)) else [self.supports_result]
        paths = [str(path) for path in paths if path]
        report = job.get('support_clearance') or self.clearance or {}
        if not paths or not report:
            return
        try:
            extent = None
            bounds_note = None
            try:
                source = job.get('downloaded_file')
                if source:
                    extent = await _heavy(support_clearance.source_extent, source)
            except Exception as exc:  # noqa: BLE001 - persisted as unverified; never fail open
                bounds_note = f'source bounds unreadable ({exc}); coverage compared without a source-to-slice mapping'
            plates = coverage_reuse.coverage_plates(paths, job, extent, bounds_note)
            result, identity = await _heavy(coverage_reuse.check_for_preview, paths,
                                            report.get('regions') or [], coverage_reuse.coverage_inputs(job), plates)
            if not isinstance(result, dict) or not isinstance(result.get('checked'), bool) or not isinstance(result.get('ok'), bool):
                raise ValueError('coverage worker returned an invalid result')
            self.supports_coverage = result
            self.supports_coverage_identity = identity
        except Exception as exc:  # noqa: BLE001 - failures are explicit and ask at Ready
            self.supports_coverage = {'checked': False, 'ok': True, 'regions': [], 'plate_mode': 'none',
                                      'unverified': [{'plate': None, 'reason': f'the support coverage check could not run ({exc})'}]}
            self.supports_coverage_identity = None
        finally:
            jobs_store._timing(self.job_path, 'preview_support_coverage_done',
                              elapsed_s=round(time.monotonic() - started, 3),
                              checked=bool(self.supports_coverage and self.supports_coverage.get('checked')))

    def _write_slice_job(self, slice_job_data, default_supports=None):
        if slice_job_data is None or self.job_path is None:
            return
        target = Path(self.job_path)
        current = json.loads(target.read_text()) if target.exists() else {}
        updated = copy.deepcopy(slice_job_data)
        for field in ('support_decision', 'clearance_acknowledgement', 'copies', 'filament_selection', 'color_decisions',
                      'filament_warning_acknowledgement', 'filament_warnings', 'filament_warnings_pending'):
            if field in current:
                if (field == 'support_decision' and isinstance(current[field], dict)
                        and current[field].get('by') == 'speculative_intake'):
                    continue  # the speculative slice's temporary decision is not the operator's
                updated[field] = current[field]
        if 'filament_warning_acknowledgement' not in current:
            # A slicing run consumed the acceptance (slice_job consumes a
            # used acceptance so a reslice re-presents the warnings); the
            # pre-slice snapshot must not resurrect it.
            updated.pop('filament_warning_acknowledgement', None)
        if 'support_decision' not in updated and default_supports is not None:
            updated['support_decision'] = {'supports': default_supports, 'by': 'speculative_intake'}
        target.write_text(json.dumps(updated, indent=2) + "\n")

    async def settle(self):
        """Wait until the slice task has fully finished, speculative slice included.

        The speculative with-supports slice rewrites JOB.json while it runs
        (support decision on, then the no-supports snapshot restored at the
        end), so any intake write or recipe comparison made in that window
        races it: an operator's recorded decision can be clobbered by the
        speculative slice_job's own write-back, and the slice-approval guard
        then refuses the handoff. Callers that record a decision or compare
        the binding must settle first; afterwards JOB.json is stable.
        """
        if self.task is not None:
            # suppress kept with reason (#1455): _run records the slice's failures
            # in self.error, so awaiting the task can only surface CancelledError —
            # an expected end state (a discarded or superseded slice) whose
            # outcome the callers read from the fields, never a lost failure.
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def take(self, binding):
        """Return the finished slice for this binding, or None when there is nothing usable."""
        if binding == self.supports_binding and (self.supports_task is not None or self.supports_result is not None):
            return await self.take_supports()
        if self.binding != binding or self.task is None:
            return None
        # suppress kept with reason (#1455): see settle() — the outcome is
        # read from self.error / self.result below.
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        self.task = None
        if self.error is not None or self.result is None:
            return None
        self._write_slice_job(self.job_no_supports)
        return self.result

    async def take_no_supports(self):
        """Return the no-supports slice result, restoring no-supports job metadata to disk."""
        if self.task is not None:
            # suppress kept with reason (#1455): see settle().
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if await asyncio.to_thread(_sliced_with_supports, self.result):
            # The first slice ran before any decision, so a designer project
            # with supports on (or painted enforcers) sliced WITH supports:
            # it is not the "without supports" artifact the operator chose
            # (#1373, cute-dino corpus case). Nothing usable here; the caller
            # re-slices with the recorded decision.
            return None
        self._write_slice_job(self.job_no_supports, default_supports=False)
        return self.result

    async def take_supports(self):
        """Return the with-supports slice result, writing with-supports job metadata to disk."""
        if self.task is not None:
            # suppress kept with reason (#1455): see settle().
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        try:
            if self.supports_task is not None:
                # suppress kept with reason (#1455): see settle() — the outcome is
                # read from self.supports_error / self.supports_result.
                with contextlib.suppress(asyncio.CancelledError):
                    await self.supports_task
        finally:  # a failed supports slice raises above; its private folder must not outlive it
            self._adopt_variant()
        self._write_slice_job(self.job_with_supports, default_supports=True)
        return self.supports_result

    async def discard(self):
        """Stop the running slice, if any, and remove what was written since it started."""
        task = self.task
        sup_task = self.supports_task
        self.task = None
        self.supports_task = None
        self.result = None
        self.supports_result = None
        self.error = None
        self.supports_error = None
        self.binding = None
        self.supports_binding = None
        self.job_no_supports = None
        self.job_with_supports = None
        self.supports_coverage = None
        self.supports_coverage_identity = None
        self.flagged = False
        self.finding = None
        self.clearance = None
        if task is not None and not task.done():
            task.cancel()
            # suppress kept with reason (#1455): the task was cancelled by us
            # just above, so its CancelledError is the expected outcome.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if sup_task is not None and not sup_task.done():
            sup_task.cancel()
            # suppress kept with reason (#1455): cancelled by us just above.
            with contextlib.suppress(asyncio.CancelledError):
                await sup_task
        if self.variant is not None:  # the isolated supports slice leaves nothing behind
            shutil.rmtree(self.variant, ignore_errors=True)
            self.variant = None
        if self.work is not None:  # a discarded slice leaves no G-code behind
            if self.before is None:
                shutil.rmtree(self.work, ignore_errors=True)
            else:  # keep what was already in the folder before this slice started
                for path in sorted(_entries(self.work) - self.before, key=lambda p: len(p.parts)):
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        path.unlink(missing_ok=True)
            self.work = None
            self.before = None
        if self.job_before is not None and self.job_path is not None:
            # The slice's JOB.json writes go with its files (P2-24): restore
            # the slice fields snapshotted at start(), atomically, as
            # model_job.write_job does — intake reads JOB.json concurrently.
            job = _read_job(self.job_path)
            if job is not None:
                for key in SLICE_JOB_FIELDS:
                    if key in self.job_before:
                        job[key] = self.job_before[key]
                    else:
                        job.pop(key, None)
                # A speculative slice's temporary decision is not an
                # operator decision (#922): discarding that slice removes
                # it, so a cancelled or reoriented job does not carry a
                # settled support question it never answered.
                if isinstance(job.get('support_decision'), dict) \
                        and job['support_decision'].get('by') == 'speculative_intake':
                    job.pop('support_decision', None)
                target = Path(self.job_path)
                tmp = target.with_name(f".JOB.json.{os.getpid()}.{time.monotonic_ns()}.tmp")
                tmp.write_text(json.dumps(job, indent=2) + "\n")
                os.replace(tmp, target)
            self.job_before = None


def _entries(work):
    """Top-level entries of a staging folder and of its orca/ run folders."""
    if not work.is_dir():
        return set()
    found = set(work.iterdir())
    if (work / 'orca').is_dir():
        found |= set((work / 'orca').iterdir())
    return found


_RUNNERS = {}


def runner_for(job_path):
    """The one runner for an intake folder. A second intake never shares it."""
    key = str(Path(job_path).parent.resolve())
    return _RUNNERS.setdefault(key, BackgroundSlice())


def drop_runner(job_path):
    _RUNNERS.pop(str(Path(job_path).parent.resolve()), None)


def _sliced_with_supports(result):
    """True when any G-code of a slice result says ``enable_support = 1``. Never raises."""
    paths = result if isinstance(result, (list, tuple)) else [result] if result else []
    for path in paths:
        try:
            text = Path(path).read_text(errors='replace')
        except (OSError, TypeError, ValueError):
            continue
        from .edit_plan import _config_value
        if str(_config_value(text, "enable_support") or '').strip() == '1':
            return True
    return False


def slice_binding(job, source_sha256, state=None):
    """Canonical JSON of the complete slice recipe: every input that changes the G-code, plus the source bytes."""
    return slice_recipe.canonical(slice_recipe.slice_recipe(job, source_sha256, state))
