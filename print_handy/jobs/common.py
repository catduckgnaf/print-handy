"""Settings-derived paths and limits plus the HTTP helpers the job code shares.

Moved verbatim from ``print_handy.core.model_job`` (#1323), so the gcode,
launch and intake modules can use them without importing model_job. The
old names are still importable from there for one release.
"""
import json, os, urllib.request

from .. import settings


PROFILE = settings.current()["paths"]["hermes_home"]
STATE = settings.state_dir()/"printer_hardware.json"
ARCHIVE = settings.current()["paths"]["archive"]
MOONRAKERCTL = settings.ROOT/"bin"/"moonrakerctl"


def _moonrakerctl_env(st):
    """Environment for bin/moonrakerctl aimed at the job's printer.

    moonrakerctl reads only MOONRAKER_URL / services.moonraker_url, the
    global (default printer's) URL; on a roster the start goes to the job's
    printer (st), so the upload must go there too.
    """
    env = dict(os.environ)
    url = st.get("moonraker_url") if isinstance(st, dict) else None
    if url:
        env["MOONRAKER_URL"] = str(url).rstrip("/")
    return env
TIMEZONE = settings.current()["discord"]["timezone"]
FILAMENT_SLOTS = settings.current()["printer"]["filament_slots"]
BED_MM = int(min(settings.current()["printer"]["bed_mm"]))
MAX_HEIGHT_MM = settings.current()["printer"]["max_print_height_mm"]

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def request_json(url, *, data=None, headers=None, timeout=25):
    h={"User-Agent":UA, "Accept":"application/json"}; h.update(headers or {})
    body=json.dumps(data).encode() if data is not None else None
    req=urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)


def _mr(st, path, data=None, timeout=30):
    return request_json(st["moonraker_url"]+path, data=data, timeout=timeout,
                        headers={"Content-Type":"application/json"} if data is not None else None)
