"""Text a chat user sees never tells them to run a shell command to unblock printing (2026-11-03).

A held launch used to be refused in Discord with "run `reconcile-launch --not-started`", which a
chat user has no way to do. Launch refusals reach chat through prepared_intake._refusal_text and
the bot's restart notice; the raw CLI wording stays only in the log line and the CLI itself.
"""
import re
from pathlib import Path

from print_handy.core import prepared_intake

ROOT = Path(__file__).resolve().parents[1]
SHELL_REMEDY = re.compile(r"reconcile-launch|--not-started")
# The CLI and the raw refusal it explains (logged, then mapped to plain words before chat).
# The launch CLI itself may name its own commands; it moved to jobs/cli.py (#1323).
ALLOWED = {"print_handy/core/launch_state.py", "print_handy/core/model_job.py", "print_handy/jobs/cli.py"}

HELD = ("REFUSED: an earlier start on this printer is uncertain (job /data/archive/bag/JOB.json, "
        "file Bag-Clip_plate_1.gcode.3mf, since 2026-10-30T19:21:32-0400). Check the printer, then run "
        "`python3 -m print_handy.core.model_job reconcile-launch --started` or `--not-started`. It is never "
        "resubmitted automatically. Nothing uploaded or started.")


def test_the_held_launch_refusal_posted_to_chat_names_no_shell_command():
    message = prepared_intake._refusal_text(HELD)
    assert message.startswith("Didn't start: an earlier start (Bag-Clip_plate_1.gcode.3mf)")
    assert not SHELL_REMEDY.search(message), message
    assert "Retry" in message


def test_no_chat_facing_module_tells_the_user_to_run_the_launch_cli():
    offenders = []
    for base in ("print_handy", "plugins"):
        for path in (ROOT / base).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if SHELL_REMEDY.search(line):
                    offenders.append(f"{rel}:{number}: {line.strip()}")
    assert offenders == [], "launch CLI remedy in chat-facing code:\n" + "\n".join(offenders)
