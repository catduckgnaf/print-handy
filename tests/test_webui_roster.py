"""Web UI status with a multi-printer roster (#337, stage 2e).

Stage 2e decision: per-printer catalog editing is explicitly deferred, and
the status surface shows the roster read-only. These tests pin that shape:
the snapshot carries one row per printer with no secret material (not even
the *_env variable names), the HTML renders the roster table only when
there is more than one printer, and a single-printer snapshot keeps its
existing shape.
"""
import time
from pathlib import Path

from print_handy import settings
from print_handy.webui import server, status

ROSTER = """\
printers:
  - id: garage-a1
    name: Garage A1
    model: bambulab.a1
    chat_id: "100000000000000003"
    lan_host: 192.168.1.60
    lan_serial: "00M00A000000000"
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
services:
  spoolman_url: http://spoolman.home:7912
"""


def _heartbeat():
    return {"heartbeat_at": time.time(), "pid": 1, "mode": "standalone",
            "connected": True, "user": "printhandy#0001", "handlers": 1}


def _ok_probe(url):
    return True, "HTTP 200"


def _snapshot(tmp_path, text):
    local = Path(tmp_path) / "local.yaml"
    local.write_text(text)
    data = settings.load(local=local, environ={})
    return status.snapshot(data, environ={}, heartbeat=_heartbeat(),
                           probe=_ok_probe, checks=[])


def test_roster_rows_carry_no_secret_material(tmp_path):
    snap = _snapshot(tmp_path, ROSTER)
    rows = snap["printers"]
    assert [row["id"] for row in rows] == ["garage-a1", "workshop-ad5x"]
    first, second = rows
    assert first["default"] is True and second["default"] is False
    assert first["model"] == "bambulab.a1" and first["lan_host"] == "192.168.1.60"
    assert second["moonraker_url"] == "http://192.168.1.61:7125"
    assert second["chat_id"] == ""  # falls back to the main channel at routing time
    for row in rows:
        assert "lan_serial" not in row
        assert not any(key.endswith("_env") for key in row)


def test_roster_table_renders_only_for_a_roster(tmp_path):
    html = server.render_status_html(_snapshot(tmp_path, ROSTER))
    assert 'href="/printers"' in html
    assert "garage-a1" in html and "workshop-ad5x" in html

    single = _snapshot(tmp_path, "printer:\n  model: flashforge.ad5x\n")
    assert len(single["printers"]) == 1
    assert "Printers (read-only)" not in server.render_status_html(single)
