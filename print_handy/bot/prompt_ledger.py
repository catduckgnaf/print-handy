"""A small on-disk note of which button questions Discord messages carry.

Prompt state itself (``core.prompts``) lives in memory and dies with the
process. A restart therefore leaves buttons on old messages that resolve
nothing. This ledger keeps only enough identity to recognize such a button
later and say so plainly: where the message is, whether it was a print
approval, who it was asked of, and which saved intake it belonged to.

It is never an answer store. Nothing here restores a prompt, replays an
approval or starts anything: a ledger hit only lets the adapter retire the
dead buttons and explain what to do next. Failures never reach the caller.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)

#: Identifies this process. An entry from another boot was posted before a restart.
BOOT_ID = uuid.uuid4().hex

FILE_NAME = "prompt-ledger.json"  # keep in step with _path() (tests/test_state_files.py reads the literal)
MAX_AGE_S = 7 * 24 * 3600
MAX_ENTRIES = 500

_lock = threading.Lock()


def _path():
    from ..runtime import _config
    return _config.state_dir() / "prompt-ledger.json"


def _load(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)


def record(clarify_id, *, channel_id, message_id, approval=False, initiator=None, intake_id=None):
    """Note one posted question. Best effort; returns True when it was written."""
    entry = {"boot": BOOT_ID, "posted": time.time(), "channel_id": str(channel_id or ""),
             "message_id": str(message_id or ""), "approval": bool(approval),
             "initiator": str(initiator) if initiator else None,
             "intake_id": str(intake_id) if intake_id else None}
    try:
        with _lock:
            path = _path()
            data = _load(path)
            now = time.time()
            data = {k: v for k, v in data.items()
                    if isinstance(v, dict) and now - float(v.get("posted") or 0) <= MAX_AGE_S}
            data[str(clarify_id)] = entry
            if len(data) > MAX_ENTRIES:
                keep = sorted(data.items(), key=lambda kv: float(kv[1].get("posted") or 0))[-MAX_ENTRIES:]
                data = dict(keep)
            _save(path, data)
        return True
    except Exception:
        logger.debug("could not record the prompt ledger entry", exc_info=True)
        return False


def lookup(clarify_id):
    """The entry for a question this install posted, else None."""
    try:
        with _lock:
            entry = _load(_path()).get(str(clarify_id))
    except Exception:
        logger.debug("could not read the prompt ledger", exc_info=True)
        return None
    return entry if isinstance(entry, dict) else None


def forget(clarify_id):
    """Drop an entry (the question was answered or retired)."""
    try:
        with _lock:
            path = _path()
            data = _load(path)
            if data.pop(str(clarify_id), None) is not None:
                _save(path, data)
    except Exception:
        logger.debug("could not update the prompt ledger", exc_info=True)


def saved_job_state(intake_id):
    """The saved intake receipt's status for ``intake_id`` as a plain word, else None.

    Read only. Never returns paths, ids or anything that identifies the job.
    """
    if not intake_id or not str(intake_id).isalnum():
        return None
    try:
        from ..runtime import _config
        receipt = json.loads((_config.state_dir() / "model-link-intakes" / f"{intake_id}.json").read_text())
    except (OSError, ValueError, ImportError):
        return None
    if not isinstance(receipt, dict):
        return None
    status = str(receipt.get("status") or "")
    if receipt.get("started"):
        return "started"
    if status == "SLICED_NOT_STARTED":
        return "sliced"
    return "other" if status else None


def stale_notice(restarted, approval, saved):
    """What a person reads after pressing a button nothing is waiting behind.

    States that nothing changed, why the button is dead, and the safe way forward.
    It never offers to continue from the old button: the question is asked again.
    """
    why = ("Print Handy restarted after this question was posted, so it can no longer be answered."
           if restarted else "This question expired or was already answered, so it can no longer be answered.")
    if saved == "started":
        unchanged = ("Nothing changed. This click did not upload anything, start a print"
                     + (", or record approval." if approval else "."))
    else:
        unchanged = ("Nothing changed: no print was started, nothing was uploaded to the printer"
                     + (", and your approval was not recorded." if approval else "."))
    lines = [f"{why} {unchanged}", "I removed the dead buttons from that message."]
    if saved == "started":
        lines.append("That job had already started. Check /print-status.")
    else:
        if saved == "sliced":
            lines.append("The sliced file is saved but not printed.")
        lines.append("To go on, send the model link or file again. I will slice it again and ask for "
                     "the approval again; an approval never carries over a restart.")
    return " ".join(lines)
