"""Bot check's roast row with a custom bank configured (#1002 Phase 4):
a valid bank counts toward the corpus, and a bank with problems is
named on the row instead of passing silently — in the roast row's
existing style. Offline; the banks are tiny temp files."""
import json

from print_handy.bot import __main__ as bot_main

VALID = {"id": "custom-workshop-motto", "family": "custom-house",
         "text": "The workshop gremlin union has certified {title} as officially finished."}


def rows(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    return {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}


def test_roast_row_counts_a_valid_custom_bank(cfg, monkeypatch, tmp_path):
    base_ok, base_detail = rows(cfg, monkeypatch)["roast"]
    assert base_ok is True and "custom roast bank" not in base_detail
    bank = tmp_path / "house.json"
    bank.write_text(json.dumps({"entries": [VALID]}), encoding="utf-8")
    cfg["vision"]["roast_custom_bank"] = str(bank)
    ok, detail = rows(cfg, monkeypatch)["roast"]
    assert ok is True
    base_size = int(base_detail.split("local corpus, ")[1].split(" lines")[0])
    size = int(detail.split("local corpus, ")[1].split(" lines")[0])
    assert size == base_size + 1


def test_roast_row_names_a_broken_custom_bank(cfg, monkeypatch, tmp_path):
    cfg["vision"]["roast_custom_bank"] = str(tmp_path / "gone.json")
    ok, detail = rows(cfg, monkeypatch)["roast"]
    assert ok is False
    assert "custom roast bank" in detail and "could not be read" in detail
    assert "gone.json" in detail
