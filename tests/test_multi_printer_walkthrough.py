import toml_twin
"""The design §3 worked example, load-checked offline (#337 acceptance).

The config below is the MULTI-PRINTER-DESIGN.md section 3 example verbatim:
a Bambu A1 and a Flashforge AD5X in one instance, two Discord channels, one
shared Spoolman. Both entries must resolve, the channels must bind to the
right printers, Spoolman must be the same service for both, and each
printer's state must live in its own folder.
"""
from pathlib import Path

from print_handy import settings
from print_handy.bot import routing

DESIGN_EXAMPLE = '''\
[discord]
chat_id = "100000000000000001"        # the AD5X channel (fallback)
allowed_user_ids = "200000000000000002"

[services]
spoolman_url = "http://spoolman.home:7912"

[[printers]]
id = "garage-a1"
model = "bambulab.a1"
chat_id = "100000000000000003"
lan_host = "192.168.1.60"
lan_serial = "00M00A000000000"

[[printers]]
id = "workshop-ad5x"
model = "flashforge.ad5x"
moonraker_url = "http://192.168.1.61:7125"
'''


def test_design_section_3_example_loads_and_routes(tmp_path):
    local = tmp_path / "local.toml"
    local.write_text(DESIGN_EXAMPLE, encoding="utf-8")
    before = local.read_bytes()

    data = settings.load(local=toml_twin.yaml_fixture(local), environ={})

    entries = settings.printers(data)
    assert [entry["id"] for entry in entries] == ["garage-a1", "workshop-ad5x"]
    assert settings.default_printer_id(data) == "garage-a1"

    # Channels: the A1's own channel, and the AD5X on the global fallback.
    assert routing.channel_map(data, "discord") == {
        "100000000000000003": "garage-a1",
        "100000000000000001": "workshop-ad5x",
    }
    assert routing.printer_for_chat(data, "discord", "100000000000000003") == "garage-a1"
    assert routing.printer_for_chat(data, "discord", "100000000000000001") == "workshop-ad5x"
    assert routing.printer_for_chat(data, "discord", "100000000000000099") is None

    # One shared Spoolman, seen identically from both printers' views.
    for printer_id in ("garage-a1", "workshop-ad5x"):
        view = settings.view_for(printer_id, data)
        assert view["services"]["spoolman_url"] == "http://spoolman.home:7912"
    assert settings.view_for("garage-a1", data)["bambu"]["lan_host"] == "192.168.1.60"
    assert settings.view_for("workshop-ad5x", data)["services"]["moonraker_url"] == \
        "http://192.168.1.61:7125"

    # State is per printer, in distinct folders.
    a1_dir = settings.printer_state_dir("garage-a1", data)
    ad5x_dir = settings.printer_state_dir("workshop-ad5x", data)
    assert a1_dir != ad5x_dir
    assert Path(a1_dir).name == "garage-a1" and Path(ad5x_dir).name == "workshop-ad5x"

    # Loading never rewrites the operator's file.
    assert local.read_bytes() == before


# --- #391 stage 3: the cross-family cross-product ---------------------------
#
# The Stage 2 matrix was sampled: the walkthrough above pairs two
# families, the per-printer suites pair state and leases, and routing
# pairs channels. These pin the combinations that can cross-contaminate
# when three different listener families share one instance, one
# Spoolman and one fallback channel — and the claimless entry, which
# must stay unreachable by chat without breaking the others' routing.

THREE_FAMILIES = '''\
[discord]
chat_id = "100000000000000001"

[services]
spoolman_url = "http://spoolman.home:7912"

[[printers]]
id = "garage-a1"
model = "bambulab.a1"
chat_id = "100000000000000003"
lan_host = "192.168.1.60"
lan_serial = "00M00A000000000"

[[printers]]
id = "workshop-ad5x"
model = "flashforge.ad5x"
moonraker_url = "http://192.168.1.61:7125"

[[printers]]
id = "bench-octo"
model = "octoprint.marlin"
chat_id = "100000000000000004"
octoprint_url = "http://192.168.1.62:5000"
'''


def _load(tmp_path, text):
    local = tmp_path / "local.toml"
    local.write_text(text, encoding="utf-8")
    return settings.load(local=toml_twin.yaml_fixture(local), environ={})


def test_three_families_route_and_view_without_cross_leak(tmp_path):
    data = _load(tmp_path, THREE_FAMILIES)
    assert [e["id"] for e in settings.printers(data)] == [
        "garage-a1", "workshop-ad5x", "bench-octo"]

    assert routing.channel_map(data, "discord") == {
        "100000000000000003": "garage-a1",
        "100000000000000001": "workshop-ad5x",  # the fallback claimant
        "100000000000000004": "bench-octo",
    }
    assert routing.chat_for_printer(data, "discord", "bench-octo") == "100000000000000004"
    assert routing.printer_for_chat(data, "discord", "100000000000000099") is None

    a1 = settings.view_for("garage-a1", data)
    ad5x = settings.view_for("workshop-ad5x", data)
    octo = settings.view_for("bench-octo", data)
    # Each view carries its own family's connection — and only its own.
    assert a1["bambu"]["lan_host"] == "192.168.1.60"
    assert a1["services"]["moonraker_url"] == ""
    assert a1["services"].get("octoprint_url", "") == ""
    assert ad5x["services"]["moonraker_url"] == "http://192.168.1.61:7125"
    assert ad5x["bambu"]["lan_host"] == ""
    assert ad5x["services"].get("octoprint_url", "") == ""
    assert octo["services"]["octoprint_url"] == "http://192.168.1.62:5000"
    assert octo["services"]["moonraker_url"] == ""
    assert octo["bambu"]["lan_host"] == ""

    # The shared services stay shared; the state folders stay separate.
    for view in (a1, ad5x, octo):
        assert view["services"]["spoolman_url"] == "http://spoolman.home:7912"
    dirs = {settings.printer_state_dir(pid, data)
            for pid in ("garage-a1", "workshop-ad5x", "bench-octo")}
    assert len(dirs) == 3


def test_a_printer_with_no_channel_claim_is_unreachable_but_harmless(tmp_path):
    """No chat_id of its own and no platform home configured: the entry
    claims nothing (design: it is simply unreachable by chat), the other
    printer's routing is unaffected, and lookups for it by chat are None
    rather than an error."""
    data = _load(tmp_path, '''\
[[printers]]
id = "claimed"
model = "bambulab.a1"
chat_id = "555"
lan_host = "192.168.1.60"
lan_serial = "00M00A000000000"

[[printers]]
id = "quiet"
model = "flashforge.ad5x"
moonraker_url = "http://192.168.1.61:7125"
''')
    assert routing.channel_map(data, "discord") == {"555": "claimed"}
    assert routing.chat_for_printer(data, "discord", "quiet") is None
    assert routing.printer_for_chat(data, "discord", "555") == "claimed"
    assert routing.printer_for_chat(data, "discord", "556") is None
    # Both entries still resolve as printers; only chat reachability differs.
    assert settings.by_id("quiet", data)["model"] == "flashforge.ad5x"
