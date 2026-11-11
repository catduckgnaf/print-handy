"""Printer firmware update state, guarded component updates and manual
update guidance, through Moonraker's Update Manager (issue #561).

Print Handy talks to many printer families, but exactly one programmatic
firmware path is safe enough to drive: Moonraker's Update Manager on
printers whose firmware Print Handy may treat as upstream — ZMod
(``firmware_family = zmod``) and vanilla Klipper (``firmware_family =
klipper`` from a non-fork vendor). Everything else in this module is
read-only reporting or plain-language guidance:

- **Stage 1 (check/report)** reads ``GET /machine/update/status`` and
  reports each component's current and available version. A user-invoked
  check may first ``POST /machine/update/refresh``; the plain read never
  refreshes (upstream Moonraker guidance is to refresh only on request —
  Moonraker also refreshes periodically by itself).
- **Stage 2 (guarded apply)** posts exactly one
  ``POST /machine/update/upgrade`` for one component, only after the
  operator confirms the exact ``current -> available`` change, only while
  the printer is idle (checked at prepare time and again at send time),
  and the outcome is verified by re-reading the status — a successful
  POST alone never counts as success. Eligibility is a property of the
  printer profile (see :func:`apply_block_reason`): vendor Klipper forks
  (Elegoo, Creality, QIDI, Anycubic) are refused even though they speak
  Moonraker, because driving a fork's Update Manager at upstream remotes
  is the vendors' documented breakage path (Elegoo says so explicitly
  for the Neptune 4).
- **Stage 3 (guides)** covers the two AD5X layers with no safe API —
  the ZMod base (USB / ``ZFLASH``) and the stock firmware underneath it
  (touchscreen / USB) — as step-by-step operator instructions. The
  guides never claim Print Handy performed any of those steps.
- **Stage 4 (guidance)** gives every other registry family a short
  statement of what Print Handy can report, the manual update steps,
  and why apply is not offered.

The transport is the spool_sync.moonraker client shape: a callable
taking ``(method, path, body=None)`` and returning the parsed JSON body.
"""
from __future__ import annotations

import inspect
import re
import time
import urllib.error
from dataclasses import dataclass, replace
from datetime import datetime, timezone

UPDATE_STATUS_PATH = "/machine/update/status"
UPDATE_REFRESH_PATH = "/machine/update/refresh"
UPDATE_UPGRADE_PATH = "/machine/update/upgrade"
# Moonraker's upgrade handler awaits the whole update before answering,
# so the POST is bounded by an upgrade-sized timeout, not the read-sized
# client timeout (15s in spool_sync.moonraker_for). Fifteen minutes
# covers ordinary component and system-package updates; the bounded
# verification re-reads remain the real outcome check either way.
UPGRADE_TIMEOUT_S = 900.0
SYSTEM_INFO_PATH = "/machine/system_info"
PRINT_STATE_PATH = "/printer/objects/query?print_stats=state"
MCU_STATE_PATH = "/printer/objects/query?mcu"

COULD_NOT_READ_TEXT = ("Could not read the printer's firmware state from Moonraker. "
                       "Nothing was sent to the printer.")
UPDATE_COULD_NOT_READ_TEXT = ("Could not read the printer's firmware state from Moonraker, so "
                              "there is no live status to update from. Nothing was sent "
                              "to the printer.")
READ_ONLY_NOTE = ("Read-only: Print Handy sent nothing to the printer but read requests. "
                  "To apply one update on an eligible printer, use "
                  "`/print-firmware update <component>` — it asks first and refuses while "
                  "a print is running.")
UPDATE_LABEL = "Update now"
CANCEL_LABEL = "Cancel"

# Klipper print_stats states in which no job is on the printer — the same
# set the macro run guard uses. Anything else — printing, paused, or a
# state Print Handy does not recognise — refuses the update (fail closed).
IDLE_STATES = frozenset({"standby", "complete", "cancelled", "error"})
BUSY_STATES = frozenset({"printing", "paused"})

# Apply eligibility (stage 2): upstream-tracking firmware families only,
# and never a vendor fork. The fork list is vendors, not models: a future
# Elegoo/Creality/QIDI/Anycubic Moonraker model is refused by the same
# rule until per-vendor evidence clears that vendor.
ELIGIBLE_FAMILIES = frozenset({"zmod", "klipper"})
FORK_VENDORS = frozenset({"elegoo", "creality", "qidi", "anycubic", "snapmaker"})


class FirmwareError(RuntimeError):
    """The printer's firmware state could not be read. Nothing was sent when this is raised."""


class FirmwareRefusal(RuntimeError):
    """An update was refused before anything was sent; the message is user-ready."""


# --------------------------------------------------------------------- profiles

def printer_entry(model_key, registry=None):
    """The printer registry entry for a model key, or None when unregistered."""
    if registry is None:
        from . import knowledge
        registry = knowledge.default_registry()
    for entry in registry.printers:
        if entry.key == model_key:
            return entry
    return None


def apply_block_reason(entry) -> str | None:
    """Why stage 2 apply is not offered for this registry entry, or None when it is.

    Eligibility is a property of the printer profile — firmware family,
    vendor and connection from the printer registry — never of what the
    printer's Update Manager happens to offer.
    """
    if entry is None:
        return ("Print Handy does not have this printer's profile in its registry, so it "
                "cannot judge whether driving its firmware updates is safe.")
    if entry.connection != "moonraker":
        return (f"This printer does not connect over Moonraker (it uses the "
                f"{entry.connection} connection), and Print Handy has no programmatic "
                f"firmware update path for that connection.")
    if entry.vendor in FORK_VENDORS:
        return (f"This printer runs {entry.vendor}'s own Klipper-based firmware — a vendor "
                f"fork, not upstream Klipper. Driving a vendor fork's Update Manager at "
                f"upstream releases is the vendors' documented breakage path (Elegoo "
                f"warns that updating a Neptune 4 to official Klipper/Moonraker makes the "
                f"machine not work properly), so Print Handy never applies firmware "
                f"updates on it.")
    if entry.firmware_family not in ELIGIBLE_FAMILIES:
        return (f"This printer's firmware family ({entry.firmware_family or 'none'}) is not "
                f"one Print Handy can update: only ZMod and vanilla Klipper printers are "
                f"eligible.")
    return None


def is_apply_eligible(entry) -> bool:
    return apply_block_reason(entry) is None


# --------------------------------------------------------------------- status

@dataclass(frozen=True)
class Component:
    """One Update Manager component: current vs available version."""

    name: str
    current: str | None = None
    remote: str | None = None
    package_count: int = 0
    # Where `current` came from: the Update Manager itself, or (ZMod only)
    # the OS distribution in /machine/system_info when the Update Manager
    # could not read the component's checkout.
    source: str = "update_manager"
    # Why Moonraker marks the component invalid (``is_valid: false``), for
    # instance a dirty repo. Moonraker refuses to update an invalid component,
    # so Print Handy never offers or requests one (#1295).
    invalid: str | None = None

    @property
    def update_available(self) -> bool:
        if self.package_count:
            return True
        return bool(self.remote) and self.remote != (self.current or "")

    @property
    def remote_unknown(self) -> bool:
        """True when Moonraker reported no readable available version ("?").

        Such a component is neither up to date nor behind: Print Handy cannot
        tell, and must not say "up to date" (live ZMod 1.7.5-2 reports "?"
        for every git component). The ``system`` row reports packages, not
        versions, so it is never unknown this way.
        """
        return self.name != "system" and not self.package_count and not self.remote


@dataclass(frozen=True)
class UpdateStatus:
    components: tuple = ()          # tuple[Component, ...]
    # Moonraker's own GitHub API budget (unauthenticated: 60 requests an hour
    # across every repo it checks). At 0, git and web components read "?"
    # until the reset time, a UNIX timestamp.
    github_remaining: int | None = None
    github_reset: float | None = None

    def component(self, name):
        # ZMod's Moonraker names the component "Z-Mod"; callers ask for "zmod".
        wanted = _component_key(name)
        for component in self.components:
            if _component_key(component.name) == wanted:
                return component
        return None

    @property
    def updates_available(self):
        return tuple(c for c in self.components if c.update_available)

    @property
    def remotes_unknown(self):
        return tuple(c for c in self.components if c.remote_unknown)


def _component_key(name):
    return re.sub(r"[^0-9a-z]", "", str(name or "").casefold())


def _version_str(value):
    if isinstance(value, str):
        value = value.strip()
        # "?" is Moonraker's placeholder for a version it could not read
        # (a git_repo component without a usable checkout, as on ZMod).
        return value if value and value != "?" else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def parse_update_status(payload) -> UpdateStatus:
    """Per-component versions from a Moonraker update-status response.

    Accepts the documented shape (``{"result": {"version_info": {name:
    {...}}}}``); the current version is read from ``version`` (or
    ``current_version``) and the available one from ``remote_version``
    (or ``available_version`` / ``latest_version``), so recorded variants
    replay unchanged. The ``system`` component reports a package count
    instead of versions. Anything structurally off raises FirmwareError
    rather than guessing at versions.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise FirmwareError("Moonraker's update-status response had no result")
    info = payload["result"].get("version_info")
    if not isinstance(info, dict) or not info:
        raise FirmwareError("Moonraker's update-status response had no version_info")
    components = []
    for name, raw in info.items():
        if not isinstance(raw, dict):
            raise FirmwareError(f"Moonraker's update-status entry for {name!r} was not an object")
        if not raw:
            # An empty entry is a placeholder, not a component: ZMod's Moonraker
            # lists `moonraker` and `klipper` as {} beside its real `moon` and
            # `klippy` entries (live AD5X, 2026-11-11, #1295).
            continue
        current = _version_str(raw.get("version")) or _version_str(raw.get("current_version"))
        remote = (_version_str(raw.get("remote_version"))
                  or _version_str(raw.get("available_version"))
                  or _version_str(raw.get("latest_version")))
        count = raw.get("package_count")
        components.append(Component(name=str(name), current=current, remote=remote,
                                    package_count=count if isinstance(count, int)
                                    and not isinstance(count, bool) else 0,
                                    invalid=_invalid_reason(raw)))
    remaining = payload["result"].get("github_requests_remaining")
    reset = payload["result"].get("github_limit_reset_time")
    return UpdateStatus(
        tuple(components),
        github_remaining=remaining if isinstance(remaining, int)
        and not isinstance(remaining, bool) else None,
        github_reset=float(reset) if isinstance(reset, (int, float))
        and not isinstance(reset, bool) else None)


def _invalid_reason(raw):
    """Moonraker's reason a component is invalid, or None when it is valid.

    Only an explicit ``is_valid: false`` counts. The reason is Moonraker's own
    warnings, joined, so the operator sees what Moonraker sees.
    """
    if raw.get("is_valid") is not False:
        return None
    warnings = [" ".join(str(w).split()) for w in raw.get("warnings") or ()
                if str(w).strip()]
    return "; ".join(warnings) or "Moonraker marks it invalid"


def _edited_moonraker_conf_hint(status, entry):
    """The fix when ZMod's own moonraker.conf was edited, or None.

    ZMod's moonraker.conf lives in the Z-Mod repo and starts "Do not edit this
    file. All edits should be made to mod_data/user.moonraker.conf". An
    installer that appends to it (HelixScreen's does) makes the repo dirty, and
    Moonraker then refuses to update ZMod.
    """
    if entry is None or entry.firmware_family != "zmod":
        return None
    zmod = status.component("zmod")
    if zmod is None or not zmod.invalid or "moonraker.conf" not in zmod.invalid:
        return None
    return ("ZMod's `moonraker.conf` has been edited (an installer such as "
            "HelixScreen's appends its own section to it), so Moonraker sees the "
            "Z-Mod repo as modified and will not update it. ZMod says added "
            "sections belong in `mod_data/user.moonraker.conf`. Moonraker "
            "write-protects files inside ZMod's repo, so neither Fluidd's editor "
            "nor Print Handy can fix this; it takes a shell on the printer (SSH): "
            "copy the added section into `mod_data/user.moonraker.conf`, run "
            "`git checkout -- moonraker.conf` in the Z-Mod repo (`/usr/data/zmod/zmod` "
            "on the AD5X), restart Moonraker, and run `/print-firmware` again.")


def _github_limit_note(status):
    """Why versions read "?" when Moonraker has used up its GitHub budget."""
    if status.github_remaining != 0 or not status.remotes_unknown:
        return None
    when = ""
    if status.github_reset:
        when = (" It resets at "
                + datetime.fromtimestamp(status.github_reset, timezone.utc).strftime("%H:%M UTC")
                + ".")
    return ("Moonraker has used up GitHub's hourly limit for update checks "
            "(60 an hour without a GitHub token), which is why latest versions "
            f"read as not reported.{when} Check again after that.")


def fetch_status(transport, *, refresh=False) -> UpdateStatus:
    """Read the update status through `transport`.

    With ``refresh=True`` (a user-invoked check) a refresh is requested
    first — the refresh response has the status shape, so it is parsed
    directly — and a failed refresh falls back to the plain status read
    rather than failing the report. The plain path makes one GET,
    plus a second to /machine/system_info when a `zmod` component has no
    readable version (_with_zmod_from_system_info). Any transport failure or malformed response raises
    FirmwareError; nothing is ever sent on this path beyond the refresh
    request itself.
    """
    if refresh:
        try:
            refreshed = parse_update_status(transport("POST", UPDATE_REFRESH_PATH))
        except Exception:
            pass  # fall through to the plain read below
        else:
            # The refresh response gets the same ZMod OS-info enrichment
            # as the plain read; the live /print-firmware handler always
            # reads with refresh=True.
            return _with_zmod_from_system_info(refreshed, transport)
    try:
        payload = transport("GET", UPDATE_STATUS_PATH)
    except FirmwareError:
        raise
    except Exception as exc:
        raise FirmwareError(f"the update-status request failed: {exc}") from exc
    return _with_zmod_from_system_info(parse_update_status(payload), transport)


def _with_zmod_from_system_info(status, transport):
    """Fill in an unreadable `zmod` component's version from the OS distribution.

    On the printer, ZMod's Update Manager entries are git_repo components
    with no usable checkout, so they report "?". The ZMod OS image names
    itself in /machine/system_info (``"zmod 1.7.5 -> 1.7.5-2"``), which is
    the installed version. Only a present `zmod` component without a
    version triggers the extra read; it is best-effort and never raises.
    """
    zmod = status.component("zmod")
    if zmod is None or zmod.current:
        return status
    try:
        version = zmod_version_from_system_info(transport("GET", SYSTEM_INFO_PATH))
    except Exception:
        return status
    if not version:
        return status
    filled = replace(zmod, current=version, source="system_info")
    return UpdateStatus(tuple(filled if c is zmod else c for c in status.components))


# ------------------------------------------------------- ZMod pairing and MCU

_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)")


def version_parts(version):
    """The leading dotted-numeric parts of a version string, as ints, or ()."""
    match = _VERSION_RE.match(str(version or "").strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def pairing_state(os_version, zmod_version) -> str:
    """"match", "mismatch" or "unknown" for the ZMod OS <-> zmod pairing rule.

    ZMod pairs its OS distribution and its ``zmod`` component in the
    first two version components (OS 1.9.4 needs ZMod 1.9.x). A mismatch
    is only claimed when both versions parse to at least two components
    and those differ; anything unreadable is "unknown", never a mismatch.
    """
    os_parts, zmod_parts = version_parts(os_version), version_parts(zmod_version)
    if len(os_parts) < 2 or len(zmod_parts) < 2:
        return "unknown"
    return "match" if os_parts[:2] == zmod_parts[:2] else "mismatch"


def os_version_from_system_info(payload):
    """The OS distribution version from a /machine/system_info response, or None."""
    try:
        distribution = payload["result"]["system_info"]["distribution"]
    except (TypeError, KeyError):
        return None
    if not isinstance(distribution, dict):
        return None
    return _version_str(distribution.get("version")) or _version_str(distribution.get("version_id"))


def zmod_version_from_system_info(payload):
    """The installed ZMod version from a /machine/system_info response, or None.

    ZMod's OS image reports a distribution named like ``"zmod 1.7.5"`` or,
    after a component update, ``"zmod 1.7.5 -> 1.7.5-2"`` (base -> current);
    the last version named is the one installed. A distribution not named
    zmod is not evidence of anything.
    """
    try:
        distribution = payload["result"]["system_info"]["distribution"]
    except (TypeError, KeyError):
        return None
    if not isinstance(distribution, dict):
        return None
    name = _version_str(distribution.get("name")) or ""
    if not name.casefold().startswith("zmod"):
        return None
    versions = re.findall(r"v?\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?", name)
    if versions:
        return versions[-1].lstrip("v")
    return _version_str(distribution.get("version"))


def fetch_os_version(transport):
    """Best-effort OS distribution version; None when unreadable (never raises)."""
    try:
        return os_version_from_system_info(transport("GET", SYSTEM_INFO_PATH))
    except Exception:
        return None


def mcu_version_from_objects(payload):
    """The MCU firmware version from a Klipper objects query, or None.

    Klipper's ``mcu`` object reports ``mcu_version`` in its status (the
    version of the firmware build running on the MCU, from the same
    Klipper checkout the host was built from). Any other shape is
    unknown, not an error.
    """
    try:
        mcu = payload["result"]["status"]["mcu"]
    except (TypeError, KeyError):
        return None
    if not isinstance(mcu, dict):
        return None
    return _version_str(mcu.get("mcu_version"))


def fetch_mcu_version(transport):
    """Best-effort MCU firmware version; None when unreadable (never raises)."""
    try:
        return mcu_version_from_objects(transport("GET", MCU_STATE_PATH))
    except Exception:
        return None


def mcu_currency(host_version, mcu_version) -> str:
    """"match", "mismatch" or "unknown" for Klipper 13 host <-> MCU currency.

    ZMod's Klipper 13 installs need ``UPDATE_MCU`` to keep the MCU in
    step with the host. Currency is only judged for a Klipper 13 host
    (0.13.x) and only when both versions parse to a release triple:
    differing triples are a mismatch, anything else unreadable is
    "unknown". Other host versions are not judged here.
    """
    host, mcu = version_parts(host_version), version_parts(mcu_version)
    if len(host) < 3 or host[:2] != (0, 13) or len(mcu) < 3:
        return "unknown"
    return "match" if host[:3] == mcu[:3] else "mismatch"


# --------------------------------------------------------------------- report

def _component_line(component, *, eligible):
    if component.name == "system" and not component.current and not component.remote:
        if component.update_available:
            return f"- **system**: {component.package_count} system packages available"
        return "- system: system packages up to date"
    current = component.current or "version not reported"
    if component.update_available and component.invalid:
        return (f"- **{component.name}**: {current} -> {component.remote} available, "
                f"but Moonraker will not apply it ({component.invalid})")
    if component.update_available:
        line = f"- **{component.name}**: {current} -> {component.remote} available"
        if eligible:
            line += f" — update with `/print-firmware update {component.name}`"
        return line
    if component.remote_unknown:
        return f"- {component.name}: {current} (latest version not reported)"
    return f"- {component.name}: {current} (up to date)"


def report_text(status, entry, *, pairing="unknown", os_version=None,
                mcu_state="unknown", mcu_versions=None) -> str:
    """The chat-ready stage 1 report for one parsed update status."""
    eligible = is_apply_eligible(entry)
    label = entry.label if entry is not None else "your printer"
    lines = [f"**Firmware on {label}** (read live from Moonraker's Update Manager):"]
    lines += [_component_line(c, eligible=eligible) for c in status.components]
    if entry is not None and entry.firmware_family == "zmod":
        # #562: the installed ZMod version is always stated for a ZMod
        # printer — as unknown when Moonraker does not report the `zmod`
        # component (or reports it without a current version), never
        # silently omitted and never guessed.
        zmod = status.component("zmod")
        if zmod is not None and zmod.current and zmod.source == "system_info":
            lines.append(f"ZMod version: {zmod.current} (from the printer's OS info; the "
                         "Update Manager could not read it)")
        elif zmod is not None and zmod.current:
            lines.append(f"ZMod version: {zmod.current}")
        else:
            lines.append("ZMod version: unknown (not reported by Moonraker)")
    unknown = status.remotes_unknown
    if unknown:
        names = ", ".join(c.name for c in unknown)
        lines.append(f"\nThe printer did not report the latest version for {names}, so "
                     "Print Handy cannot tell whether an update exists for "
                     f"{'it' if len(unknown) == 1 else 'them'}. Check in Fluidd or Mainsail.")
        limit = _github_limit_note(status)
        if limit:
            lines.append(limit)
    elif not status.updates_available:
        lines.append("\nEverything Print Handy can check here is up to date.")
    if entry is not None and entry.vendor in FORK_VENDORS:
        lines.append("\nThis printer runs its vendor's own Klipper-based firmware. These "
                     "versions are the vendor fork's, reported for information only — "
                     "Print Handy does not apply updates on vendor forks (see "
                     "`/print-firmware` guidance in docs/FIRMWARE-UPDATE.md).")
    if pairing == "mismatch":
        zmod = status.component("zmod")
        lines.append(f"\n**ZMod version mismatch:** the printer's OS reports "
                     f"{os_version or 'an unreadable version'} but the `zmod` component "
                     f"is {zmod.current if zmod else 'unreadable'}. ZMod pairs the OS and "
                     "the `zmod` component in their first two version numbers, and "
                     "component updates fail while they disagree (ZMod's docs say a mismatch "
                     "causes instability). Fluidd or Mainsail shows the OS version under System, "
                     "Distribution. `/print-firmware guide zmod` walks through the update, "
                     "including when ZMod requires the USB route.")
    hint = _edited_moonraker_conf_hint(status, entry)
    if hint:
        lines.append("\n**ZMod cannot update:** " + hint)
    if pairing == "match":
        lines.append("\nZMod base and component versions pair correctly "
                     f"({'.'.join(str(p) for p in version_parts(os_version)[:2])}.x).")
    if mcu_state == "mismatch" and mcu_versions is not None:
        host, mcu = mcu_versions
        lines.append(f"\n**MCU out of step:** Klipper on the host is {host} but the MCU "
                     f"firmware is {mcu}. On ZMod's Klipper 13, run "
                     "`UPDATE_MCU FORCE=13` at the printer (or your install's MCU "
                     "update step) to bring the MCU in step before printing. A bare "
                     "`UPDATE_MCU` switches the MCU to the other firmware, which on "
                     "a Klipper 13 install is the native one.")
    if entry is not None and entry.firmware_family == "zmod":
        lines.append("\nNot covered by this check: the ZMod base itself and the stock "
                     "Flashforge firmware underneath it update by hand — "
                     "`/print-firmware guide zmod` and `/print-firmware guide stock` "
                     "walk through both.")
    if eligible:
        lines.append("\n" + READ_ONLY_NOTE)
    else:
        lines.append("\nRead-only: this report sent nothing to the printer but read "
                     "requests, and Print Handy does not apply firmware updates on "
                     "this printer.")
    return "\n".join(lines)


def firmware_report(transport, entry, *, refresh=False) -> str:
    """Read the status (and, for ZMod, the pairing/MCU extras) and build the report.

    The status read is the only required one: its failure raises
    FirmwareError. The pairing and MCU reads are best-effort and
    unknown-tolerant — absent data is simply not reported. The ZMod
    version is the exception (#562): for a ZMod-family printer the
    report always states it, as unknown when Moonraker does not report
    the `zmod` component.
    """
    status = fetch_status(transport, refresh=refresh)
    pairing, os_version = "unknown", None
    mcu_state, mcu_versions = "unknown", None
    if entry is not None and entry.firmware_family == "zmod":
        os_version = fetch_os_version(transport)
        zmod = status.component("zmod")
        # A version read from the OS info cannot be paired against the OS info.
        if zmod is not None and zmod.source != "system_info":
            pairing = pairing_state(os_version, zmod.current)
    klipper = status.component("klipper")
    if klipper is not None and klipper.current:
        mcu_version = fetch_mcu_version(transport)
        mcu_state = mcu_currency(klipper.current, mcu_version)
        if mcu_state == "mismatch":
            mcu_versions = (klipper.current, mcu_version)
    return report_text(status, entry, pairing=pairing, os_version=os_version,
                       mcu_state=mcu_state, mcu_versions=mcu_versions)


def not_moonraker_text(connection) -> str:
    """The plain fallback when a printer's family has no Moonraker endpoint and
    no registry entry to build guidance from."""
    where = f" (it uses the {connection} connection)" if connection else ""
    return (f"This printer has no Moonraker Update Manager{where}, so Print Handy cannot "
            "read its firmware update state. Its firmware updates run at the printer "
            "itself — see docs/FIRMWARE-UPDATE.md for the per-family guide. "
            "Nothing was sent to the printer.")


# --------------------------------------------------------------------- stage 2: apply

def print_state(transport) -> str:
    """The printer's print_stats state, case-folded, through `transport`.

    Same contract as the macro guard: a response without a readable
    state is a FirmwareError, not a guess.
    """
    try:
        payload = transport("GET", PRINT_STATE_PATH)
    except FirmwareError:
        raise
    except Exception as exc:
        raise FirmwareError(f"the print-state request failed: {exc}") from exc
    try:
        state = payload["result"]["status"]["print_stats"]["state"]
    except (TypeError, KeyError):
        raise FirmwareError("Moonraker's print-state response had no print_stats state") from None
    if not isinstance(state, str) or not state.strip():
        raise FirmwareError("Moonraker's print-state response had no print_stats state")
    return state.strip().casefold()


def _require_idle(state, *, recheck=False):
    if state in IDLE_STATES:
        return
    when = "On the re-check just now, the" if recheck else "The"
    if state in BUSY_STATES:
        raise FirmwareRefusal(f"{when} printer is {state}, and firmware must not update "
                              "while a print is running or paused. Nothing was sent.")
    raise FirmwareRefusal(f"{when} printer reported an unrecognised state ({state!r}), so "
                          "Print Handy cannot confirm it is idle. Nothing was sent.")


@dataclass(frozen=True)
class PreparedUpdate:
    """One validated update: the live component and its exact version change."""

    component: Component

    @property
    def name(self):
        return self.component.name


def prepare_update(transport, entry, component_token) -> PreparedUpdate:
    """Validate one update request against the profile and the live printer; send nothing.

    The component name is only ever resolved against the live status just
    read — never trusted as typed — and the printer must be eligible,
    idle, and (on ZMod) free of a base/component pairing mismatch.
    """
    reason = apply_block_reason(entry)
    if reason is not None:
        raise FirmwareRefusal(reason + " Nothing was sent.")
    try:
        status = fetch_status(transport)
    except FirmwareError:
        raise FirmwareError(UPDATE_COULD_NOT_READ_TEXT) from None
    component = status.component(component_token)
    if component is None:
        raise FirmwareRefusal(f"The printer's update status does not list a component named "
                              f"{component_token!r}. Run `/print-firmware` to see the "
                              "components it actually reports — a typed name is never sent "
                              "on trust. Nothing was sent.")
    if component.remote_unknown:
        raise FirmwareRefusal(f"The printer does not report the latest version of "
                              f"**{component.name}**, so Print Handy cannot tell whether "
                              "an update exists and will not request one. Check in Fluidd "
                              "or Mainsail. Nothing was sent.")
    if component.invalid:
        hint = _edited_moonraker_conf_hint(status, entry) if \
            _component_key(component.name) == "zmod" else None
        raise FirmwareRefusal(f"Moonraker marks **{component.name}** invalid "
                              f"({component.invalid}) and refuses to update it, so Print "
                              "Handy will not request one. "
                              + (hint + " " if hint else "")
                              + "Nothing was sent.")
    if not component.update_available:
        raise FirmwareRefusal(f"**{component.name}** is already up to date "
                              f"({component.current or 'version not reported'}); there is "
                              "no update to apply. Nothing was sent.")
    if entry is not None and entry.firmware_family == "zmod":
        zmod = status.component("zmod")
        pairing = pairing_state(fetch_os_version(transport),
                                zmod.current if zmod else None)
        if pairing == "mismatch":
            raise FirmwareRefusal("The printer's ZMod OS and `zmod` component versions do "
                                  "not pair (their first two version numbers differ), and "
                                  "ZMod component updates fail in that state. Update the "
                                  "ZMod base from USB first — `/print-firmware guide zmod` "
                                  "walks through it. Nothing was sent.")
    try:
        state = print_state(transport)
    except FirmwareError:
        raise FirmwareError("Could not read the printer's state from Moonraker, so Print "
                            "Handy cannot confirm it is idle. Nothing was sent.") from None
    _require_idle(state)
    return PreparedUpdate(component=component)


def update_prompt_text(prepared: PreparedUpdate) -> str:
    """The confirmation question: the exact component and version change."""
    component = prepared.component
    if component.name == "system" and not component.remote:
        change = f"{component.package_count} system packages"
    else:
        change = f"**{component.name}**: {component.current or 'version not reported'} " \
                 f"-> {component.remote}"
    return "\n".join([
        "Update printer firmware?",
        "",
        f"This applies exactly one update: {change}.",
        "",
        "The printer must stay on and connected while it updates, and it may restart. "
        "Print Handy re-checks the printer is idle immediately before sending, then "
        "re-reads the firmware state afterwards and reports the verified result.",
        "Only this one component updates — ZMod's own guidance is one module at a time."])


def _update_verified(prepared: Component, component: Component) -> bool:
    """Whether a post-update re-read proves the confirmed update landed.

    Verification is against the exact change the operator confirmed. For
    a versioned component the installed version must now equal the
    prepared target: "no update shows as available" alone proves nothing,
    because a remote version Moonraker can no longer read ("?") also
    reads as no update while the installed version stays unchanged
    (P2-229). The ``system`` component reports packages, not versions, so
    its proof stays the offered package count clearing.
    """
    if prepared.remote is not None:
        return component.current == prepared.remote
    if prepared.package_count:
        return not component.update_available
    return not component.update_available and not component.remote_unknown


def _post_upgrade(transport, name) -> None:
    """POST the one upgrade, with an upgrade-sized timeout when the
    transport accepts one (see UPGRADE_TIMEOUT_S). The transport's
    signature is inspected rather than probed, so a transport that
    cannot take a timeout is called plainly — the POST is never sent
    twice."""
    try:
        params = inspect.signature(transport).parameters
        accepts_timeout = "timeout" in params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values())
    except (TypeError, ValueError):
        accepts_timeout = False
    if accepts_timeout:
        transport("POST", UPDATE_UPGRADE_PATH, {"name": name}, timeout=UPGRADE_TIMEOUT_S)
    else:
        transport("POST", UPDATE_UPGRADE_PATH, {"name": name})


def send_update(transport, prepared: PreparedUpdate, *, verify_attempts=3,
                verify_delay_s=5.0, sleep=time.sleep) -> str:
    """Re-check idle, POST the one confirmed upgrade, then verify by re-reading.

    The state can change while the operator decides, so prepare_update's
    idle check is repeated here; a busy or unreadable re-check sends
    nothing. After the POST, the status is re-read (a bounded number of
    attempts, waiting between them for a printer that may be restarting)
    and the outcome reported from that re-read only: verified only when
    the re-read shows the confirmed target version installed (see
    :func:`_update_verified`); otherwise still showing the update, an
    update state that can no longer be read, or the printer never came
    back — none of which is reported as success.

    A POST the printer answered with an HTTP error was refused and is
    reported as failed at once. Any other POST failure (a timeout, a
    dropped connection) is uncertain — Moonraker awaits the update
    before answering, so the request may well have landed — and enters
    the same bounded verification instead of failing on the spot; the
    POST is never resent.
    """
    try:
        state = print_state(transport)
    except FirmwareError:
        raise FirmwareRefusal("Could not re-check the printer's state just now, so the "
                              "update was not sent. Nothing was sent.") from None
    _require_idle(state, recheck=True)
    name = prepared.component.name
    post_error = None
    try:
        _post_upgrade(transport, name)
    except urllib.error.HTTPError as exc:
        raise FirmwareError(f"The printer refused the update request ({exc}). "
                            "Nothing was updated by it — check the printer's own "
                            "update screen for the reason.") from exc
    except Exception as exc:  # uncertain outcome: verify by re-reading below
        post_error = exc
    last_status = None
    for attempt in range(max(1, verify_attempts)):
        if attempt:
            sleep(verify_delay_s)
        try:
            last_status = fetch_status(transport)
        except FirmwareError:
            last_status = None
            continue
        component = last_status.component(name)
        if component is not None and _update_verified(prepared.component, component):
            return (f"Updated **{name}** to {component.current or 'its latest version'} — "
                    "verified by re-reading the printer's firmware state after the update.")
    uncertain = (f" The update request itself did not finish ({post_error}), so it is "
                 "not known whether the printer started updating."
                 ) if post_error is not None else ""
    if last_status is not None:
        component = last_status.component(name)
        if component is not None:
            if component.remote_unknown:
                return (f"The update was requested, but the printer no longer reports "
                        f"the latest version of **{name}**, so Print Handy cannot verify "
                        f"the update landed (it reports "
                        f"{component.current or 'no version'} installed). Check the "
                        "printer's own update screen before trying again.") + uncertain
            if component.update_available:
                return (f"The update was requested, but **{name}** still shows "
                        f"{component.remote} as available (current: "
                        f"{component.current or 'version not reported'}). Treat the update as "
                        "failed or not yet applied — check the printer's own update screen "
                        "before trying again.") + uncertain
            return (f"The update was requested, but **{name}** now reports "
                    f"{component.current or 'version not reported'} installed, not the "
                    f"confirmed target {prepared.component.remote}, and no update shows "
                    "as available. Treat the update as unverified — check the printer's "
                    "own update screen before trying again.") + uncertain
    return ("The printer did not answer after the update request, so Print Handy cannot "
            f"verify what happened to **{name}**. It may still be restarting — give it a "
            "few minutes, then run `/print-firmware` to see the state it reports.") + uncertain


NOT_APPLIED_TEXT = "Update not applied. Nothing sent."


async def confirmed_update(transport, entry, component_token, ask) -> str:
    """Stage 2 end to end: prepare, ask, then send. The one apply flow.

    Every surface that applies an update goes through here:
    ``/print-firmware update <component>`` and the Update button on an
    update alert (print_handy.firmware_alerts). ``ask(question,
    choices)`` is the surface's confirmation prompt and returns the
    chosen label (or None). Only an explicit UPDATE_LABEL sends; any
    other answer, a timeout included, sends nothing. prepare_update and
    send_update keep every gate (profile eligibility, the live
    component, both idle checks, the verified re-read). Returns the
    chat-ready outcome.
    """
    import asyncio
    try:
        prepared = await asyncio.to_thread(prepare_update, transport, entry, component_token)
    except (FirmwareRefusal, FirmwareError) as exc:
        return str(exc)
    answer = await ask(update_prompt_text(prepared), [UPDATE_LABEL, CANCEL_LABEL])
    if answer != UPDATE_LABEL:
        return NOT_APPLIED_TEXT
    try:
        return await asyncio.to_thread(send_update, transport, prepared)
    except (FirmwareRefusal, FirmwareError) as exc:
        return str(exc)


# --------------------------------------------------------------------- stage 3: guides

GUIDE_VERIFY_LINE = ("When the printer is back, run `/print-firmware` and Print Handy will "
                     "read and verify the versions it now reports.")

ZMOD_GUIDE = """**Updating ZMod (guided: you do these steps in Fluidd or Mainsail, or at the printer)**

Print Handy cannot do these steps for you: ZMod updates itself through its
web UI or a USB drive, and neither has an API Print Handy can drive. This
guide is the checklist, in the order ZMod's setup guide gives
("Updating the Mod"). Most updates need no USB drive.

1. Find out which kind of update this is. In Fluidd or Mainsail, open
   Settings, Software Update, and press Check for updates. If ZMod shows
   "Update Z-Mod via USB", it is a firmware-level update: use step 3 or
   4, even if you updated recently. Otherwise use step 2.
2. Routine update, no USB drive: update ZMod and its modules from that
   same Software Update page.
   - Many errors on the first check are normal: the plugins are not part
     of the firmware and download separately. Press Check for updates
     again, then restore and update each module one by one. The printer
     reboots partway through.
   - A component `/print-firmware` reports with a real available version
     can also be applied here with `/print-firmware update <component>`.
     A component whose latest version shows as not reported (Moonraker
     says `?`, usual for the `zmod`, `klipper` and `moonraker` git
     components) can only be updated from the Software Update page.
   Then go to step 5.
3. USB update with the `ZFLASH` macro: use a USB drive formatted FAT,
   FAT16 or FAT32 (MBR). Insert it and power the printer on. Without the
   native screen, the drive must be in **before** you power on. Then run
   `ZFLASH` from the printer's console in Fluidd or Mainsail. It checks
   for the latest ZMod release, downloads it to the USB drive, verifies
   the MD5 hash, and installs it after a reboot. It saves a trip to your
   computer, not a trip to the printer: the drive still has to be in it.
   You can leave the drive in for future updates.
4. Or a USB update by hand: download the latest ZMod release for your
   model, copy the file to the root of the USB drive without unpacking
   it, power the printer off, insert the drive and power on. Wait for
   the reboot and the success message without removing the drive; ZMod's
   docs say this can take up to about 40 minutes on the AD5X, with two
   reboots. Then remove the drive and power cycle the printer.
   After either USB update your `mod_data` settings are kept. Finish with
   step 2 to bring the modules up to date.
5. Check the version pairing: the OS version (Fluidd or Mainsail, System,
   Distribution) must match the first two numbers of the ZMod version
   (OS 1.9.x with ZMod 1.9.x). ZMod's docs say mismatched versions cause
   instability. `/print-firmware` checks this pairing for you and names a
   mismatch.
6. If you use Klipper 13 and `/print-firmware` reports the MCU out of step
   after a `klipper` update, run `UPDATE_MCU FORCE=13` at the printer.
   Not a bare `UPDATE_MCU` here: without `FORCE` it switches the MCU to
   the other firmware, which on a Klipper 13 install is the native one.
   This is a check after the components, not a step every update needs.

""" + GUIDE_VERIFY_LINE

STOCK_GUIDE = """**Updating the stock Flashforge firmware under ZMod (guided — you do these steps at the printer)**

The stock firmware is the layer underneath ZMod. It updates from the
printer's own screen or a USB drive — there is no API Print Handy can use,
so Print Handy cannot do these steps for you. On a ZMod printer, do the
pre-steps first or the update can strand the install:

1. Disable every active plugin except `recommend`, `timelapse` and
   `notify`: `DISABLE_PLUGIN name=<plugin>` for each of the others.
2. On Klipper 13, run `UPDATE_MCU` *before* updating the stock firmware,
   so the MCU and Klipper are not left on mismatched versions.
3. Update the stock firmware itself, one of two ways:
   - From the native screen: with the printer online, open the device
     info screen and choose Firmware Update (or accept the update prompt
     when one appears). Note a screen update may need Chinese cloud
     services enabled in ZMod's settings; if the screen finds no update,
     your serial number may not be in the rollout yet — use the USB path.
   - From USB: put Flashforge's firmware file for your model on a
     FAT/FAT32 USB drive, insert it while the printer is powered off,
     then power on and follow the screen.
   The IFS firmware is *not* part of this: it updates separately, with
   the IFS Update button on the same device info screen.
4. If you updated from the native screen, run `RESET_PASSWD` before
   turning the printer off.
5. If you updated from USB on an AD5X, re-enable ZMod from the USB drive
   with `AD5X-enable-zmod.tgz` (Creator 5 / Creator 5 Pro use their own
   `Creator5-enable-zmod.tgz` / `Creator5Pro-enable-zmod.tgz`).
6. Re-enable the plugins you disabled in step 1.

On a stock (non-ZMod) AD5X, steps 1, 2, 4 and 5 do not apply — just use
step 3 at the screen or from USB.

""" + GUIDE_VERIFY_LINE

_GUIDES = {"zmod": ZMOD_GUIDE, "stock": STOCK_GUIDE}


def guide_applies(which, entry) -> bool:
    """Whether a stage 3 guide covers this registry entry's printer."""
    if entry is None:
        return False
    if which == "zmod":
        return entry.firmware_family == "zmod"
    if which == "stock":
        return entry.firmware_family == "zmod" or entry.vendor == "flashforge_stock"
    return False


def guide_text(which, entry) -> str:
    """One stage 3 guide for this printer, or a plain not-applicable answer."""
    guide = _GUIDES.get(which)
    if guide is None:
        return ("Guides exist for the ZMod base (`/print-firmware guide zmod`) and for the "
                "stock Flashforge firmware under it (`/print-firmware guide stock`). "
                "Nothing was sent to the printer.")
    if guide_applies(which, entry):
        return guide
    label = entry.label if entry is not None else "this printer"
    covers = ("the ZMod base firmware on ZMod printers (AD5X, AD5M, AD5M Pro, Creator 5, "
              "Creator 5 Pro)" if which == "zmod" else
              "the stock Flashforge firmware on the AD5X — under ZMod, or stock")
    return (f"The {which} guide covers {covers}. {label} is not one of those, so the "
            "steps would not match its firmware. Run `/print-firmware` to see what Print "
            "Handy can report for it, and docs/FIRMWARE-UPDATE.md for its own update "
            "path. Nothing was sent to the printer.")


# --------------------------------------------------------------------- stage 4: guidance

@dataclass(frozen=True)
class FamilyGuidance:
    """One family's stage 4 verdict: what can be reported, the manual steps, why no apply."""

    title: str
    can_report: str
    manual_steps: str
    why_no_apply: str


_FORK_WHY = ("It runs its vendor's own Klipper-based firmware — a fork, not upstream "
             "Klipper — and its releases are sequenced by the vendor. Driving a fork's "
             "Update Manager at upstream releases is the documented breakage path, so "
             "Print Handy never applies updates on vendor forks.")

GUIDANCE = {
    "elegoo_neptune": FamilyGuidance(
        title="Elegoo Neptune 4 family (vendor Klipper fork)",
        can_report=("the versions its Moonraker endpoint reports — the vendor fork's own "
                    "versions, for information only"),
        manual_steps=("Elegoo's own package: copy the `ELEGOO_UPDATE_DIR` folder from "
                      "Elegoo's firmware download for your exact model and hardware "
                      "version to a USB drive, insert it, and start the update at the "
                      "touchscreen. The touchscreen's own firmware is a separate `.tft` "
                      "file loaded from a TF card."),
        why_no_apply=("Elegoo explicitly warns that updating a Neptune 4 to official "
                      "Klipper/Moonraker/Fluidd \"will make the machine not work "
                      "properly\". " + _FORK_WHY)),
    "creality_klipper": FamilyGuidance(
        title="Creality Klipper family (K1, Ender-3 V3 — vendor fork)",
        can_report=("the versions its Moonraker endpoint reports — Creality's fork's own "
                    "versions, for information only"),
        manual_steps=("Creality's releases: the touchscreen OTA / Creality Cloud offer "
                      "when one is presented, or Creality's USB firmware package for "
                      "your exact model, installed at the printer."),
        why_no_apply=_FORK_WHY),
    "qidi_klipper": FamilyGuidance(
        title="QIDI Klipper family (vendor fork)",
        can_report=("the versions its Moonraker endpoint reports — QIDI's fork's own "
                    "versions, for information only"),
        manual_steps=("QIDI's official update file for your exact model, copied to a USB "
                      "drive and installed at the printer."),
        why_no_apply=("Online-updating Klipper/Moonraker through the printer's web UI is "
                      "documented to break QIDI's OEM board/display connections — QIDI "
                      "shipped replacement eMMC boards for exactly this. " + _FORK_WHY)),
    "anycubic_kobra2": FamilyGuidance(
        title="Anycubic Kobra 2 family",
        can_report=("update state only where a Moonraker endpoint exists at all: stock "
                    "KobraOS exposes none, and Print Handy's profiles cover "
                    "Moonraker-exposed (community firmware) installs, whose reported "
                    "state is whatever that install's own configuration says"),
        manual_steps=("Stock installs: Anycubic's USB `update/update.swu` package, "
                      "installed at the printer. Community-firmware installs follow "
                      "that firmware's own update process."),
        why_no_apply=("Neither stock (no endpoint to ask) nor community installs "
                      "(unknown update remotes) give Print Handy a responsible apply "
                      "path.")),
    "snapmaker_u1": FamilyGuidance(
        title="Snapmaker U1 (vendor Klipper fork)",
        can_report=("the versions its Moonraker endpoint reports — Snapmaker's fork's "
                    "own versions, for information only"),
        manual_steps=("Snapmaker's own firmware package for the U1, installed from the "
                      "printer's screen or Snapmaker's instructions. Do not point the "
                      "printer at an upstream Klipper release."),
        why_no_apply=("The U1 runs Snapmaker's own Klipper-based firmware, a vendor "
                      "fork. " + _FORK_WHY)),
    "flashforge_stock": FamilyGuidance(
        title="Flashforge stock firmware (AD5X)",
        can_report=("nothing programmatic: the stock LAN API Print Handy uses carries "
                    "device info, not firmware update state"),
        manual_steps=("At the printer: Device Info → Firmware Update for the OTA check, "
                      "IFS Update separately for the IFS firmware, or a firmware file "
                      "on a USB drive inserted while powered off. "
                      "`/print-firmware guide stock` walks through it."),
        why_no_apply=("No firmware-update endpoint exists in the stock LAN API Print "
                      "Handy talks to.")),
    "bambulab": FamilyGuidance(
        title="Bambu Lab",
        can_report=("the installed firmware version, from the LAN status Print Handy "
                    "already reads; whether a newer version exists is a Bambu "
                    "cloud/wiki fact, not something the LAN protocol answers"),
        manual_steps=("Cloud OTA from the printer screen, Bambu Studio or Bambu Handy "
                      "with cloud mode on — or the manual offline upgrade: Bambu's "
                      "published firmware file on the printer's SD card, installed at "
                      "the printer. LAN-only mode, the mode Print Handy uses, "
                      "receives no OTA updates."),
        why_no_apply=("No local trigger for a stock firmware update exists in the LAN "
                      "protocol Print Handy uses.")),
    "prusa": FamilyGuidance(
        title="Prusa (PrusaLink)",
        can_report=("the printer and firmware identity PrusaLink already exposes; "
                    "PrusaLink carries no update-availability state"),
        manual_steps=("Prusa's Buddy firmware `.bbf` file on a FAT32 USB drive: insert "
                      "the drive and reboot, then confirm the flash at the printer's "
                      "own prompt. Every Prusa update route ends at that USB flash."),
        why_no_apply="No firmware-update endpoint exists in the PrusaLink API."),
    "elegoo_sdcp": FamilyGuidance(
        title="Elegoo Centauri Carbon (SDCP)",
        can_report=("no firmware-update state: the SDCP protocol Print Handy "
                    "implements has no firmware version or update fields"),
        manual_steps=("Touchscreen OTA when the printer is online, or the USB package "
                      "(`update.bin` / `update/update.swu`) — the USB path is also "
                      "the rollback path."),
        why_no_apply="No firmware command exists in the SDCP protocol."),
    "octoprint_marlin": FamilyGuidance(
        title="Marlin via OctoPrint",
        can_report=("the board's firmware name and version where the printer reports "
                    "it (the `M115` lineage); OctoPrint's own version describes "
                    "OctoPrint, not the board"),
        manual_steps=("Flash the board over serial with tooling configured for your "
                      "board's flash method — in the OctoPrint world that is the "
                      "Firmware Updater plugin. OctoPrint's built-in Software Update "
                      "updates OctoPrint and its plugins, not the Marlin board."),
        why_no_apply=("Print Handy does not install or control OctoPrint plugins, and "
                      "it does not flash boards over serial.")),
}


def guidance_for(entry) -> FamilyGuidance | None:
    """The stage 4 guidance for a registry entry, or None.

    None means the entry has the full stage 1–2 path (eligible ZMod /
    vanilla Klipper) or is a family this module does not know — the
    coverage test in tests/test_firmware_update.py iterates the real
    registry and fails loudly for any entry that is neither eligible nor
    covered here, so a future family cannot be silently dropped.
    """
    if entry is None or is_apply_eligible(entry):
        return None
    if entry.connection == "octoprint" or entry.vendor == "octoprint":
        return GUIDANCE["octoprint_marlin"]
    if entry.vendor == "elegoo" and entry.connection == "moonraker":
        return GUIDANCE["elegoo_neptune"]
    if entry.vendor == "creality":
        return GUIDANCE["creality_klipper"]
    if entry.vendor == "qidi":
        return GUIDANCE["qidi_klipper"]
    if entry.vendor == "anycubic":
        return GUIDANCE["anycubic_kobra2"]
    if entry.vendor == "snapmaker":
        return GUIDANCE["snapmaker_u1"]
    if entry.vendor == "flashforge_stock":
        return GUIDANCE["flashforge_stock"]
    if entry.vendor == "bambulab":
        return GUIDANCE["bambulab"]
    if entry.vendor == "prusa":
        return GUIDANCE["prusa"]
    if entry.vendor == "elegoo_sdcp":
        return GUIDANCE["elegoo_sdcp"]
    return None


def guidance_text(entry) -> str:
    """The chat-ready stage 4 answer for one printer's family."""
    guidance = guidance_for(entry)
    label = entry.label if entry is not None else "your printer"
    if guidance is None:
        return not_moonraker_text(entry.connection if entry is not None else None)
    return "\n".join([
        f"**Firmware updates for {label}** — {guidance.title}",
        "",
        f"What Print Handy can report: {guidance.can_report}.",
        f"How the update is done: {guidance.manual_steps}",
        f"Why Print Handy doesn't apply it: {guidance.why_no_apply}",
        "",
        "Nothing was sent to the printer. The full per-family matrix is in "
        "docs/FIRMWARE-UPDATE.md."])
