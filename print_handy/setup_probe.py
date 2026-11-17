"""Read-only, bounded Moonraker discovery for guided setup."""
from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import urlopen

logger = logging.getLogger(__name__)
CREATOR_KEYS = frozenset({"flashforge.creator5", "flashforge.creator5_pro",
                          "flashforge_opencreator.creator5", "flashforge_opencreator.creator5_pro",
                          "flashforge_reforge.creator5", "flashforge_reforge.creator5_pro"})
# The vendor package for each Creator firmware firmware_check.identify names.
CREATOR_VENDORS = {"zmod": "flashforge", "opencreator": "flashforge_opencreator",
                   "reforge": "flashforge_reforge"}


def read_result(base, path):
    """Unavailable, unauthorized, or malformed discovery never implies a match."""
    try:
        with urlopen(base.rstrip("/") + path, timeout=2) as response:
            payload = json.loads(response.read(1024 * 1024))
        result = payload.get("result") if isinstance(payload, dict) else None
        return result if isinstance(result, dict) else {}
    except (OSError, HTTPError, URLError, ValueError) as exc:
        # Do not log a URL or exception text: either may contain credentials.
        logger.debug("Setup discovery unavailable (%s)", type(exc).__name__)
        return {}


def matching_creator(key, objects):
    """Preserve base/Pro choice; object names identify firmware, not hardware."""
    if key not in CREATOR_KEYS or not isinstance(objects, list):
        return None
    # Exclusive tool authority objects only: zmod_color, creator5_toolchanger,
    # ff_toolchange or ff_print (firmware_check). AFC alone is shared by two
    # firmwares and decides nothing here.
    from .printers.flashforge_opencreator import firmware_check
    found = {family for family, present in (
        ("zmod", "zmod_color" in objects),
        ("opencreator", "creator5_toolchanger" in objects),
        ("reforge", bool(firmware_check.reforge_evidence(objects)))) if present}
    if len(found) != 1:  # conflicting or absent evidence
        return None
    target = CREATOR_VENDORS[found.pop()] + "." + key.split(".", 1)[1]
    return target if target != key else None


def webcam_snapshot(base, webcams):
    """Resolve a configured snapshot against the web host, not Moonraker's port."""
    if not isinstance(webcams, list):
        return ""
    parsed = urlsplit(base)
    # Moonraker's conventional API port is not the camera reverse proxy.
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    if port and port != 7125:
        host += ":" + str(port)
    web = urlunsplit((parsed.scheme, host, "/", "", ""))
    for camera in webcams:
        if not isinstance(camera, dict) or camera.get("enabled") is False:
            continue
        snapshot = camera.get("snapshot_url")
        if not isinstance(snapshot, str) or not snapshot.strip():
            continue
        resolved = urljoin(web, snapshot.strip())
        if urlsplit(resolved).scheme in {"http", "https"}:
            return resolved
    return ""
