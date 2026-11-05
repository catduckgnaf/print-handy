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
import time
from pathlib import Path

from . import owned_worker, slice_recipe

# The JOB.json fields a slice commits (the set model_job's decision changes
# retire into artifact_history, plus the derived-plate and check records).
# discard() restores exactly these to their pre-run values: removing the
# output files while leaving JOB.json claiming that G-code — verified,
# bound and timestamped — left a discarded slice trusted by a later resume
# or print-next (P2-24).
SLICE_JOB_FIELDS = ('gcode', 'gcode_verified', 'provenance', 'artifact_binding', 'sliced_at',
                    'slice_config', 'slice_recipe', 'derived_plates', 'refused_plates',
                    'multipart_plan', 'offline_check', 'plate_merge', 'merged', 'tool_mapping')


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
        self.flagged = False
        self.finding = None
        self.clearance = None
        self.started = asyncio.Event()
        self.first_slice_done = asyncio.Event()
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
        self.flagged = False
        self.finding = None
        self.clearance = None
        self.first_slice_done = asyncio.Event()
        self.task = asyncio.get_running_loop().create_task(self._run(slice_func))

    async def _run(self, slice_func):
        self.started.set()
        await self.release.wait()
        # discard() cancels this task. Pass the cancel on to the slice and wait until it has
        # settled: a child process is stopped and reaped (owned_worker), a thread (tests) cannot
        # be stopped and is waited for. Otherwise a write could land after discard deletes it.
        work = asyncio.ensure_future(slice_func(self.job_path, self.work, self))
        try:
            self.result = await asyncio.shield(work)
        except asyncio.CancelledError:
            work.cancel()
            await owned_worker.settle(work)
            raise
        except Exception as exc:  # the caller decides whether a failed early slice is fatal
            self.error = exc
            return
        finally:
            self.finished_at = time.time()
            self.first_slice_done.set()

        job_data = _read_job(self.job_path)
        if job_data is None:
            return
        self.job_no_supports = copy.deepcopy(job_data)

        # Check if flagged by Orca warnings or support clearance
        from . import slice_warnings, support_clearance
        finding = slice_warnings.support_finding(job_data)
        clearance = job_data.get('support_clearance')
        if clearance is None and job_data.get('downloaded_file'):
            clearance = support_clearance.safe_analyze(job_data.get('downloaded_file'))

        is_flagged = bool(finding) or support_clearance.risky(clearance)
        self.flagged = is_flagged
        self.finding = finding
        self.clearance = clearance
        self.first_slice_done.set()

        if is_flagged:
            try:
                import inspect
                from . import slice_recipe, model_job
                sup_job = copy.deepcopy(job_data)
                sup_job['support_decision'] = {'supports': True, 'by': 'speculative_intake'}
                if self.job_path is not None:
                    target = Path(self.job_path)
                    target.write_text(json.dumps(sup_job, indent=2) + "\n")

                src_sha = (sup_job.get('color_manifest') or {}).get('source_sha256') or (
                    sup_job.get('downloaded_file') and Path(sup_job['downloaded_file']).is_file() and
                    model_job.file_sha256(sup_job['downloaded_file'])
                )
                sup_binding = slice_binding(sup_job, src_sha)
                sup_recipe_sha = slice_recipe.binding_sha256(sup_binding)
                self.supports_binding = sup_binding

                kw = {}
                sig = inspect.signature(slice_func)
                if any(p.kind == p.VAR_KEYWORD or p.name == 'recipe_sha256' for p in sig.parameters.values()):
                    kw['recipe_sha256'] = sup_recipe_sha

                sup_work = asyncio.ensure_future(slice_func(self.job_path, self.work, self, **kw))
                self.supports_task = sup_work
                self.supports_result = await asyncio.shield(sup_work)
                self.job_with_supports = _read_job(self.job_path)
            except asyncio.CancelledError:
                if self.supports_task and not self.supports_task.done():
                    self.supports_task.cancel()
                    await owned_worker.settle(self.supports_task)
                raise
            except Exception as exc:
                self.supports_error = exc
            finally:
                self._write_slice_job(self.job_no_supports)

    def _write_slice_job(self, slice_job_data, default_supports=None):
        if slice_job_data is None or self.job_path is None:
            return
        target = Path(self.job_path)
        current = json.loads(target.read_text()) if target.exists() else {}
        updated = copy.deepcopy(slice_job_data)
        for field in ('support_decision', 'clearance_acknowledgement', 'copies', 'filament_selection', 'color_decisions'):
            if field in current:
                if (field == 'support_decision' and isinstance(current[field], dict)
                        and current[field].get('by') == 'speculative_intake'):
                    continue  # the speculative slice's temporary decision is not the operator's
                updated[field] = current[field]
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
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def take(self, binding):
        """Return the finished slice for this binding, or None when there is nothing usable."""
        if binding == self.supports_binding and (self.supports_task is not None or self.supports_result is not None):
            return await self.take_supports()
        if self.binding != binding or self.task is None:
            return None
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
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self._write_slice_job(self.job_no_supports, default_supports=False)
        return self.result

    async def take_supports(self):
        """Return the with-supports slice result, writing with-supports job metadata to disk."""
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.supports_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self.supports_task
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
        self.flagged = False
        self.finding = None
        self.clearance = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if sup_task is not None and not sup_task.done():
            sup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sup_task
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


def slice_binding(job, source_sha256, state=None):
    """Canonical JSON of the complete slice recipe: every input that changes the G-code, plus the source bytes."""
    return slice_recipe.canonical(slice_recipe.slice_recipe(job, source_sha256, state))
