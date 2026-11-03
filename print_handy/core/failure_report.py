"""One classification for every model failure that reaches chat (#817).

A failure used to reach the operator as "stopped before it was ready" or as
a slicer's bare exit code. The operator could not tell which step broke, what
caused it, what to do, or whether the printer had already been sent
anything. This module is the single place that answers all four:

* **stage**: where the pipeline stopped (:data:`STAGES`);
* **cause**: what went wrong in plain words;
* **recovery**: the one thing to do next;
* **submitted**: ``NOTHING`` when the failure happened before anything left
  Print Handy, ``UNKNOWN`` when a file may already be on the printer.

Raise sites hand over the exception (and, for a slicer, the evidence it read
from its own logs via :func:`slicer_evidence`); the wording lives here. Every
cause and recovery is written in this module from a recognized pattern, so
nothing from an exception, a log or a path can ride into chat. The one
exception is :func:`safe_detail`, which may quote a single short sentence of
the slicer's own error text after it passes a strict filter. Raw detail stays
in the archived failure folder (``state/slice-failures/<id>``, see
:func:`failure_folders`) and in the log.

The AI repair offer (#818) attaches to :class:`Report`: ``signature`` names
which known damage was found, so a repair route can key on it without
re-reading the log.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .. import diagnostics, logs

#: The pipeline stages a model failure can stop at, with the words chat uses.
STAGES = {
    "intake": "intake",
    "parse": "parse",
    "repair": "repair",
    "color plan": "color plan",
    "plate plan": "plate plan",
    "slice": "slice",
    "upload": "upload",
    "launch": "launch",
}

#: Lead sentence for a failure that was recognized, per stage.
_HEADLINES = {
    "intake": "That model could not be prepared.",
    "parse": "That model file could not be read.",
    "repair": "That model could not be repaired.",
    "color plan": "The colors for that model could not be planned.",
    "plate plan": "That model could not be laid out on the plate.",
    "slice": "Slicing that model failed.",
    "upload": "Sending the file to the printer failed.",
    "launch": "Starting the print failed.",
}


class Submitted(str, Enum):
    NOTHING = "nothing"   # refused before anything was uploaded or started
    UPLOADED = "uploaded"  # the file reached the printer, but nothing is printing
    UNKNOWN = "unknown"   # submitted, and the printer's answer was not confirmed


@dataclass(frozen=True)
class Report:
    stage: str
    cause: str
    recovery: str
    submitted: Submitted = Submitted.NOTHING
    retryable: bool = False
    #: The id of the recognized signature, or None. Part of the #818 seam.
    signature: str | None = None
    #: False for the honest "no specific cause is known" fallback.
    classified: bool = True
    #: The diagnostics category a recognized signature implies (marker suffix); None lets
    #: diagnostics.classify decide from the exception.
    category: diagnostics.Category | None = None

    def as_dict(self) -> dict:
        """The record stored in receipts: plain strings only."""
        return {"stage": self.stage, "cause": self.cause, "recovery": self.recovery,
                "submitted": self.submitted.value, "signature": self.signature}


def _signature_row(sig_id, stage, pattern, cause, recovery, retryable=False):
    return (sig_id, stage, re.compile(pattern, re.I), cause, recovery, retryable)


_BUNDLE_TAIL = " If it fails again, run `/print-handy bundle` and share the bundle."

#: Known slicer failure signatures, most specific first. The pattern runs over
#: the slicer's logs at the raise site; the cause and recovery are ours.
_SIGNATURES = (
    _signature_row(
        "model_unreadable", "parse",
        r"can ?not find object id|invalid 3mf format|archive does not contain a valid model"
        r"|input model file.{0,40}can ?not be parsed|failed to (load|parse) (the )?(3mf|model)",
        "The slicer could not read this 3MF file. Its model data is damaged or laid out in a "
        "way the slicer does not understand.",
        "Re-export the model as a fresh 3MF (or send the STL) from the program that made it, "
        "then send it again." + _BUNDLE_TAIL),
    _signature_row(
        "no_volume", "parse",
        r"no printable volume|zero-volume|empty triangle table|no printable mesh objects",
        "This file has no solid volume. The mesh is flat, zero-thickness or empty, so there "
        "is nothing to print.",
        "Re-export the model as a solid, closed mesh (give it real thickness), then send it again."),
    _signature_row(
        "bad_3mf_structure", "parse",
        r"unresolved 3mf|cyclic 3mf|3mf component cycle|missing 3mf object|ambiguous 3mf object"
        r"|duplicate (or missing )?3mf|invalid 3mf (plate|build|filament|transform)|3mf metadata"
        r"|3mf has no plate assignments|plate assignments|3mf object count|3mf plate",
        "The 3MF file's internal structure is inconsistent (for example a part that refers to "
        "an object that is missing or loops back on itself), so its parts cannot be told apart.",
        "Re-export the model as a fresh 3MF (or send the STL) from the program that made it, "
        "then send it again." + _BUNDLE_TAIL),
    _signature_row(
        "unsupported_format", "parse",
        r"only stl(/| and )3mf|not geometry|requires a 3mf source",
        "That file is not a model Print Handy can slice. Only STL and 3MF files are supported.",
        "Send the model as an .stl or .3mf file."),
    _signature_row(
        "unclear_units", "intake",
        r"length units|unrecognized input unit|units must be mm or inch|unit confirmation",
        "The model's measurement units are missing or unclear, so its real size is not known.",
        "Send it again and answer the millimeters or inches question, or re-export with units set."),
    _signature_row(
        "untuned_nozzle", "intake",
        r"no tuned profile exists|templates are tuned for",
        "No tuned slicing profile exists for the selected nozzle size.",
        "Select a nozzle size with a tuned profile, then send the model again."),
    _signature_row(
        "too_complex", "intake",
        r"safety ceiling|byte ceiling|element count exceeds",
        "The model is too large or detailed to process safely.",
        "Simplify or reduce the mesh resolution in your modeling program, then send it again."),
    _signature_row(
        "outside_bed", "plate plan",
        r"outside (of )?the (print|build) ?(volume|plate|bed|area)"
        r"|exceeds? the (plate|bed|print volume|build volume)|exceeds printer volume"
        r"|too (large|big) to fit|(object|model)s? (is|are) (too large|larger than)",
        "The model does not fit inside the printer's build volume at the size and position it "
        "was given.",
        "Pick a smaller size when asked, scale the model down, or send a smaller model."),
    _signature_row(
        "empty_plate", "plate plan",
        r"nothing to be sliced|print is empty|no objects? (were )?(found|to slice)"
        r"|no (printable )?objects? (is |are )?(fully )?inside",
        "The slicer found nothing to print on the plate. The model is empty, hidden, or sits "
        "outside the bed.",
        "Check that the model has solid geometry and is not off the plate, then send it again."),
    _signature_row(
        "thin_features", "slice",
        r"failed slicing the model|return_code[ =:]*-100\b",
        "The slicer could not build toolpaths for part of the model, usually features thinner "
        "than one nozzle line.",
        "Thicken the thin walls or fine details in the model, or send a different version."),
    _signature_row(
        "out_of_memory", "slice",
        r"bad_alloc|out of memory|cannot allocate memory|memory allocation",
        "The slicer ran out of memory on this model.",
        "Free memory on the host or send a simpler, lower-resolution model, then retry once.",
        True),
)

SIGNATURE_IDS = frozenset(row[0] for row in _SIGNATURES)
_BY_ID = {row[0]: row for row in _SIGNATURES}


def match_signature(text: str, return_code=None) -> str | None:
    """The id of the first known signature in ``text``, or None.

    ``return_code`` is the slicer's own result code (Orca's ``result.json``
    ``return_code``); -6 means the input could not be parsed.
    """
    text = str(text or "")
    for sig_id, _stage, pattern, *_rest in _SIGNATURES:
        if pattern.search(text):
            return sig_id
    if return_code == -6:
        return "model_unreadable"
    return None


_DETAIL_LIMIT = 140
_DETAIL_REFUSED = re.compile(
    r"traceback|exception|\bat 0x|[/\\]|\b[A-Za-z]:|\.(py|cpp|so|dll|log|json|3mf|stl)\b|"
    r"[\x00-\x08\x0b-\x1f]", re.I)


def safe_detail(text) -> str:
    """One short sentence of a slicer's own error text, or '' when it is not safe.

    Only the first line is considered. It is refused when it looks like a
    traceback, names a file or a path, has control characters, or is too long;
    otherwise it is redacted and returned. Chat quotes it as the slicer's words.
    """
    line = " ".join(str(text or "").split("\n")[0].split())
    if not line or len(line) > _DETAIL_LIMIT or _DETAIL_REFUSED.search(line):
        return ""
    return logs.redact(line).strip()


def slicer_evidence(log_text: str, error_string: str = "", return_code=None) -> dict:
    """What a slicer boundary learned from its own logs: ``signature`` and ``detail``.

    Returned as keyword arguments for :class:`SliceFailure`. Both are safe to
    store and to send over the worker wire; neither carries raw log text.
    """
    return {"signature": match_signature(f"{error_string}\n{log_text}", return_code),
            "detail": safe_detail(error_string)}


def from_signature(sig_id) -> Report | None:
    row = _BY_ID.get(sig_id)
    if row is None:
        return None
    _id, stage, _pattern, cause, recovery, retryable = row
    category = diagnostics.Category.TRANSIENT if retryable else diagnostics.Category.CONFIGURATION
    return Report(stage, cause, recovery, Submitted.NOTHING, retryable, sig_id, True, category)


def describe(exc, stage: str, *, transient: str | None = None, other: str | None = None) -> Report:
    """Classify ``exc`` at ``stage``.

    Order: a signature the raise site already read (``exc.signature``), a
    cause :mod:`diagnostics` recognizes, a signature in the exception's own
    text, then the honest fallback. ``transient`` and ``other`` are the
    caller's recovery wording for the fallback, as :func:`diagnostics.next_step_for`
    takes them.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown failure stage {stage!r}")
    report = from_signature(getattr(exc, "signature", None))
    if report is not None:
        return report
    known = diagnostics.known_cause(exc)
    if known is not None:
        return Report(stage, known.cause, known.remedy, Submitted.NOTHING,
                      known.category == diagnostics.Category.TRANSIENT)
    report = from_signature(match_signature(str(exc)))
    if report is not None:
        return report
    recovery = diagnostics.next_step_for(
        exc, transient=transient or "Try again; if it stops again, run `/print-handy bundle` "
                                    "and share the bundle so it can be looked into.",
        other=other or "Run `/print-handy bundle` and share the bundle so it can be looked into.")
    return Report(stage, "Print Handy hit a problem it has no specific explanation for.", recovery,
                  Submitted.NOTHING, diagnostics.classify(exc) == diagnostics.Category.TRANSIENT,
                  None, classified=False)


def state_line(report: Report) -> str:
    if report.submitted is Submitted.UNKNOWN:
        return ("The printer may already have the file or have started it; Print Handy could "
                "not confirm which.")
    if report.submitted is Submitted.UPLOADED:
        return "The file is on the printer, but nothing is printing."
    return "Nothing was uploaded or started."


def lead(report: Report, headline: str | None = None) -> str:
    """The first line: the headline, the stage, and the cause."""
    head = headline if headline is not None else _HEADLINES[report.stage]
    return f"{head} Stage: {STAGES[report.stage]}. {report.cause}"


def post(exc, stage: str, *, subsystem: str, headline: str | None = None,
         not_happened: str | None = None, transient: str | None = None,
         other: str | None = None) -> str:
    """The chat post for one failure at ``stage``.

    A recognized failure leads with its stage's own sentence. ``headline`` is
    the caller's lead for the unrecognized fallback only (callers keep their
    long-standing wording there). ``not_happened`` replaces the state line.
    """
    report = describe(exc, stage, transient=transient, other=other)
    first = lead(report, headline if not report.classified else None)
    return diagnostics.plain_post(first, not_happened=not_happened or state_line(report),
                                  next_step=report.recovery, subsystem=subsystem, exc=exc,
                                  category=report.category)


def start_report(*, refused: bool, printer_rejected: bool = False, upload_failed: bool = False) -> Report:
    """Classify a failed print-now run: ``upload`` or ``launch``, refused or unknown.

    ``refused`` is True only for an explicit terminal REFUSED line, which is the
    one thing that confirms nothing was uploaded or started. A printer that
    rejected the start request already has the file. Anything else is submitted
    with an unknown outcome.
    """
    if refused and printer_rejected:
        return Report("launch", "The printer rejected the start request.",
                      "Fix what the printer reported, then press Retry.", Submitted.UPLOADED)
    if refused:
        return Report("launch", "Print Handy refused to start before sending anything.",
                      "Fix the reported problem, then press Retry.", Submitted.NOTHING)
    return Report("upload" if upload_failed else "launch",
                  "The printer did not confirm whether the print started.",
                  "Check the printer screen before doing anything else, and do not press Start "
                  "again until you know.", Submitted.UNKNOWN)


def plain(report: Report) -> str:
    """A complete post body for ``report`` without a reference marker: stage and cause,
    what was or was not submitted, and the recovery."""
    return f"{lead(report, '').strip()}" + chr(10) + f"{state_line(report)} {report.recovery}"


def failure_folders(hermes_home, limit: int = 5) -> list[dict]:
    """The newest archived slice-failure folders, newest first.

    ``[{"name", "path", "modified"}]`` for the Web UI status page. Nothing here
    reads the files: the folders hold the raw slicer logs and stay on disk.
    """
    root = Path(hermes_home) / "state" / "slice-failures"
    try:
        rows = [(p.stat().st_mtime, p) for p in root.iterdir() if p.is_dir()]
    except OSError:
        return []
    rows.sort(key=lambda row: row[0], reverse=True)
    return [{"name": p.name, "path": str(p), "modified": mtime} for mtime, p in rows[:limit]]
