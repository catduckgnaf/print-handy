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
- **Channel switch (#1634)** changes one component's Update Manager
  ``channel:`` line in the printer's own Moonraker config, for the
  component whose prerelease an owner subscribed to but Moonraker never
  offers because its own config still says ``channel: stable``. The
  same eligibility and idle guards as stage 2, an owner confirmation
  showing the exact one-line change, a backup copy saved through
  Moonraker's file API before the write, a Moonraker restart, and
  verification from ``/machine/update/status`` — with the backup
  restored when anything fails. A channel switch installs nothing and
  downgrades nothing; it only changes which releases Moonraker offers
  for that component.
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

import logging
import inspect
import re
import time
import urllib.error
from dataclasses import dataclass, replace
from urllib.parse import quote
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

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
CONFIG_PATH = "/server/config"
CONFIG_DOWNLOAD_PATH = "/server/files/config/"
CONFIG_UPLOAD_PATH = "/server/files/upload"
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

# Roughly how long one component update takes, in seconds, as a
# (short, long) range (#1610). Display only: the operator deciding
# whether to start an update wants to know if it is a two-minute job or
# a fifteen-minute one — a ZMod update (download, install, service
# restarts, a possible reboot) runs much longer than a HelixScreen one.
# The outcome is always the verified re-read in send_update, never the
# clock; these ranges never gate anything.
UPDATE_ESTIMATES_S = {
    "zmod": (300, 900),
    "klipper": (300, 600),
    "system": (300, 900),
    "moonraker": (120, 300),
    "helixscreen": (120, 300),
}
DEFAULT_UPDATE_ESTIMATE_S = (120, 600)


def update_estimate(name) -> tuple:
    """The (short, long) rough duration range in seconds for a component."""
    return UPDATE_ESTIMATES_S.get(_component_key(name), DEFAULT_UPDATE_ESTIMATE_S)


def update_estimate_text(name) -> str:
    """The estimate as the operator reads it: "roughly 5-15 minutes"."""
    low, high = update_estimate(name)
    return f"roughly {low // 60}-{high // 60} minutes"

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
# Forge-X (#1506 tranche 3): its registry family stays "none", and its one
# applicable component, forge-x, is offered only when the owner turns on
# [firmware] forgex_apply (off by default). The gate is the profile plus that
# setting, never what the printer offers.
# OpenCreator (#1490): never applied, whatever its Update Manager offers.
OPENCREATOR_VENDOR = "flashforge_opencreator"
OPENCREATOR_COMPONENTS = frozenset({"opencreator", "opencreatorupdater"})   # the second is the SBC edition
# Reforge: never applied. Its updates are a package flashed from a USB
# stick through the stock updater, and its Moonraker config carries no
# update_manager section (Reforge docs/upgrading.md, moonraker.conf).
REFORGE_VENDOR = "flashforge_reforge"
FORGEX_VENDOR = "flashforge_forgex"
FORGEX_COMPONENT = "forgex"   # _component_key("forge-x")


def forgex_apply_enabled(cfg) -> bool:
    """[firmware] forgex_apply from a settings mapping; False when absent."""
    firmware = (cfg or {}).get("firmware") if isinstance(cfg, dict) else None
    return isinstance(firmware, dict) and firmware.get("forgex_apply") is True
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


def apply_block_reason(entry, cfg=None) -> str | None:
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
    if entry.vendor == OPENCREATOR_VENDOR:
        # Decided on #1490 (2026-11-15): report and alert only. The profile
        # is a Klipper-family Moonraker printer, so without this line it
        # would be apply-eligible.
        return ("Print Handy reports OpenCreator updates and never applies them. "
                "OpenCreator is alpha firmware on a Klipper fork, and one of its "
                "updates can flash the printer's boards. Update from Software "
                "Update in Fluidd or Mainsail while the printer is idle, and expect "
                "a Klipper restart afterwards. See docs/OPENCREATOR.md.")
    if entry.vendor == REFORGE_VENDOR:
        return ("Print Handy never applies Reforge updates. A Reforge update is a "
                "release package installed from a USB stick at the printer, like the "
                "first flash, and Reforge configures no Moonraker Update Manager "
                "entries. See docs/REFORGE.md.")
    if entry.vendor == FORGEX_VENDOR:
        if forgex_apply_enabled(cfg):
            return None
        return ("Print Handy reports Forge-X updates but applies them only when "
                "`[firmware] forgex_apply` is turned on (off by default): the forge-x "
                "component tracks a branch tip, and installing from chat is not yet "
                "verified on a real Forge-X printer. Update from Software Update in "
                "Fluidd or Mainsail instead, or see docs/FORGEX.md.")
    if entry.firmware_family not in ELIGIBLE_FAMILIES:
        return (f"This printer's firmware family ({entry.firmware_family or 'none'}) is not "
                f"one Print Handy can update: only ZMod and vanilla Klipper printers are "
                f"eligible.")
    return None


def is_apply_eligible(entry, cfg=None) -> bool:
    return apply_block_reason(entry, cfg) is None


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
    # How many commits a git component is behind its remote, when Moonraker
    # lists them; None when it does not say.
    behind: int | None = None
    # The component's Update Manager channel ("stable", "beta", "dev")
    # and configured type ("git_repo", "web", ...), when Moonraker
    # reports them (#1634). The channel is what decides whether a
    # prerelease ever shows as the component's available version.
    channel: str | None = None
    configured_type: str | None = None

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
        """The live component a typed name means, or None.

        ZMod's Moonraker names the component "Z-Mod"; callers ask for
        "zmod". An exact name (case and punctuation ignored, so "helix
        screen" is ``helixscreen``) wins; otherwise the name's alias
        group (COMPONENT_ALIASES: "helix", "klipper" for ZMod's
        ``klippy``, ...) picks the live component carrying one of the
        group's names. Resolution only ever returns a component the
        printer listed: an alias never invents one.
        """
        wanted = _component_key(name)
        if not wanted:
            return None
        for component in self.components:
            if _component_key(component.name) == wanted:
                return component
        group = component_alias_group(wanted)
        for alias in group:
            for component in self.components:
                if _component_key(component.name) == alias:
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


# The one alias table for component names (owner, 2026-11-11: "update
# helix == helixscreen and helix screen, same with guppy"). Each group is
# every name a person types for one component, canonical token first, as
# _component_key keys (so "helix screen", "Helix-Screen" and "z-mod" need
# no entry of their own). Moonraker names differ per host: ZMod's
# Moonraker lists Klipper as ``klippy`` and Moonraker itself as ``moon``,
# a vanilla Klipper host lists ``klipper`` and ``moonraker``. Resolution
# (UpdateStatus.component) is always against the live status just read;
# an alias is never sent on trust.
COMPONENT_ALIASES = (
    ("zmod",),
    ("helixscreen", "helix"),
    ("guppyscreen", "guppy"),
    ("klipper", "klippy"),
    ("moonraker", "moon"),
)
GUPPY_GROUP = COMPONENT_ALIASES[2]


def component_alias_group(name) -> tuple:
    """Every key one typed component name may match, itself first."""
    key = _component_key(name)
    for group in COMPONENT_ALIASES:
        if key in group:
            return (key, *(alias for alias in group if alias != key))
    return (key,) if key else ()


def canonical_component(name) -> str:
    """The canonical token for a typed component name ("helix" -> "helixscreen").

    A name outside COMPONENT_ALIASES comes back as its key. Used to word
    suggestions and commands; the live status still decides what exists.
    """
    key = _component_key(name)
    for group in COMPONENT_ALIASES:
        if key in group:
            return group[0]
    return key


GUPPY_IN_ZMOD_TEXT = ("GuppyScreen ships inside ZMod on this printer and updates with ZMod "
                      "itself, so it has no update of its own. `/print-firmware update zmod` "
                      "updates ZMod, GuppyScreen included. Nothing was sent.")


def is_guppy_on_zmod(entry, status, name) -> bool:
    """True when a ZMod printer is asked about GuppyScreen it does not list.

    ZMod bundles GuppyScreen (its own init script and binary, configured
    from ZMod's data folder) rather than registering it with Moonraker's
    Update Manager, so there is no component to update. A host that does
    list a ``guppyscreen`` component resolves normally and never gets here.
    """
    return (entry is not None and entry.firmware_family == "zmod"
            and _component_key(name) in GUPPY_GROUP
            and (status is None or status.component(name) is None))


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
                                    invalid=_invalid_reason(raw),
                                    behind=_commits_behind(raw),
                                    channel=_short_str(raw.get("channel")),
                                    configured_type=_short_str(raw.get("configured_type"))))
    remaining = payload["result"].get("github_requests_remaining")
    reset = payload["result"].get("github_limit_reset_time")
    return UpdateStatus(
        tuple(components),
        github_remaining=remaining if isinstance(remaining, int)
        and not isinstance(remaining, bool) else None,
        github_reset=float(reset) if isinstance(reset, (int, float))
        and not isinstance(reset, bool) else None)


def _short_str(value):
    """A short Moonraker-reported token (a channel, a component type), or None."""
    if isinstance(value, str):
        value = value.strip().casefold()
        return value or None
    return None


def _commits_behind(raw):
    """Commits behind from Moonraker's own list or count, or None when it gives neither."""
    count = raw.get("commits_behind_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    commits = raw.get("commits_behind")
    return len(commits) if isinstance(commits, list) else None


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
        except Exception:  # noqa: BLE001 - the refresh is optional; the plain read below decides
            logger.info("firmware update: the refresh request failed; using the plain status read",
                        exc_info=True)
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


def zmod_base_from_system_info(payload):
    """The USB-installed ZMod base from /machine/system_info, or None.

    ZMod's start.sh writes the distribution name as ``"zmod <usb> ->
    <current>"``; the first version is the base. A plain ``"zmod 1.7.5"``
    is its own base; otherwise the distribution version is used.
    """
    try:
        distribution = payload["result"]["system_info"]["distribution"]
    except (TypeError, KeyError):
        return None
    if not isinstance(distribution, dict):
        return None
    name = str(distribution.get("name") or "")
    if name.casefold().startswith("zmod"):
        found = re.findall(r"v?\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?", name)
        if found:
            return found[0].lstrip("v")
    return os_version_from_system_info(payload)


def fetch_zmod_base(transport):
    """Best-effort USB-installed ZMod base; None when unreadable (never raises)."""
    try:
        return zmod_base_from_system_info(transport("GET", SYSTEM_INFO_PATH))
    except Exception:
        return None


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


def _opencreator_line(status):
    """The OpenCreator entry stated plainly: it has no version, only a commit position.

    OpenCreator publishes no version number. Moonraker shows a git
    describe string for the checkout, so the honest facts are that
    string and how many commits behind it is. Unknown stays unknown.
    """
    component = next((c for c in status.components
                      if _component_key(c.name) in OPENCREATOR_COMPONENTS), None)
    if component is None:
        return ("OpenCreator: not listed by this printer's Update Manager, so Print Handy "
                "cannot tell whether it is current.")
    head = (f"OpenCreator ({component.name}) publishes no version number. Moonraker reports "
            f"its checkout as {component.current or 'not reported'}")
    if component.invalid:
        return f"{head}, and marks it not valid ({component.invalid}), so its state is unknown."
    if component.remote_unknown or component.behind is None:
        return f"{head}, and does not say how far behind it is."
    if component.behind == 0:
        if component.update_available:
            # Zero commits behind but a different remote string: the two
            # facts disagree, so neither "level" nor "behind" is claimed.
            return (f"{head}, with no commits behind, but its source reads {component.remote}. "
                    "Print Handy cannot tell whether it is current; check Software Update "
                    "in Fluidd or Mainsail.")
        return f"{head}, level with its source."
    count = component.behind
    return (f"{head}, {count} commit{'' if count == 1 else 's'} behind its source. Update from "
            "Software Update in Fluidd or Mainsail while the printer is idle.")


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
    if entry is not None and entry.vendor == OPENCREATOR_VENDOR:
        lines.append(_opencreator_line(status))
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
    text, _status = firmware_report_with_status(transport, entry, refresh=refresh)
    return text


def firmware_report_with_status(transport, entry, *, refresh=False):
    """firmware_report, plus the UpdateStatus it was built from.

    The Web UI's update offers and notifications (#1610) work from the
    same single read — a checked printer is read once, never twice.
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
    return (report_text(status, entry, pairing=pairing, os_version=os_version,
                        mcu_state=mcu_state, mcu_versions=mcu_versions), status)


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


def _forgex_refusal(component) -> str | None:
    """Why one Forge-X component may not be applied, or None (#1506 tranche 3).

    Only forge-x itself (the mod, a git component on Moonraker). Fluidd,
    Mainsail and GuppyScreen ship with the mod's line and are left to it.
    Over the air only within a version line: the first two numbers of the
    installed and the offered version must both be readable and equal; a new
    line is a USB re-flash (docs/FORGEX.md). An unreadable version refuses.
    """
    if _component_key(component.name) != FORGEX_COMPONENT:
        return (f"On Forge-X Print Handy installs only the **forge-x** component; "
                f"**{component.name}** updates with Forge-X itself. Nothing was sent.")
    have, offered = version_parts(component.current), version_parts(component.remote)
    if len(have) < 2 or len(offered) < 2:
        return ("Print Handy cannot read both the installed and the offered Forge-X "
                f"versions ({component.current or 'not reported'} -> "
                f"{component.remote or 'not reported'}), so it cannot tell whether the "
                "update stays within your version line. Nothing was sent.")
    if have[:2] != offered[:2]:
        return (f"**forge-x** {component.remote} is a new Forge-X version line (installed: "
                f"{component.current}). Forge-X moves between lines only by a USB re-flash "
                "at the printer, never over the air; see docs/FORGEX.md. Nothing was sent.")
    return None


def prepare_update(transport, entry, component_token, cfg=None) -> PreparedUpdate:
    """Validate one update request against the profile and the live printer; send nothing.

    The component name is only ever resolved against the live status just
    read — never trusted as typed — and the printer must be eligible,
    idle, and (on ZMod) free of a base/component pairing mismatch.
    """
    reason = apply_block_reason(entry, cfg)
    if reason is not None:
        raise FirmwareRefusal(reason + " Nothing was sent.")
    try:
        status = fetch_status(transport)
    except FirmwareError:
        raise FirmwareError(UPDATE_COULD_NOT_READ_TEXT) from None
    component = status.component(component_token)
    if component is None and is_guppy_on_zmod(entry, status, component_token):
        raise FirmwareRefusal(GUPPY_IN_ZMOD_TEXT)
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
    if entry is not None and entry.vendor == FORGEX_VENDOR and component.update_available:
        refusal = _forgex_refusal(component)
        if refusal is not None:
            raise FirmwareRefusal(refusal)
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
        if _component_key(component.name) == "zmod" and component.remote:
            # ZMod's own rule (zversion.sh): a target on another major.minor
            # than the USB-installed base needs a USB / ZFLASH install, not a
            # Software Update. Enforced here, not only in the alert text, so
            # no entry point can apply it (#1297 review).
            base = fetch_zmod_base(transport)
            target_parts, base_parts = version_parts(component.remote), version_parts(base)
            if len(target_parts) >= 2 and len(base_parts) >= 2 \
                    and target_parts[:2] != base_parts[:2]:
                raise FirmwareRefusal(f"**{component.name}** {component.remote} is a new ZMod "
                                      f"base (the printer was installed from USB as {base}), "
                                      "and ZMod installs a new base from USB or `ZFLASH`, not "
                                      "through Software Update. `/print-firmware guide zmod` "
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


async def confirmed_update(transport, entry, component_token, ask, cfg=None) -> str:
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
        prepared = await asyncio.to_thread(prepare_update, transport, entry, component_token, cfg)
    except (FirmwareRefusal, FirmwareError) as exc:
        return str(exc)
    answer = await ask(update_prompt_text(prepared), [UPDATE_LABEL, CANCEL_LABEL])
    if answer != UPDATE_LABEL:
        return NOT_APPLIED_TEXT
    try:
        return await asyncio.to_thread(send_update, transport, prepared)
    except (FirmwareRefusal, FirmwareError) as exc:
        return str(exc)


# ------------------------------------------------- channel switch (#1634)

# The channels Moonraker accepts per Update Manager component type, from
# Moonraker's own configuration docs: git_repo takes stable, beta or dev;
# web, zip, executable and python components take stable or beta. A type
# Moonraker does not report (or one this table does not know) gets the
# common subset, stable or beta. The `system` component has no channel.
CHANNELS_BY_TYPE = {
    "git_repo": ("stable", "beta", "dev"),
    "web": ("stable", "beta"),
    "zip": ("stable", "beta"),
    "executable": ("stable", "beta"),
    "python": ("stable", "beta"),
}
DEFAULT_CHANNELS = ("stable", "beta")
CHANNEL_VALUES = ("stable", "beta", "dev")

SWITCH_LABEL = "Switch channel"
NOT_SWITCHED_TEXT = "Channel not switched. Nothing sent."

# The config file ZMod owns: its moonraker.conf starts "Do not edit this
# file" and lives inside the Z-Mod repo, which Moonraker write-protects.
# A section Print Handy may edit lives in a file the owner owns — on a
# ZMod printer, mod_data/user.moonraker.conf.
READ_ONLY_CONFIG_BASENAME = "moonraker.conf"
USER_CONFIG_HINT = "mod_data/user.moonraker.conf"
BACKUP_SUFFIX = ".print-handy.bak"


def channels_for_type(configured_type) -> tuple:
    """The channel values Moonraker accepts for one component type."""
    return CHANNELS_BY_TYPE.get(str(configured_type or "").strip().casefold(),
                                DEFAULT_CHANNELS)


def _config_result(payload) -> dict:
    """The result mapping of a /server/config response (either wrap shape)."""
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            return result
        return payload
    return {}


def section_file(payload, section_names) -> str | None:
    """The config file Moonraker reports one of `section_names` in, or None.

    ``/server/config`` lists, per file, the sections parsed from it;
    filenames are relative to the main config file's folder. Section
    names match case- and punctuation-insensitively, the same way
    component names resolve.
    """
    files = _config_result(payload).get("files")
    if not isinstance(files, list):
        return None
    wanted = {_component_key(name) for name in section_names}
    for entry in files:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        sections = entry.get("sections")
        if not isinstance(filename, str) or not filename \
                or not isinstance(sections, list):
            continue
        if any(_component_key(section) in wanted for section in sections
               if isinstance(section, str)):
            return filename
    return None


def _is_read_only_config(filename) -> bool:
    name = str(filename or "").replace("\\", "/").rstrip("/")
    return name.rsplit("/", 1)[-1] == READ_ONLY_CONFIG_BASENAME


def _safe_config_filename(filename) -> str:
    """A config filename safe to download and upload, or a refusal.

    Filenames come from Moonraker's own /server/config listing, but they
    are still only ever used relative to the config root: no absolute
    paths, no parent hops, no backslashes.
    """
    name = str(filename or "").strip()
    parts = name.replace("\\", "/").split("/")
    if (not name or name.startswith("/") or "\\" in name
            or any(part in ("", ".", "..") for part in parts)):
        raise FirmwareRefusal(f"Moonraker reported an unusable config file name "
                              f"({filename!r}), so Print Handy will not write to it. "
                              "Nothing was sent.")
    return "/".join(parts)


_SECTION_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]")
_CHANNEL_LINE_RE = re.compile(r"^(\s*channel\s*[:=]\s*)(\S+)(.*)$")


def _section_present(text, section_name) -> bool:
    wanted = _component_key(section_name)
    for line in str(text or "").splitlines():
        header = _SECTION_HEADER_RE.match(line)
        if header and _component_key(header.group(1)) == wanted:
            return True
    return False


def switch_channel_in_text(text, section_name, target_channel):
    """Change only the ``channel:`` line of one section in a config text.

    Returns (new_text, old_line, new_line, current_channel), the lines
    stripped for display. Refuses (FirmwareRefusal) when the section is
    absent, when it has no ``channel:`` line — Print Handy changes an
    existing line and never adds one, because a section without one
    follows Moonraker's default for its type and adding a line is a
    bigger edit than the owner confirmed — or when the line already
    names the target channel. Everything else in the file, including
    the line's own spacing and any trailing comment, is preserved.
    """
    wanted = _component_key(section_name)
    lines = str(text or "").splitlines(keepends=True)
    in_section = False
    seen_section = False
    for index, line in enumerate(lines):
        header = _SECTION_HEADER_RE.match(line)
        if header:
            in_section = _component_key(header.group(1)) == wanted
            seen_section = seen_section or in_section
            continue
        if not in_section:
            continue
        body = line.rstrip("\r\n")
        match = _CHANNEL_LINE_RE.match(body)
        if not match:
            continue
        prefix, current, rest = match.groups()
        current = current.strip().casefold()
        if current == target_channel:
            raise FirmwareRefusal(f"The [{section_name}] section in the file already "
                                  f"sets `channel: {target_channel}`. Nothing was sent.")
        new_body = f"{prefix}{target_channel}{rest}"
        lines[index] = new_body + line[len(body):]
        return "".join(lines), body.strip(), new_body.strip(), current
    if not seen_section:
        raise FirmwareRefusal(f"The file does not contain a [{section_name}] section, "
                              "so there is no channel line to change. Nothing was sent.")
    raise FirmwareRefusal(f"The [{section_name}] section has no `channel:` line, so it "
                          "follows Moonraker's default channel for its type. Print Handy "
                          "changes an existing channel line and never adds one. Nothing "
                          "was sent.")


class MoonrakerConfigFiles:
    """Download and upload in one printer's Moonraker config root (#1634).

    The JSON transport cannot move plain files, so this speaks the file
    API directly through print_handy.moonraker (which adds the API key):
    GET /server/files/config/<name> to download, and a multipart POST to
    /server/files/upload with root=config to upload. A filename may
    carry its subdirectory (mod_data/user.moonraker.conf).
    """

    def __init__(self, base_url):
        self.base = str(base_url or "").rstrip("/")

    def download(self, filename) -> str:
        from . import moonraker
        name = _safe_config_filename(filename)
        url = f"{self.base}{CONFIG_DOWNLOAD_PATH}{quote(name, safe='/')}"
        return moonraker.url_text(url, timeout=15, moonraker=True)

    def upload(self, filename, text) -> None:
        from . import moonraker
        name = _safe_config_filename(filename)
        boundary = "----PrintHandyConfigUploadBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="root"\r\n\r\n'
            "config\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            f"{text}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        with moonraker.open_url(f"{self.base}{CONFIG_UPLOAD_PATH}", data=body,
                                method="POST", timeout=30, moonraker=True,
                                headers={"Content-Type":
                                         f"multipart/form-data; boundary={boundary}"}):
            pass


def config_files_for(printer_id, data=None) -> MoonrakerConfigFiles:
    """The config file access for one roster printer (or the default one)."""
    from . import settings
    if printer_id:
        view = settings.view_for(printer_id, data)
    else:
        view = data if data is not None else settings.current()
    base = str((view.get("services") or {}).get("moonraker_url") or "")
    return MoonrakerConfigFiles(base)


@dataclass(frozen=True)
class PreparedChannelSwitch:
    """One validated channel switch: the exact one-line change, backup first."""

    component: Component
    section: str
    filename: str
    current_channel: str
    target_channel: str
    original_text: str
    new_text: str
    old_line: str
    new_line: str

    @property
    def name(self):
        return self.component.name

    @property
    def backup_filename(self):
        return self.filename + BACKUP_SUFFIX


def prepare_channel_switch(transport, files, entry, component_token, channel_token,
                           cfg=None) -> PreparedChannelSwitch:
    """Validate one channel switch against the profile and the live printer; write nothing.

    Mirrors prepare_update's discipline: eligibility from the printer
    profile, the component resolved against the live status just read,
    the target checked against the channels Moonraker accepts for that
    component's type, the section located in the config file Moonraker
    reports for it (never the read-only moonraker.conf ZMod owns), the
    printer idle, and the exact one-line change read from the file
    itself. The backup is taken in send_channel_switch, not here, so a
    prepared switch that is never confirmed writes nothing at all.
    """
    reason = apply_block_reason(entry, cfg)
    if reason is not None:
        raise FirmwareRefusal(reason + " Nothing was sent.")
    target = str(channel_token or "").strip().casefold()
    if target not in CHANNEL_VALUES:
        raise FirmwareRefusal(f"{channel_token!r} is not an update channel Moonraker "
                              "accepts: use stable, beta or dev. Nothing was sent.")
    try:
        status = fetch_status(transport)
    except FirmwareError:
        raise FirmwareError(UPDATE_COULD_NOT_READ_TEXT) from None
    component = status.component(component_token)
    if component is None:
        raise FirmwareRefusal(f"The printer's update status does not list a component "
                              f"named {component_token!r}. Run `/print-firmware` to see "
                              "the components it actually reports — a typed name is "
                              "never sent on trust. Nothing was sent.")
    if _component_key(component.name) == "system":
        raise FirmwareRefusal("The **system** component tracks OS packages and has no "
                              "update channel to switch. Nothing was sent.")
    if not component.channel:
        raise FirmwareRefusal(f"Moonraker does not report an update channel for "
                              f"**{component.name}**, so there is no channel to switch. "
                              "Nothing was sent.")
    allowed = channels_for_type(component.configured_type)
    if target not in allowed:
        raise FirmwareRefusal(f"Moonraker accepts only {' or '.join(allowed)} for a "
                              f"{component.configured_type or 'component'} like "
                              f"**{component.name}**; {target} is not one of them. "
                              "Nothing was sent.")
    if target == component.channel:
        raise FirmwareRefusal(f"**{component.name}** is already on the {target} channel. "
                              "Nothing was sent.")
    try:
        config_payload = transport("GET", CONFIG_PATH)
    except Exception as exc:
        raise FirmwareError(f"Could not read the printer's Moonraker config listing, "
                            f"so Print Handy cannot say which file holds "
                            f"**{component.name}**. Nothing was sent. ({exc})") from exc
    section_names = (f"update_manager {component.name}",
                     f"update_manager client {component.name}")
    filename = section_file(config_payload, section_names)
    if filename is None:
        raise FirmwareRefusal(f"Moonraker's config listing does not place an "
                              f"[update_manager {component.name}] section in any file, "
                              "so Print Handy does not know where its channel is set. "
                              "Nothing was sent.")
    filename = _safe_config_filename(filename)
    if _is_read_only_config(filename):
        raise FirmwareRefusal(f"The [update_manager {component.name}] section is in "
                              f"`{filename}`, which ZMod owns and Moonraker "
                              "write-protects, so Print Handy will not edit it. Move "
                              f"the section into `{USER_CONFIG_HINT}` (the file ZMod "
                              "reserves for your own sections), restart Moonraker, and "
                              "run this again. Nothing was sent.")
    try:
        state = print_state(transport)
    except FirmwareError:
        raise FirmwareError("Could not read the printer's state from Moonraker, so Print "
                            "Handy cannot confirm it is idle. Nothing was sent.") from None
    _require_idle(state)
    try:
        original = files.download(filename)
    except FirmwareRefusal:
        raise
    except Exception as exc:
        raise FirmwareError(f"Could not download `{filename}` from the printer, so the "
                            f"channel was not switched. Nothing was sent. ({exc})") from exc
    section = next((name for name in section_names
                    if _section_present(original, name)), section_names[0])
    new_text, old_line, new_line, current = switch_channel_in_text(original, section,
                                                                   target)
    return PreparedChannelSwitch(component=component, section=section, filename=filename,
                                 current_channel=current, target_channel=target,
                                 original_text=original, new_text=new_text,
                                 old_line=old_line, new_line=new_line)


def channel_switch_prompt_text(prepared: PreparedChannelSwitch) -> str:
    """The confirmation question: the exact one-line change and its safeguards."""
    lines = [
        "Switch a Moonraker update channel?",
        "",
        f"This changes exactly one line in `{prepared.filename}` on the printer:",
        f"- [{prepared.section}] `{prepared.old_line}` becomes `{prepared.new_line}`",
        "",
        "Print Handy first saves a backup copy of the file "
        f"(`{prepared.backup_filename}`), writes the change through Moonraker's "
        "file API, restarts Moonraker, and then checks the update status to "
        "confirm the new channel. If the write or the restart fails, the backup "
        "is restored and Moonraker is restarted again.",
        "A channel switch installs nothing and downgrades nothing: it only "
        f"changes which releases Moonraker offers for **{prepared.component.name}**.",
    ]
    if prepared.target_channel == "stable":
        lines.append(f"Switching back to stable does not downgrade "
                     f"**{prepared.component.name}**: it keeps the version it has "
                     "until a stable release passes it.")
    return "\n".join(lines)


def _restore_config(files, prepared) -> bool:
    """Put the original file text back; True when the upload went through."""
    try:
        files.upload(prepared.filename, prepared.original_text)
    except Exception:
        logger.exception("firmware channel switch: restoring %s failed",
                         prepared.filename)
        return False
    return True


def _run_restart(restart):
    """Run the caller's Moonraker restart; the error, or None when it answered.

    The restart is the caller's to send: printer-control commands route
    through print_handy.printer_control (#1317), so each surface hands
    in its own printer's service restart and this module never names
    the endpoint. A dropped connection is an expected shape here
    (Moonraker goes down to restart), so any failure is treated as
    unproven and the verification re-read decides.
    """
    try:
        restart()
    except Exception as exc:  # noqa: BLE001 - verification decides, see docstring
        return exc
    return None


def send_channel_switch(transport, files, prepared: PreparedChannelSwitch, *,
                        restart, verify_attempts=8, verify_delay_s=5.0,
                        sleep=time.sleep) -> str:
    """Back up, write the one line, restart Moonraker, verify; restore on failure.

    ``restart`` is the surface's Moonraker restart for this printer
    (its print_handy.printer_control service method), called once after
    the write and once more after a restore. The idle check is repeated
    first — the state can change while the operator decides. The backup
    copy is uploaded before the config file itself is touched; if the
    backup cannot be saved, nothing is written. Any later failure (the
    write itself, or the new channel not showing in
    /machine/update/status after the restart) restores the original
    text and restarts Moonraker again, and the outcome says whether
    that restore went through.
    """
    try:
        state = print_state(transport)
    except FirmwareError:
        raise FirmwareRefusal("Could not re-check the printer's state just now, so the "
                              "channel was not switched. Nothing was sent.") from None
    _require_idle(state, recheck=True)
    name = prepared.component.name
    try:
        files.upload(prepared.backup_filename, prepared.original_text)
    except Exception as exc:
        raise FirmwareError(f"Could not save the backup copy "
                            f"(`{prepared.backup_filename}`), so the channel was not "
                            f"switched and `{prepared.filename}` was not touched. "
                            f"({exc})") from exc
    try:
        files.upload(prepared.filename, prepared.new_text)
    except Exception as exc:
        if _restore_config(files, prepared):
            return (f"The channel switch for **{name}** failed while writing "
                    f"`{prepared.filename}` ({exc}). The backup was restored, so the "
                    f"file is unchanged and **{name}** is still on the "
                    f"{prepared.current_channel} channel.")
        return (f"The channel switch for **{name}** failed while writing "
                f"`{prepared.filename}` ({exc}), and restoring the backup failed "
                f"too. Fix it by hand: copy `{prepared.backup_filename}` over "
                f"`{prepared.filename}` in the printer's config folder (Fluidd or "
                "Mainsail's config editor can do it) and restart Moonraker.")
    _run_restart(restart)
    last_status = None
    for attempt in range(max(1, verify_attempts)):
        if attempt:
            sleep(verify_delay_s)
        try:
            last_status = fetch_status(transport)
        except Exception:
            # Any failed verification leaves the config write unproven; keep
            # retrying and ultimately restore the saved original config.
            last_status = None
            continue
        component = last_status.component(name)
        if component is not None and component.channel == prepared.target_channel:
            text = (f"Switched **{name}** to the {prepared.target_channel} channel — "
                    "verified: Moonraker's update status now reports "
                    f"channel: {prepared.target_channel} for it. The backup copy is "
                    f"`{prepared.backup_filename}`.")
            if prepared.target_channel == "stable":
                text += (f" This does not downgrade **{name}**: it keeps "
                         f"{component.current or 'its installed version'} until a "
                         "stable release passes it.")
            return text
    if _restore_config(files, prepared):
        _run_restart(restart)
        seen = ""
        if last_status is not None:
            component = last_status.component(name)
            if component is not None and component.channel:
                seen = f" It still reports the {component.channel} channel."
        return (f"The channel switch for **{name}** did not take effect: after the "
                f"restart Moonraker does not report the {prepared.target_channel} "
                f"channel for it.{seen} The backup was restored and Moonraker "
                f"restarted again, so **{name}** is still on the "
                f"{prepared.current_channel} channel.")
    return (f"The channel switch for **{name}** did not take effect, and restoring "
            f"the backup failed too. Fix it by hand: copy "
            f"`{prepared.backup_filename}` over `{prepared.filename}` in the "
            "printer's config folder and restart Moonraker.")


async def confirmed_channel_switch(transport, files, entry, component_token,
                                   channel_token, ask, cfg=None, *, restart) -> str:
    """The channel switch end to end: prepare, ask, then send (#1634).

    The one flow every surface uses: chat's ``/print-firmware channel``
    and the Web UI Firmware page's Switch channel action.
    ``ask(question, choices)`` is the surface's confirmation prompt and
    returns the chosen label (or None). Only an explicit SWITCH_LABEL
    sends; any other answer, a timeout included, sends nothing.
    ``restart`` is the surface's Moonraker restart for this printer
    (its print_handy.printer_control service method, #1317).
    prepare_channel_switch and send_channel_switch keep every gate
    (profile eligibility, the live component, the channel values
    Moonraker accepts for the type, the read-only file refusal, both
    idle checks, the backup, the verified re-read).
    """
    import asyncio
    try:
        prepared = await asyncio.to_thread(prepare_channel_switch, transport, files,
                                           entry, component_token, channel_token, cfg)
    except (FirmwareRefusal, FirmwareError) as exc:
        return str(exc)
    answer = await ask(channel_switch_prompt_text(prepared),
                       [SWITCH_LABEL, CANCEL_LABEL])
    if answer != SWITCH_LABEL:
        return NOT_SWITCHED_TEXT
    try:
        return await asyncio.to_thread(send_channel_switch, transport, files, prepared,
                                       restart=restart)
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
    "flashforge_opencreator": FamilyGuidance(
        title="OpenCreator firmware (Flashforge Creator 5, Creator 5 Pro)",
        can_report=("the components its Moonraker update manager lists (klipper, "
                    "moonraker, the web interfaces, the screen, and the opencreator "
                    "entry itself). OpenCreator publishes no version number, so its "
                    "entry is a git describe string and a count of commits behind, "
                    "and a state Moonraker cannot read is stated as unknown, never "
                    "as up to date"),
        manual_steps=("At the printer, in Fluidd or Mainsail: Software Update, "
                      "while the printer is idle. Expect a Klipper restart "
                      "afterwards. See docs/OPENCREATOR.md."),
        why_no_apply=("OpenCreator is alpha firmware on a Klipper fork, and one of "
                      "its updates can flash the printer's boards. Print Handy "
                      "reports its update state and tells you when a release is "
                      "out, and leaves installing it to you.")),
    "flashforge_reforge": FamilyGuidance(
        title="Reforge firmware (Flashforge Creator 5, Creator 5 Pro)",
        can_report=("whatever its Moonraker Update Manager lists, which on a stock "
                    "Reforge install is nothing: Reforge ships no update_manager "
                    "entries, so its own version is not reported over Moonraker"),
        manual_steps=("Download the Reforge release package for your exact model "
                      "(Creator 5 or Creator 5 Pro), put it on a FAT32 USB stick, and "
                      "install it at the printer the same way as the first flash. "
                      "Your printer.cfg and moonraker-custom.conf are kept. See "
                      "docs/REFORGE.md."),
        why_no_apply=("A Reforge update is a USB package installed through the "
                      "printer's own updater, not a Moonraker update, so there is "
                      "nothing for Print Handy to apply.")),
    "flashforge_forgex": FamilyGuidance(
        title="Forge-X firmware (Flashforge AD5M, AD5M Pro)",
        can_report=("the versions its Moonraker update manager reports for its "
                    "components (forge-x, fluidd, mainsail, guppyscreen) — the "
                    "forge-x component's version is a git describe string for the "
                    "branch it tracks, not a release number, and a version Moonraker "
                    "reports as not reported is stated as not reported, never as "
                    "up to date"),
        manual_steps=("At the printer, in Fluidd or Mainsail: Configuration → "
                      "Software Update, for updates within the installed version "
                      "line (for example one 1.4.2 beta build to a later one). Moving to a new "
                      "version line is not an over-the-air update: download the "
                      "Forge-X release image for your exact model, put it on a USB "
                      "drive without unpacking it, and install it at the printer "
                      "from the drive. See docs/FORGEX.md."),
        why_no_apply=("Print Handy reports Forge-X update state and installs it only "
                      "when `[firmware] forgex_apply` is on (off by default): "
                      "the forge-x component tracks a branch tip rather than release "
                      "tags, and installing is not yet verified on a real Forge-X "
                      "printer. With it on, only forge-x installs, only within the "
                      "installed version line, behind the usual confirmation and idle "
                      "checks.")),
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
    if entry.vendor == OPENCREATOR_VENDOR:
        return GUIDANCE["flashforge_opencreator"]
    if entry.vendor == REFORGE_VENDOR:
        return GUIDANCE["flashforge_reforge"]
    if entry.vendor == "flashforge_forgex":
        return GUIDANCE["flashforge_forgex"]
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
