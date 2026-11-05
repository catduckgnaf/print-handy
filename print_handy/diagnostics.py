"""Error taxonomy, greppable markers and operator messages (#390, Stage 1).

The diagnostics overhaul's foundation. Before #390, Print Handy's worst
failures were silent or unhelpful ones (#373, #364, #384): the system knew
something was wrong and said nothing useful about it. This module is the
shared vocabulary every later stage uses:

* a small **taxonomy** — every failure at a main boundary is one of
  ``transient``, ``configuration``, ``hardware`` or ``programming``
  (``unknown`` when nothing honest can be said yet);
* a stable, greppable **marker** per (subsystem, category) pair, in the
  spirit of the ``DISCORD_STARTUP_RETRY`` log line from #369, so a support
  conversation can start from one searchable string;
* an **operator message** builder enforcing the Discord-message bar from
  #390 scope 1: what failed, the most likely cause, and what the operator
  can do about it — never bare exception text, never a silent drop.

Nothing here changes print-path behaviour: classification and formatting
only, and every string that leaves this module is passed through
``print_handy.logs.redact`` so no secret material can ride along.
"""
from __future__ import annotations

import errno
import re
import socket
from enum import Enum

from . import logs


class Category(str, Enum):
    TRANSIENT = "transient"
    CONFIGURATION = "configuration"
    HARDWARE = "hardware"
    PROGRAMMING = "programming"
    UNKNOWN = "unknown"


#: Errnos that mean "the network hiccuped", mirroring the Discord startup
#: retry classification in print_handy.bot.client (#364).
TRANSIENT_ERRNOS = frozenset({
    getattr(socket, "EAI_AGAIN", -3),
    errno.EAGAIN, errno.EWOULDBLOCK, errno.ECONNABORTED, errno.ECONNREFUSED,
    errno.ECONNRESET, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT,
})

#: Default remedy per category, used when a boundary has nothing more
#: specific to say. Wording is operator-facing: an action, not a diagnosis.
DEFAULT_REMEDIES = {
    Category.TRANSIENT: "Wait a moment and try again; if it keeps happening, "
                        "check the network connection between Print Handy and the service.",
    Category.CONFIGURATION: "Check the named setting in your Print Handy configuration, "
                            "then run `python3 -m print_handy.doctor` (or `/print-handy debugging`) to confirm.",
    Category.HARDWARE: "Check the printer and its connection; no print was started by this message.",
    Category.PROGRAMMING: "This looks like a Print Handy bug. Please file a bug report with the "
                          "reference marker below and the diagnostics bundle (see docs/DIAGNOSTICS.md).",
    Category.UNKNOWN: "Run `python3 -m print_handy.doctor` (or `/print-handy debugging`) for a "
                      "check-by-check diagnosis, and include the reference marker below in any report.",
}

#: Markers that predate this module and must never be renamed: support
#: history and tests already search for them.
LEGACY_MARKERS = {
    "DISCORD_STARTUP_RETRY": "Discord login failed transiently at startup and is being retried (#364/#369).",
}

_SUBSYSTEM_RE = re.compile(r"[^A-Z0-9]+")


def _token(text: str) -> str:
    return _SUBSYSTEM_RE.sub("_", str(text).upper()).strip("_") or "GENERAL"


def marker(subsystem: str, category: Category) -> str:
    """The stable greppable marker for one subsystem/category pair.

    Example: ``marker("spoolman", Category.TRANSIENT)`` is
    ``"PH_SPOOLMAN_TRANSIENT"``. Markers are append-only within a
    naming generation: once a marker has shipped in a log line or a
    user-facing message it is not renamed again, so support threads
    stay searchable. The one rename that has happened is #590: the
    ``PB_`` prefix (Print Buddy, the old product name) became ``PH_``
    (Print Handy); docs/DIAGNOSTICS.md carries the old -> new alias
    table so old reports stay searchable too.
    """
    return f"PH_{_token(subsystem)}_{Category(category).value.upper()}"


#: The documentation site's base URL — the one address every error-code
#: link is built from (#924). It is the Pages site the README links to
#: and scripts/check_release_surfaces.py gates releases on; the site
#: repo (catduckgnaf/print-handy-docs) re-syncs its pages from this
#: repo's docs/ folder, so docs/DIAGNOSTICS.md here is the source the
#: anchors below resolve on.
DOCS_BASE_URL = "https://catduckgnaf.github.io/print-handy-docs/"
DIAGNOSTICS_URL = DOCS_BASE_URL + "diagnostics"

#: Where a code with no catalog section of its own links instead: the
#: general reference-codes section of the diagnostics page.
REFERENCE_CODES_ANCHOR = "reference-codes"


def anchor_for(code: str) -> str:
    """The docs anchor for one error code (#924): the deterministic slug
    rule — the code lowercased, underscores turned into hyphens. Every
    catalog heading in docs/DIAGNOSTICS.md carries this anchor
    explicitly, and tests/test_error_code_docs.py pins registry and
    catalog to each other, so a built link always resolves."""
    return str(code).strip().lower().replace("_", "-")


#: The (subsystem, category) pairs the production boundaries actually
#: emit, from the #924 whole-repo inventory: every subsystem passed to
#: plain_post / operator_message / marker in print_handy and
#: plugins/print-feedback, plus the doctor, debug-mode and startup-exit
#: codes. Categories are the ones reachable at that boundary:
#: classify() never returns hardware, so it appears only for printer,
#: the one boundary that may pass it explicitly for a physical failure.
#: A pair outside this table still formats (marker() is total), but it
#: links to the general section — see docs_url_for.
MARKER_CODE_CATEGORIES = {
    "discord": (Category.CONFIGURATION, Category.TRANSIENT),
    "spoolman": (Category.CONFIGURATION, Category.TRANSIENT, Category.UNKNOWN),
    "intake": (Category.CONFIGURATION, Category.TRANSIENT,
               Category.PROGRAMMING, Category.UNKNOWN),
    "slicing": (Category.CONFIGURATION, Category.TRANSIENT,
                Category.PROGRAMMING, Category.UNKNOWN),
    "printer": (Category.CONFIGURATION, Category.TRANSIENT,
                Category.PROGRAMMING, Category.UNKNOWN, Category.HARDWARE),
    "debug": (Category.CONFIGURATION, Category.TRANSIENT,
              Category.PROGRAMMING, Category.UNKNOWN),
    "logging": (Category.CONFIGURATION,),
    "listener": (Category.TRANSIENT,),
}

#: Every error code with its own section in docs/DIAGNOSTICS.md, mapped
#: to a short title. The single source of truth for which codes exist:
#: the marker codes are built from MARKER_CODE_CATEGORIES above, and
#: print_handy.core.failure_report registers its signature codes here
#: at import (register_error_codes). The drift test parses the docs
#: page and asserts this registry and the catalog anchors match both
#: ways, so the two cannot silently drift apart.
ERROR_CODES = {
    marker(subsystem, category): f"{subsystem} {category.value}"
    for subsystem, categories in MARKER_CODE_CATEGORIES.items()
    for category in categories
}


def register_error_codes(codes: dict) -> None:
    """Add codes owned by another module to the registry (#924).

    Used by print_handy.core.failure_report for its signature codes;
    registration is what makes docs_url_for link a code to its own
    catalog section instead of the general fallback.
    """
    ERROR_CODES.update(codes)


def docs_url_for(code: str) -> str:
    """The diagnostics-page URL for one error code (#924).

    A registered code links to its own catalog anchor; an unregistered
    code (a subsystem/category pair no current boundary emits) links to
    the general reference-codes section rather than an anchor that
    does not exist.
    """
    anchor = anchor_for(code) if code in ERROR_CODES else REFERENCE_CODES_ANCHOR
    return f"{DIAGNOSTICS_URL}#{anchor}"


def reference_line(code: str) -> str:
    """The chat `Reference:` tail for one code (#924): a Markdown link
    whose text is the raw code itself, so the PH_ token stays in the
    raw text and existing log/chat greps for it keep working."""
    return f"Reference: [{code}]({docs_url_for(code)})"


def plain_reference_tail(code: str) -> str:
    """The `Reference:` tail for plain-text surfaces (#924): the bare
    code followed by its URL in parentheses — terminals, the doctor
    and other non-Markdown output, where link syntax would show raw."""
    return f"Reference: {code} ({docs_url_for(code)})"


def bracket_reference(code: str) -> str:
    """The doctor-row form of a code (#924): the established bracket
    token, then the plain URL in parentheses."""
    return f"[{code}] ({docs_url_for(code)})"


class KnownCause:
    """A recognised, operator-fixable cause for one failure (#590).

    ``cause`` says in plain words what went wrong and ``remedy`` says
    what to do about it; both are written here, in this module, from
    the recognised pattern — never copied out of raw exception text,
    so nothing internal (paths beyond the named token files, secrets,
    tracebacks) can ride along into chat.
    """

    __slots__ = ("category", "cause", "remedy")

    def __init__(self, category: Category, cause: str, remedy: str):
        self.category = Category(category)
        self.cause = cause
        self.remedy = remedy


#: Model sites whose curated token/auth failures carry a specific,
#: user-safe message (print_handy.core.model_job raises them): the
#: site name, the credential the operator must set up, and the
#: docs/PROJECT-REFERENCE.md section that explains it.
_SITE_CREDENTIALS = (
    ("MakerWorld", "MakerWorld account token", "MakerWorld account token"),
    ("Thingiverse", "Thingiverse App Token", "Thingiverse App Token"),
    ("MyMiniFactory", "MyMiniFactory sign-in", "MyMiniFactory sign-in"),
    ("Cults3D", "Cults3D API key", "Cults3D API key"),
    ("Creality Cloud", "Creality Cloud login", "Creality Cloud login token"),
    ("Thangs", "Thangs login token", "Thangs login token"),
    ("Nexprint", "Nexprint login token", "Nexprint login token"),
)

_PID_LIMIT_PHRASE = "container is at its process limit"

_CREDENTIAL_CUES = ("token", "api key", "app token", "credentials", "nickname")
_CREDENTIAL_PROBLEMS = ("need", "missing", "empty", "refused", "401", "403",
                        "must contain", "invalid", "expired", "no api key",
                        "needs an api key first", "is wrong")

#: Download gaps that are a property of the site, not of the install:
#: the remedy is a browser download, never a retry. Each entry is the
#: site name, the phrasing cue in its curated gap message, and the cause.
_SITE_DOWNLOAD_GAPS = (
    ("MakerOnline", "cannot download",
     "MakerOnline's downloads need a browser security check that Print Handy "
     "does not fake, so the file cannot be fetched from here."),
    ("Cults3D", "cannot download",
     "Cults3D's API gives a listing's details but never its 3D files, so the "
     "file cannot be fetched from here."),
    # Creality Cloud (#605 follow-on): memberDownload answered, but no file
    # URL could be found in the answer (model_job.CREALITYCLOUD_ANSWER_GAP).
    ("Creality Cloud", "could not find a file url",
     "Creality Cloud answered the download request, but Print Handy could "
     "not find a file URL in the answer, so nothing was downloaded (the "
     "site may have changed its answer)."),
)

#: Creality Cloud (#605 follow-on): the model the link names is gone —
#: the API's 512/517 envelope ("says this model does not exist") and the
#: inventory lookup ("model not found for …") say the same thing.
_CREALITY_MISSING_CUES = ("says this model does not exist", "model not found for")

#: Refusals raised by the color plate exporters in core/color_geometry.py
#: (#716). Each names a model layout the exporter cannot split by color.
_COLOR_PLATE_CUES = (
    "single-color plate requires",
    "production subset expects",
    "geometry and selection inventory disagree",
    "selected build object also contains an unselected part",
    "derived plate slot color requires project settings",
)

#: Direct file links (#607, #608 follow-on): the curated DirectLinkError
#: cases from print_handy.core.model_job, as (phrasing cue, category,
#: cause, remedy). Recognition is gated on the exception type name (the
#: SpoolmanUnavailable trick), so lookalike text from another boundary
#: cannot claim these causes; the cause/remedy wording is built here,
#: never copied from the exception text — the original message can name
#: the operator's host, which stays out of chat. The HTTP-status case is
#: handled separately below because its category depends on the code.
_DIRECT_LINK_CAUSES = (
    ("is not a public address", Category.CONFIGURATION,
     "That link points at an address that is not public, so Print Handy "
     "will not fetch it.",
     "Use a public link anyone on the internet can open."),
    ("must start with http://", Category.CONFIGURATION,
     "That link does not start with http:// or https://, so it cannot be "
     "fetched as a direct file link.",
     "Send an http:// or https:// link to the file."),
    ("has a user name or password in it", Category.CONFIGURATION,
     "That link carries a user name or password in it, and links like "
     "that are refused.",
     "Share the file with a public link that needs no sign-in."),
    ("has no host name", Category.CONFIGURATION,
     "That link has no host name, so there is nothing to fetch.",
     "Send the complete link to the file."),
    ("has no address to fetch", Category.CONFIGURATION,
     "That direct link has no address to fetch.",
     "Send the complete link to the file."),
    ("could not look up the host", Category.TRANSIENT,
     "Print Handy could not look up the host for that link, so the file "
     "was not downloaded.",
     "Check the link and the network connection, then send the link again."),
    ("could not reach the host", Category.TRANSIENT,
     "Print Handy could not reach the host for that link, so the file "
     "was not downloaded.",
     "Check the link and the network connection, then send the link again."),
    ("redirected too many times", Category.CONFIGURATION,
     "That link redirected too many times, so the file was not downloaded.",
     "Send a link that points at the file more directly."),
    ("returned a web page instead of a print file", Category.CONFIGURATION,
     "That link returned a web page instead of a print file, so nothing "
     "was saved as a model.",
     "Use the host's direct download link; a Dropbox share link works as-is."),
    ("is not a 3mf file", Category.CONFIGURATION,
     "The file at that link is not a 3MF file, so it was not saved.",
     "Check that the link points at the model's .3mf file itself."),
    ("which is not a print file", Category.CONFIGURATION,
     "That link served a file that is not a print file Print Handy can use.",
     "Check that the link points at the model's .stl or .3mf file itself."),
    ("downloaded as an empty file", Category.CONFIGURATION,
     "That link downloaded as an empty file, so nothing was saved.",
     "Check that the link points at the model's .stl or .3mf file itself."),
)

#: The one DirectLinkError whose category depends on data inside the
#: curated text: "answered with HTTP {code}" (#608). DirectLinkError
#: discards the HTTPError, so classify()'s ``code`` attribute path can
#: never see the status; it is parsed out of the curated text here.
_DIRECT_HTTP_RE = re.compile(r"answered with http (\d{3})")


def known_cause(exc: BaseException) -> KnownCause | None:
    """The recognised operator-fixable cause of ``exc``, or None.

    Recognition is an allowlist over the curated, operator-phrased
    messages Print Handy itself raises at its model-site boundaries
    (#590): a missing/expired/refused site credential, a site robot
    check, or a site's built-in download gap. Two later boundaries fold
    into the same allowlist: Creality Cloud's answer gap and missing
    model (#605), and the direct file link refusals (#607), recognised
    by the ``DirectLinkError`` type name over its curated phrasing.
    Anything else returns None and keeps the conservative ``unknown``
    treatment — an unrecognised exception's text is never surfaced in
    chat.
    """
    text = str(exc or "")
    low = text.lower()
    # The container's PID limit (print_handy.core.resources). Matched on the stable phrase as well as
    # the type, because a worker process reports its failure to the parent as message text only.
    error_type = getattr(exc, 'worker_error_type', type(exc).__name__)
    if error_type == "ResourceLimitError" or _PID_LIMIT_PHRASE in low:
        return KnownCause(
            Category.CONFIGURATION,
            "The container hit its process limit (PID limit), so the slicer or a worker "
            "could not start.",
            "Remove the PID limit from the container (--pids-limit in the Unraid Extra "
            "Parameters, pids_limit in Compose) or raise it to 4096 or more, restart the "
            "container, then try again. `python3 -m print_handy.doctor` shows the current usage.")
    for site, cue, gap in _SITE_DOWNLOAD_GAPS:
        if site.lower() in low and cue in low:
            return KnownCause(
                Category.CONFIGURATION,
                gap,
                "Download the .stl or .3mf file from the model page in a browser, "
                "then upload or drop that file here instead.")
    if "creality cloud" in low and any(cue in low for cue in _CREALITY_MISSING_CUES):
        return KnownCause(
            Category.CONFIGURATION,
            "Creality Cloud says that model does not exist — it may have "
            "been removed, or the link is wrong.",
            "Check the link on the Creality Cloud model page, then send "
            "the corrected link.")
    for site, credential, section in _SITE_CREDENTIALS:
        if site.lower() not in low:
            continue
        challenged = "robot check" in low or "cloudflare" in low or "challenging" in low
        if challenged and "refused" not in low and "401" not in low and "403" not in low:
            return KnownCause(
                Category.TRANSIENT,
                f"{site} is challenging this connection with a robot check, so "
                "Print Handy could not fetch the model.",
                f"Open {site} in a browser and pass any check there, and check the "
                f"{credential} (see \"{section}\" in docs/PROJECT-REFERENCE.md). "
                "Print Handy does not try to bypass robot checks.")
        if not any(cue in low for cue in _CREDENTIAL_CUES):
            continue
        if any(cue in low for cue in _CREDENTIAL_PROBLEMS):
            return KnownCause(
                Category.CONFIGURATION,
                f"{site} links need your {credential}, and it is missing, empty "
                "or was refused.",
                f"Set up the {credential} — see \"{section}\" in "
                "docs/PROJECT-REFERENCE.md. Until it is in place, "
                f"{site} links cannot be fetched.")
    if "screenless user offset has no verified matching authority" in low:
        return KnownCause(
            Category.CONFIGURATION,
            "The printer is running screenless (no native screen, for example Helix), but "
            "Print Handy's confirmed Z offset is from the native screen or no longer matches "
            "the offset the printer reports. It will not borrow the other mode's offset.",
            "Confirm the current screenless Z offset with /calibration, then send the print "
            "again. Retrying the slice will not help.")
    if "native calibration authority is not operator confirmed" in low or (
            "native zprobeoffset" in low and "operator-confirmed baseline" in low):
        return KnownCause(
            Category.CONFIGURATION,
            "The printer is using its native screen, but Print Handy's confirmed calibration is "
            "for screenless mode or no longer matches the screen's Z offset.",
            "Confirm the native screen's Z offset with /calibration, then send the print "
            "again. Retrying the slice will not help.")
    if "3mf declares the length unit" in low:
        # A 3MF unit that intake could not convert exactly (core/threemf_canonical.py, #825).
        # The refusal text names the unit and the remedy, so it is configuration, never unknown.
        return KnownCause(
            Category.CONFIGURATION,
            "This 3MF uses a length unit Print Handy cannot convert exactly.",
            "Re-export the model in millimeters (or inches, centimeters, meters, feet or microns "
            "in the main model) and send it again.")
    if any(cue in low for cue in _COLOR_PLATE_CUES):
        # Color plate export (#716): the multicolor exporters refuse a
        # source layout they cannot split into single-color plates. Name it
        # instead of falling back to PH_SLICING_UNKNOWN.
        return KnownCause(
            Category.PROGRAMMING,
            "Print Handy could not split this model's colors into single-color plates.",
            "Choose One color for everything to print it now, and run /print-handy bundle "
            "so the color layout can be looked into.")
    if error_type == "SpoolmanUnavailable":
        if "not configured" in low:
            return KnownCause(
                Category.CONFIGURATION,
                "Spoolman is not configured: services.spoolman_url is empty.",
                "Set services.spoolman_url to your Spoolman service, then try again.")
        return KnownCause(
            Category.TRANSIENT,
            "Spoolman did not answer.",
            "Check that the Spoolman service is running and that "
            "services.spoolman_url points at it.")
    if error_type == "DirectLinkError":
        # Direct file links (#607): every curated case is recognised, so
        # a direct-link failure names its cause instead of falling back
        # to unknown. The HTTP case parses its status out of the text
        # (see _DIRECT_HTTP_RE); an unlisted status stays configuration,
        # matching the remedy (check the link is public and the file is
        # still there).
        http = _DIRECT_HTTP_RE.search(low)
        if http is not None:
            code = int(http.group(1))
            category = (Category.TRANSIENT if code == 429 or code >= 500
                        else Category.CONFIGURATION)
            return KnownCause(
                category,
                f"The host for that link answered with HTTP {code}, so "
                "the file was not downloaded.",
                "Check the link is public and the file is still there, "
                "then send it again." if category is Category.TRANSIENT else
                "Check the link is public and the file is still there.")
        for cue, category, cause, remedy in _DIRECT_LINK_CAUSES:
            if cue in low:
                return KnownCause(category, cause, remedy)
    return None


def classify(exc: BaseException, *, category: Category | None = None) -> Category:
    """Classify one failure. An explicit ``category`` always wins.

    The heuristic fallback is deliberately conservative: transport and
    timeout failures are transient, Print Handy's own settings errors are
    configuration, and anything unrecognised is ``unknown`` rather than
    a guessed cause — a boundary that knows better passes its category
    explicitly.
    """
    if category is not None:
        return Category(category)
    declared = getattr(exc, "diagnostic_category", None)
    if declared is not None:
        # A boundary's own exception type knows what it is (#390): e.g. a
        # deterministic slice refusal is configuration, never *_UNKNOWN.
        try:
            return Category(declared)
        except ValueError:
            pass
    from . import settings  # lazy: settings is imported by nearly everything
    if isinstance(exc, settings.SettingsError):
        return Category.CONFIGURATION
    known = known_cause(exc)
    if known is not None:
        return known.category
    code = getattr(exc, "code", None)  # urllib HTTPError and friends
    if isinstance(code, int):
        if code in (401, 403):
            return Category.CONFIGURATION
        if code == 429 or code >= 500:
            return Category.TRANSIENT
    reason = getattr(exc, "reason", None)  # urllib URLError wraps the real failure
    if isinstance(reason, BaseException) and reason is not exc:
        # Classify what actually failed (a refused connection, a reset, a
        # timeout): the wrapper itself is an OSError with no errno, so it
        # would otherwise fall through to UNKNOWN and suppress the retry
        # advice a recoverable failure earns. HTTPError never reaches
        # here with an exception reason — its reason is the message text.
        return classify(reason)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return Category.TRANSIENT
    # subprocess timeouts are not TimeoutError subclasses. Workers serialize this
    # category so the parent does not silently turn a timeout into *_UNKNOWN.
    import subprocess
    if isinstance(exc, subprocess.TimeoutExpired):
        return Category.TRANSIENT
    if isinstance(exc, (TypeError, AttributeError, AssertionError, NameError, ImportError)):
        return Category.PROGRAMMING
    if isinstance(exc, OSError):
        return Category.TRANSIENT if exc.errno in TRANSIENT_ERRNOS else Category.UNKNOWN
    return Category.UNKNOWN


def next_step_for(exc: BaseException, *, transient: str, other: str) -> str:
    """Pick a post's next step by whether retrying can actually help (#590).

    ``transient`` (worded by the caller, e.g. "send it again") is only
    honest for a transient failure; every other category gets
    ``other`` — for a known cause that is the cause's own remedy, and
    for an unknown failure the caller's non-retry step (typically the
    diagnostics bundle hint).
    """
    known = known_cause(exc) if exc is not None else None
    if known is not None:
        return known.remedy
    if exc is not None and classify(exc) == Category.TRANSIENT:
        return transient
    return other


def known_post(first_line: str, *, not_happened: str | None = None,
               subsystem: str, exc: BaseException) -> str | None:
    """A plain post naming the recognised cause of ``exc``, or None (#590).

    When :func:`known_cause` recognises the failure, the post leads
    with ``first_line``, states the cause in plain words, and gives
    the cause's remedy as the next step — no retry advice for a
    failure retrying cannot fix. Returns None when the cause is not
    recognised, so the caller falls back to its generic wording.
    """
    known = known_cause(exc)
    if known is None:
        return None
    return plain_post(f"{first_line} {known.cause}",
                      not_happened=not_happened,
                      next_step=known.remedy,
                      subsystem=subsystem, exc=exc)


def plain_post(first_line: str, *, not_happened: str | None = None,
               next_step: str | None = None, subsystem: str | None = None,
               exc: BaseException | None = None,
               category: Category | None = None,
               code: str | None = None) -> str:
    """One user-facing failure/refusal post meeting the #467 plain-language bar.

    The first line says in plain words what the person asked for and what
    happened — no exception class names, no raw exception or stderr text,
    no ALL-CAPS prefix, no internal jargon. ``not_happened`` states what
    did *not* happen where that matters ("Nothing was uploaded or
    started."), and ``next_step`` gives the one clear thing to do next.
    Technical detail is demoted, not deleted: when ``subsystem`` is named
    the post ends with the stable greppable ``PH_*`` marker (classified
    from ``exc`` when given, never its text), rendered since #924 as a
    link to the code's section in the diagnostics docs (the raw token
    stays in the text, so greps still hit), and the full detail stays in
    the log and the diagnostics bundle, where the caller already put it.

    This is the shared builder for chat posts; the Discord action
    handlers' ``_failure_post`` (#390) and the intake/guided surfaces use
    it so there is one wording system, not two.
    """
    lines = [str(first_line).strip()]
    tail = " ".join(part.strip() for part in (not_happened, next_step) if part and part.strip())
    if tail:
        lines.append(tail)
    if subsystem is not None:
        resolved = classify(exc, category=category) if exc is not None else (
            Category(category) if category is not None else Category.UNKNOWN)
        # #924: a caller that classified the failure more specifically
        # (failure_report's signature code) links that code instead of
        # the generic subsystem marker. Either way the tail is a link
        # whose text is the raw code, so greps for the token still hit.
        lines.append(reference_line(code if code is not None else marker(subsystem, resolved)))
    return logs.redact("\n".join(lines))


def operator_message(what: str, *, subsystem: str,
                     category: Category | None = None,
                     exc: BaseException | None = None,
                     cause: str | None = None,
                     remedy: str | None = None) -> str:
    """One user-facing failure message meeting the #390 bar.

    ``what`` names what failed (never a bare exception string). The result
    always carries the likely cause line (from ``cause``, else the
    exception type — never its raw text, which can embed secrets or
    internals), an actionable remedy line, and the greppable marker.
    The whole message is redacted before it is returned.
    """
    resolved = classify(exc, category=category) if exc is not None else (
        Category(category) if category is not None else Category.UNKNOWN)
    if cause is None:
        cause = (f"{type(exc).__name__} while talking to {subsystem}" if exc is not None
                 else "the cause is not known yet")
    lines = [f"⚠ {what}",
             f"Likely cause: {cause} ({resolved.value}).",
             f"What to do: {remedy or DEFAULT_REMEDIES[resolved]}",
             reference_line(marker(subsystem, resolved))]
    return logs.redact("\n".join(lines))
