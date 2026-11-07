"""CPU budget, thread-pool environment, spawn failures and the PID-limit diagnostic (container thread cap)."""
import asyncio
import errno
import os
import subprocess
import sys
import time

import pytest

from print_handy import diagnostics, doctor
from print_handy.core import orca_adapter, owned_worker, resources


def write(root, name, text):
    (root / name).write_text(text)
    return root


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    monkeypatch.delenv(resources.THREADS_ENV, raising=False)
    for name in resources.POOL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # An empty cgroup root: the tests never read the machine's real limits unless they set their own.
    monkeypatch.setattr(resources, 'CGROUP_ROOT', tmp_path_factory.mktemp('cgroup'))
    monkeypatch.setattr(resources, 'affinity_cpus', lambda: list(range(8)))  # the host's CPUs, whatever it has


# --- CPU budget ---------------------------------------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('200000 100000\n', 2), ('150000 100000\n', 2), ('50000 100000\n', 1), ('max 100000\n', None)])
def test_cgroup_v2_quota(tmp_path, text, expected):
    write(tmp_path, 'cpu.max', text)
    assert resources.thread_budget({}, tmp_path) == expected


def test_quota_above_the_cpus_this_process_may_use_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, 'affinity_cpus', lambda: [0, 1])
    write(tmp_path, 'cpu.max', '800000 100000\n')
    assert resources.thread_budget({}, tmp_path) == 2
    assert resources.thread_budget({resources.THREADS_ENV: '6'}, tmp_path) == 6  # an explicit count still wins


def test_cgroup_v1_quota(tmp_path):
    (tmp_path / 'cpu').mkdir()
    write(tmp_path / 'cpu', 'cpu.cfs_quota_us', '200000\n')
    write(tmp_path / 'cpu', 'cpu.cfs_period_us', '100000\n')
    assert resources.thread_budget({}, tmp_path) == 2


def test_cgroup_v1_unlimited(tmp_path):
    (tmp_path / 'cpu').mkdir()
    write(tmp_path / 'cpu', 'cpu.cfs_quota_us', '-1\n')
    write(tmp_path / 'cpu', 'cpu.cfs_period_us', '100000\n')
    assert resources.thread_budget({}, tmp_path) is None


def test_no_cgroup_files_means_no_budget(tmp_path):
    assert resources.thread_budget({}, tmp_path) is None


def test_cpuset_only_limit_budgets_from_affinity(tmp_path, monkeypatch):
    # No quota anywhere, but the process may only run on 2 of the host's 8
    # CPUs (--cpuset-cpus / AllowedCPUs): the pools follow the affinity (#952).
    monkeypatch.setattr(resources, 'affinity_cpus', lambda: [0, 1])
    monkeypatch.setattr(resources, '_host_cpu_count', lambda: 8)
    assert resources.thread_budget({}, tmp_path) == 2


def test_full_affinity_means_no_cpuset_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(resources, 'affinity_cpus', lambda: list(range(8)))
    monkeypatch.setattr(resources, '_host_cpu_count', lambda: 8)
    assert resources.thread_budget({}, tmp_path) is None


def test_override_wins_and_bad_override_is_ignored(tmp_path):
    write(tmp_path, 'cpu.max', '200000 100000\n')
    assert resources.thread_budget({'PRINT_HANDY_THREADS': '3'}, tmp_path) == 3
    assert resources.thread_budget({'PRINT_HANDY_THREADS': '0'}, tmp_path) == 1
    assert resources.thread_budget({'PRINT_HANDY_THREADS': 'lots'}, tmp_path) == 2


# --- environment and affinity -------------------------------------------------------------

def test_apply_to_environment_sets_pools_without_clobbering():
    env = {'PRINT_HANDY_THREADS': '2', 'OMP_NUM_THREADS': '7'}
    assert resources.apply_to_environment(env) == 2
    assert env['OMP_NUM_THREADS'] == '7'
    assert env['OPENBLAS_NUM_THREADS'] == env['MKL_NUM_THREADS'] == env['NUMEXPR_NUM_THREADS'] == '2'


def test_no_budget_leaves_environment_alone():
    env = {}
    assert resources.apply_to_environment(env) is None
    assert env == {}
    assert resources.exports(env) == ''


def test_exports_for_entrypoint():
    lines = resources.exports({'PRINT_HANDY_THREADS': '2', 'OMP_NUM_THREADS': '9'}).splitlines()
    assert 'export OMP_NUM_THREADS=9' not in lines
    assert 'export OPENBLAS_NUM_THREADS=2' in lines


def test_affinity_prefix(monkeypatch):
    monkeypatch.setattr(resources.shutil, 'which', lambda name: '/usr/bin/taskset')
    assert resources.affinity_prefix(2, list(range(72))) == ['/usr/bin/taskset', '-c', '0,1']
    assert resources.affinity_prefix(2, [0, 1]) == []
    assert resources.affinity_prefix(None, list(range(72))) == []  # no budget: no mask
    monkeypatch.setattr(resources.shutil, 'which', lambda name: None)
    assert resources.affinity_prefix(2, list(range(72))) == []


def test_default_executor_follows_budget():
    class Loop:
        executor = None

        def set_default_executor(self, executor):
            self.executor = executor

    loop = Loop()
    assert resources.cap_default_executor(loop, 2) == 6
    assert loop.executor._max_workers == 6
    loop.executor.shutdown()
    assert resources.cap_default_executor(Loop(), None) is None


# --- launches carry the budget -------------------------------------------------------------

def test_orca_launch_gets_affinity_and_pool_env(monkeypatch):
    monkeypatch.setenv('PRINT_HANDY_THREADS', '2')
    monkeypatch.setattr(resources.shutil, 'which', lambda name: '/usr/bin/taskset')
    monkeypatch.setattr(resources, 'affinity_cpus', lambda: list(range(72)))
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, '', '')

    monkeypatch.setattr(orca_adapter.subprocess, 'run', fake_run)
    orca_adapter._spawn_slicer(['orca-slicer', '--help'], timeout=5, cwd='.')
    assert seen['cmd'] == ['/usr/bin/taskset', '-c', '0,1', 'orca-slicer', '--help']
    assert seen['env']['OMP_NUM_THREADS'] == '2'


def test_orca_launch_unchanged_without_budget(monkeypatch):
    seen = {}
    monkeypatch.setattr(orca_adapter.subprocess, 'run',
                        lambda cmd, **kw: seen.update(cmd=cmd, **kw) or subprocess.CompletedProcess(cmd, 0, '', ''))
    orca_adapter._spawn_slicer(['orca-slicer'], timeout=5, cwd='.')
    assert seen['cmd'] == ['orca-slicer']
    assert 'OMP_NUM_THREADS' not in seen['env']


@pytest.mark.skipif(os.name != 'posix', reason='owned workers use process groups')
def test_worker_launch_gets_pool_env(monkeypatch):
    monkeypatch.setenv('PRINT_HANDY_THREADS', '2')
    script = 'import os,sys; sys.stdout.write(os.environ["OPENBLAS_NUM_THREADS"])'
    out, _ = asyncio.run(owned_worker.run([sys.executable, '-c', script], b'', label='probe',
                                          lane=owned_worker.GENERAL))
    assert out == b'2'


# --- PID limit ----------------------------------------------------------------------------

def set_pids(root, current, limit):
    write(root, 'pids.current', f'{current}\n')
    write(root, 'pids.max', f'{limit}\n')


def test_pid_status_and_report(tmp_path):
    assert resources.pid_status(tmp_path) is None
    assert resources.pid_report(tmp_path) is None
    set_pids(tmp_path, 10, 'max')
    assert resources.pid_status(tmp_path) == (10, None)
    assert resources.pid_report(tmp_path) is None  # no limit: nothing to report
    set_pids(tmp_path, 100, 4096)
    assert resources.pid_report(tmp_path) == ('100/4096 processes and threads in use', False)
    set_pids(tmp_path, 256, 256)
    text, warn = resources.pid_report(tmp_path)
    assert warn and '256/256' in text


def test_pid_status_reads_the_v1_controller_directory(tmp_path):
    # cgroup v1 keeps the pids controller in a pids/ directory under the
    # root, with the same file names as v2 (P2-251).
    (tmp_path / 'pids').mkdir()
    set_pids(tmp_path / 'pids', 42, 1024)
    assert resources.pid_status(tmp_path) == (42, 1024)
    assert resources.pid_report(tmp_path) == ('42/1024 processes and threads in use', False)


def test_slicer_timeout_at_pid_limit_names_the_limit(monkeypatch):
    set_pids(resources.CGROUP_ROOT, 256, 256)

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(orca_adapter.subprocess, 'run', hang)
    with pytest.raises(resources.ResourceLimitError) as info:
        orca_adapter._spawn_slicer(['orca-slicer'], timeout=1, cwd='.')
    assert '256 of 256' in str(info.value)


def test_slicer_timeout_samples_the_pid_count_before_the_slicer_is_killed(monkeypatch):
    set_pids(resources.CGROUP_ROOT, 256, 256)

    def hang_then_die(cmd, timeout, **kwargs):
        # subprocess.run kills the slicer before it raises, which frees its threads.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (resources.CGROUP_ROOT / 'sampled').exists():
            time.sleep(0.01)
        set_pids(resources.CGROUP_ROOT, 20, 256)
        raise subprocess.TimeoutExpired(cmd, timeout)

    real_error = resources.pid_limit_error

    def marking(root=None):
        found = real_error(root)
        (resources.CGROUP_ROOT / 'sampled').write_text('1')
        return found

    monkeypatch.setattr(resources, 'pid_limit_error', marking)
    monkeypatch.setattr(orca_adapter.subprocess, 'run', hang_then_die)
    with pytest.raises(resources.ResourceLimitError) as info:
        orca_adapter._spawn_slicer(['orca-slicer'], timeout=0.3, cwd='.')
    assert '256 of 256' in str(info.value)


def test_slicer_timeout_below_the_limit_stays_a_timeout(monkeypatch):
    set_pids(resources.CGROUP_ROOT, 20, 4096)

    def hang(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(orca_adapter.subprocess, 'run', hang)
    with pytest.raises(subprocess.TimeoutExpired):
        orca_adapter._spawn_slicer(['orca-slicer'], timeout=1, cwd='.')


def test_slicer_eagain_names_the_limit(monkeypatch):
    def refuse(cmd, **kwargs):
        raise BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable')

    monkeypatch.setattr(orca_adapter.subprocess, 'run', refuse)
    with pytest.raises(resources.ResourceLimitError):
        orca_adapter._spawn_slicer(['orca-slicer'], timeout=1, cwd='.')


def test_missing_binary_is_not_a_pid_problem(monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError(errno.ENOENT, 'no such file')

    monkeypatch.setattr(orca_adapter.subprocess, 'run', missing)
    with pytest.raises(FileNotFoundError):
        orca_adapter._spawn_slicer(['orca-slicer'], timeout=1, cwd='.')


def test_worker_spawn_eagain_is_a_known_diagnostic(monkeypatch):
    async def refuse(*args, **kwargs):
        raise BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable')

    monkeypatch.setattr(owned_worker.asyncio, 'create_subprocess_exec', refuse)
    with pytest.raises(owned_worker.WorkerError) as info:
        asyncio.run(owned_worker.run(['x'], b'', label='model check', lane=owned_worker.GENERAL))
    known = diagnostics.known_cause(info.value)
    assert known is not None and known.category == diagnostics.Category.CONFIGURATION
    assert 'PID limit' in known.cause


def test_known_cause_survives_the_worker_boundary():
    # A worker reports its error to the parent as plain RuntimeError text.
    relayed = RuntimeError(resources.pid_limit_message())
    known = diagnostics.known_cause(relayed)
    assert known is not None
    assert '4096' in known.remedy
    post = diagnostics.known_post('Preparing that model stopped before it was ready.', subsystem='intake', exc=relayed)
    assert 'PH_INTAKE_CONFIGURATION' in post and 'PH_INTAKE_UNKNOWN' not in post


# --- doctor -------------------------------------------------------------------------------

def test_doctor_pid_row():
    set_pids(resources.CGROUP_ROOT, 250, 256)
    text, warn = doctor.check_pids({})
    assert warn and '250/256' in text
    set_pids(resources.CGROUP_ROOT, 10, 'max')
    assert doctor.check_pids({}) is None
