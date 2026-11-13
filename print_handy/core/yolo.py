"""Yolo: "print it, skip the checks" (operator-vouched files).

Saying ``yolo`` with a file (or a model link) is the operator's explicit
statement that the file is good and the bed is clear. Two paths use it:

- An already-sliced G-code file (``.gcode`` or a ``.gcode.3mf`` package)
  goes straight to ``print-now --yolo``: no slicing, no questions.
- A model (STL/3MF or a link) runs the normal intake, but every question is
  answered automatically with its go option (a waiver such as Print anyway
  first, else the recommended first choice, never Cancel), and the start
  runs ``print-now --yolo``.

Every check still runs and every auto-answer is posted in chat, so nothing is
skipped silently; a failed check is reported instead of refusing (see
``model_job._waive``). Mechanics are never waived: a printer that is already
printing, the control lock, the launch lease, the upload checksum and the
start readback. Neither are the printer's mechanical limits (S6): a move
outside the axis limits, a toolpath whose bounds cannot be verified, or a
heater target above the printer's absolute maximum still refuses
(``model_job.require_mechanical_limits``). Only a configured print approver
may yolo (``may_yolo``); with none configured, nobody may.
"""

import contextvars
import re
import zipfile
from pathlib import Path

# A yolo request is an explicit command, not a word mentioned in ordinary
# prose. Allow the command alone or followed by an intake URL/text; require
# a token boundary after it so "YOLO" in a sentence cannot waive checks.
YOLO_RE = re.compile(r'(?<![\w-])yolo(?![\w-])', re.I)
YOLO_COMMAND_RE = re.compile(r'^\s*yolo(?:\s+|$)', re.I)
GCODE_SUFFIXES = ('.gcode', '.gcode.3mf')

# Set for the whole of one yolo intake (prepared_intake.prepare_link).
ACTIVE = contextvars.ContextVar('print_handy_yolo', default=False)

# Labels a yolo run takes over any other choice, most preferred first: the
# explicit waivers, then the start buttons.
_PREFERRED = (
    'Print anyway, accept the risk (bed is clear)',
    'Slice anyway (accept the risk)',
    'Print anyway',
    'Start next plate · bed is clear',
    'Slice and print now (bed is clear)',
    'Slice and print with supports (bed is clear)',
    'Slice and print with supports, not recommended (bed is clear)',
    'Re-slice with supports',
)
_NEGATIVE = re.compile(r'^(cancel|stop|no\b|do not|don\'t|not now|skip)|\(cancel\)', re.I)
# Choices yolo never takes on its own: they act on the printer or change the
# model (a forced restart wipes Resume; a rescale changes the part). A question
# offering one of these, and none of the go labels above, goes to a person.
_NEVER = re.compile(r'restart|reboot|firmware|bigger|smaller|scale|delete|wipe|discard', re.I)
# Largest plate G-code yolo extracts from a .gcode.3mf package (zip-bomb guard).
MAX_GCODE_BYTES = 1024 * 1024 * 1024


def requested(text):
    """True when the message starts with an explicit yolo command."""
    return bool(YOLO_COMMAND_RE.match(str(text or '')))


def strip(text):
    """The message without its leading yolo command."""
    return re.sub(r'\s{2,}', ' ', YOLO_RE.sub(' ', str(text or ''))).strip()


def is_gcode_name(name):
    low = str(name or '').lower()
    return low.endswith(GCODE_SUFFIXES)


def gcode_file(event):
    """The single dropped G-code file (.gcode or .gcode.3mf), else None."""
    hits = [Path(str(p)) for p in (getattr(event, 'media_urls', None) or []) if is_gcode_name(p)]
    return hits[0] if len(hits) == 1 and hits[0].is_file() else None


def auto_answer(choices):
    """The index a yolo run picks: a preferred go label, else the first non-cancel choice.

    None when a person must answer: the question offers a printer action or
    a model change (``_NEVER``) and none of the go labels.
    """
    labels = [str(c) for c in choices]
    for want in _PREFERRED:
        if want in labels:
            return labels.index(want)
    if any(_NEVER.search(label) for label in labels):
        return None
    for i, label in enumerate(labels):
        if not _NEGATIVE.search(label.strip()):
            return i
    return 0


# The Web UI chat's one user. The web chat is built with this operator as
# its approval user (bot/web.py), so it approves prints there, and yolos.
WEB_OPERATOR = 'webui'


def _approvers(platform, cfg):
    from .. import settings as _settings
    if cfg is None:
        from ..runtime import _config
        cfg = _config.cfg()
    section = (cfg or {}).get(str(platform or 'discord')) or {}
    return section, _settings.approver_ids(section)


def may_yolo(user_id, platform='discord', cfg=None):
    """Whether this sender may yolo: only a configured print approver (S6).

    Stricter than answering a print approval on purpose: yolo waives every
    check that is not a mechanical limit, so it is never open to everyone by
    default. With no ``approval_user_id`` configured on the platform nobody
    may yolo there (``approval_all_users`` counts only once at least one
    approver is configured, as the explicit choice to let everyone
    approve). The web chat's operator is that platform's approval user by
    construction, exactly as for print approvals there.
    """
    platform = str(platform or 'discord')
    if platform == WEB_OPERATOR:
        return user_id is not None and str(user_id) == WEB_OPERATOR
    section, approvers = _approvers(platform, cfg)
    if not approvers:
        return False
    return bool(section.get('approval_all_users') is True
                or (user_id is not None and str(user_id) in approvers))


def refusal(platform='discord', cfg=None):
    """Why yolo was refused, saying how to allow it when no approver is set."""
    platform = str(platform or 'discord')
    base = 'Yolo skips checks, so only a print approver can use it'
    try:
        configured = platform == WEB_OPERATOR or bool(_approvers(platform, cfg)[1])
    except Exception:  # an unreadable config: say only the rule
        configured = True
    if not configured:
        return (f'{base}, and none is configured for {platform}. Set [{platform}] approval_user_id '
                f'to your user ID (Web UI Settings, or PRINT_HANDY__{platform.upper()}__APPROVAL_USER_ID) '
                'to allow it.')
    return base + '.'


def extract_gcode(package, dest_dir):
    """The plate G-code inside a .gcode.3mf package, written next to dest_dir.

    Returns (gcode_path, plate_count). Plate 1 is used when the package holds
    several plates; the caller says so.
    """
    with zipfile.ZipFile(package) as archive:
        plates = sorted((n for n in archive.namelist() if re.fullmatch(r'Metadata/plate_\d+\.gcode', n)),
                        key=lambda n: int(re.search(r'(\d+)', n.rsplit('/', 1)[1]).group(1)))
        if not plates:
            raise ValueError(f'{Path(package).name} has no plate G-code inside (Metadata/plate_N.gcode)')
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'\.gcode\.3mf$', '', Path(package).name, flags=re.I)
        out = dest_dir / f'{stem}.gcode'
        info = archive.getinfo(plates[0])
        if info.file_size > MAX_GCODE_BYTES:
            raise ValueError(f'{plates[0]} is {info.file_size} bytes uncompressed, over the '
                             f'{MAX_GCODE_BYTES} byte limit')
        written = 0
        with archive.open(info) as src, open(out, 'wb') as dst:
            while chunk := src.read(1 << 20):
                written += len(chunk)
                if written > MAX_GCODE_BYTES:  # a lying header does not get past the cap
                    dst.close()
                    out.unlink(missing_ok=True)
                    raise ValueError(f'{plates[0]} expands past the {MAX_GCODE_BYTES} byte limit')
                dst.write(chunk)
    return out, len(plates)
