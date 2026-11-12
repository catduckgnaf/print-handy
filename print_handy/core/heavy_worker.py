"""Run one CPU-heavy intake step in a child process and print its JSON result.

Large 3MF inspection is pure-Python XML work that holds the GIL for seconds at a time, so
asyncio.to_thread still freezes the gateway event loop and Discord button callbacks time out.
A child process has its own GIL and its own memory, so the gateway stays responsive.
"""
import importlib
import json
import sys
from pathlib import Path

ALLOWED = {
    ('model_job', 'audit'), ('model_job', 'slice_job'), ('model_job', 'maker_download'),
    ('model_job', 'printables_download'), ('model_job', 'thingiverse_download'),
    ('source_colors', 'inventory'),
    ('orca_adapter', 'orca_plate_capacity'), ('orca_adapter', 'fit_options'),
    ('orca_adapter', 'largest_fitting_fraction'),
    ('model_job', 'per_part_fit_options'), ('plate_merge', 'full_size_split_possible'),
    ('geometry_layout', 'inspect_geometry'), ('geometry_layout', 'arrange_and_export'),
    ('geometry_layout', 'probe_single_plate_rearrange'), ('model_job', 'full_size_one_plate'),
    ('cad_split', 'check_source_against_split_plan'), ('cad_split', 'prepare_cad_split'),
    ('model_parts', 'combine'), ('model_job', 'painted_fit_for_job'),
}


def _plain(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def preload():
    """Import every offloadable step's module (#1332, the warm spare).

    A warm spare runs this before it blocks on its request, so the 0.6 to
    1.1 s import cost is paid while nobody waits. A cold worker skips it
    and imports only the module its step needs.
    """
    for module in sorted({module for module, _func in ALLOWED}):
        importlib.import_module(f'print_handy.core.{module}')


def main(argv=None):
    # Works as `python3 -m print_handy.core.heavy_worker` or as a plain script path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from print_handy.core import resources
    resources.apply_to_environment()  # before numpy loads: BLAS/OpenMP pools follow the CPU quota
    if '--warm' in (sys.argv[1:] if argv is None else argv):
        preload()  # then wait for the one request this spare serves
    request = json.loads(sys.stdin.read())
    # The worker is a Print Handy process too: configure the shared logging
    # (stderr only — stdout stays the JSON reply channel) and bind the
    # intake context the parent forwarded, so slicer and geometry log lines
    # carry the same identity and settings as every other process (P2-55).
    # The parent re-emits whatever lands on stderr into its own log.
    from print_handy import logs
    logs.configure()
    context = request.get('log_context')
    if isinstance(context, dict):
        logs.bind(**{str(key): value for key, value in context.items()})
    key = (request['module'], request['func'])
    if key not in ALLOWED:
        raise SystemExit(f'not an offloadable step: {key}')
    func = getattr(importlib.import_module(f'print_handy.core.{key[0]}'), key[1])
    args = list(request.get('args', []))
    if key != ('model_job', 'slice_job') and args and isinstance(args[0], str):
        args[0] = Path(args[0])  # these take a Path source file; JSON carried it as a string
    result = func(*args, **request.get('kwargs', {}))
    sys.stdout.write('\n' + json.dumps({'ok': True, 'path': isinstance(result, Path), 'result': _plain(result)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # report to the parent; it re-raises with the same message
        from print_handy.core.worker_errors import encode
        reply = encode(exc)
        sys.stdout.write('\n' + json.dumps(reply))
