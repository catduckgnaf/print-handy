"""Every start call site takes its verdict from start_truth (#1318, #1160 proposal 2).

A start call site is a print path that sends a start request (calls
``lease.submitting()``). The print paths live in ``launch/print_now.py`` and
``launch/gates.py`` since #1323 split them out of ``core/model_job.py``; this
suite reads all three as one module, so a path moved back or forth is still
seen. It enumerates them from the source and fails when:

- a print path sends a start without an entry in ``start_truth.START_CALL_SITES``;
- a registered path's verdict never reaches its transport's start-truth function;
- a polled path confirms its start without the transport's function and a baseline;
- any code in the start modules spells a started, preparing or uncertain verdict itself
  (a ``phase``/``outcome`` literal, or a comparison of a readback against one) instead of
  taking it from start_truth.
"""
import ast
from pathlib import Path

from print_handy.core import start_truth

ROOT = Path(__file__).resolve().parents[2]
MODEL_JOB = ROOT / "print_handy" / "core" / "model_job.py"
# The modules holding the print paths (#1323 moved them out of model_job).
PRINT_PATH_MODULES = (MODEL_JOB, ROOT / "print_handy" / "launch" / "print_now.py",
                      ROOT / "print_handy" / "launch" / "gates.py")
# Modules that build or relay print-now's outcome dicts; none may spell a verdict itself.
START_MODULES = (*PRINT_PATH_MODULES, ROOT / "print_handy" / "core" / "prepared_intake.py",
                 ROOT / "print_handy" / "runtime" / "print_next.py")

PHASES = {start_truth.PHASE_EXECUTING, start_truth.PHASE_PREPARING, start_truth.PHASE_AWAITING,
          start_truth.PHASE_UNRESOLVED, start_truth.PHASE_SUBMITTED}
OUTCOMES = {start_truth.OUTCOME_PRINTING, start_truth.OUTCOME_PREPARING, start_truth.OUTCOME_UNCONFIRMED,
            start_truth.OUTCOME_UNKNOWN, "failed"}
VERDICT_KEYS = {"phase": PHASES, "outcome": OUTCOMES}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _print_paths():
    """The print-path modules as one module body, so a name resolves wherever it moved."""
    body = []
    for path in PRINT_PATH_MODULES:
        body.extend(_tree(path).body)
    return ast.Module(body=body, type_ignores=[])


def _top_functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _sends_start(fn):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "submitting"
               and not n.args for n in ast.walk(fn))


def _truth_refs(fn):
    """The start_truth attributes a function references."""
    return {n.attr for n in ast.walk(fn)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "start_truth"}


def _closure(name, functions):
    """`name` and every module function it calls by name, transitively."""
    seen, todo = set(), [name]
    while todo:
        current = todo.pop()
        if current in seen or current not in functions:
            continue
        seen.add(current)
        todo.extend(n.id for n in ast.walk(functions[current]) if isinstance(n, ast.Name) and n.id in functions)
    return seen


def _strings(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(_strings(e) for e in node.elts)) if node.elts else set()
    return set()


def _names_verdict(node):
    """Whether an expression reads a verdict field: x.outcome, x.phase, x.get('phase'), x['outcome']."""
    if isinstance(node, ast.Attribute):
        return node.attr in VERDICT_KEYS
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        return bool(_strings(node.args[0]) & set(VERDICT_KEYS))
    if isinstance(node, ast.Subscript):
        return bool(_strings(node.slice) & set(VERDICT_KEYS))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str" and node.args:
        return _names_verdict(node.args[0])
    if isinstance(node, ast.BoolOp):
        return any(_names_verdict(v) for v in node.values)
    return False


def verdict_literals(tree, *, comparisons=True):
    """(line, text) for every place the code spells a start verdict instead of taking it from start_truth."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                allowed = VERDICT_KEYS.get(key.value) if isinstance(key, ast.Constant) else None
                if allowed and _strings(value) & allowed:
                    found.append((node.lineno, f"{key.value}: {ast.unparse(value)}"))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in VERDICT_KEYS and _strings(kw.value) & VERDICT_KEYS[kw.arg]:
                    found.append((node.lineno, f"{kw.arg}={ast.unparse(kw.value)}"))
        elif comparisons and isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(_names_verdict(o) for o in operands) and any(_strings(o) & (PHASES | OUTCOMES) for o in operands):
                found.append((node.lineno, ast.unparse(node)))
    return found


def test_every_print_path_that_sends_a_start_is_registered():
    functions = _top_functions(_print_paths())
    senders = {name for name, fn in functions.items() if _sends_start(fn)}
    assert senders == set(start_truth.START_CALL_SITES), (
        "a print path sends a start without a START_CALL_SITES entry, or an entry names a path that no "
        f"longer sends one: senders {sorted(senders)}, registered {sorted(start_truth.START_CALL_SITES)}")


def test_every_registered_call_site_reaches_its_start_truth_function():
    functions = _top_functions(_print_paths())
    for site, truth in start_truth.START_CALL_SITES.items():
        assert callable(getattr(start_truth, truth, None)), (site, truth)
        refs = set().union(*(_truth_refs(functions[f]) for f in _closure(site, functions)))
        assert truth in refs, f"{site} decides its start verdict without start_truth.{truth}"


def test_every_polled_confirmation_uses_a_baselined_start_truth_function():
    tree = _print_paths()
    functions = _top_functions(tree)
    polls = [(name, call) for name, fn in functions.items() if name != "_poll_start_confirmation"
             for call in ast.walk(fn)
             if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
             and call.func.id == "_poll_start_confirmation"]
    assert len(polls) >= 5  # OctoPrint, Bambu Lab, Elegoo SDCP, PrusaLink, stock Flashforge
    for name, call in polls:
        truth = [kw.value for kw in call.keywords if kw.arg == "truth"]
        assert truth and isinstance(truth[0], ast.Lambda), f"{name} polls a start without a start-truth function"
        body = truth[0].body
        assert (isinstance(body, ast.Call) and isinstance(body.func, ast.Attribute)
                and isinstance(body.func.value, ast.Name) and body.func.value.id == "start_truth"), name
        assert len(body.args) == 3, f"{name} judges its start without the pre-request baseline"


def test_no_start_module_spells_a_verdict_itself():
    for path in START_MODULES:
        found = verdict_literals(_tree(path), comparisons=path in PRINT_PATH_MODULES)
        assert not found, f"{path.name} decides start verdicts without start_truth: {found}"


def test_the_classifier_is_called_only_through_start_truth():
    calls = [n.lineno for n in ast.walk(_print_paths())
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "classify_start_readback"]
    assert not calls, f"a print path classifies a readback outside start_truth.moonraker_readback at lines {calls}"


def test_the_scanner_catches_a_call_site_that_bypasses_start_truth():
    """The checks above would fail on the shapes the consolidation removed."""
    bypass = ast.parse(
        "def confirm(readback, out):\n"
        "    if readback and readback.outcome == 'preparing':\n"
        "        return {'started': True, 'phase': 'preparing', 'outcome': 'preparing'}\n"
        "    out.update(phase='preparing_or_unresolved', outcome='unconfirmed')\n"
        "    return str(out.get('phase') or '') in ('executing', 'preparing')\n")
    assert len(verdict_literals(bypass)) == 6
    unregistered = ast.parse("def _print_new_family(lease):\n    lease.submitting()\n")
    assert _sends_start(_top_functions(unregistered)["_print_new_family"])
    assert not verdict_literals(ast.parse("out = {'phase': start_truth.PHASE_PREPARING, **fields}\n"))
