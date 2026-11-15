"""Settings-derived paths and limits plus the HTTP helpers the job code shares.

Moved verbatim from ``print_handy.core.model_job`` (#1323), so the gcode,
launch and intake modules can use them without importing model_job. The
old names are still importable from there for one release.
"""
import json, os

from .. import moonraker, settings


PROFILE = settings.current()["paths"]["state_home"]
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


def request_json(url, *, data=None, headers=None, timeout=25, moonraker_url=False):
    h={"User-Agent":UA, "Accept":"application/json"}; h.update(headers or {})
    # Through the one Moonraker transport (#1466): it adds X-Api-Key only
    # when this URL is a Moonraker URL (marked, or a configured Moonraker
    # base), so the outside model sites request_json also serves never
    # receive the printer's key.
    return moonraker.url_json(url, data=data, headers=h, timeout=timeout,
                              moonraker=moonraker_url)


def _mr(st, path, data=None, timeout=30):
    return request_json(st["moonraker_url"]+path, data=data, timeout=timeout, moonraker_url=True,
                        headers={"Content-Type":"application/json"} if data is not None else None)
