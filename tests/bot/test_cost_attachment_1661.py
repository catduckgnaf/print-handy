"""Chat attachment cost asks (#1661): a sliced file dropped in chat with
a cost ask is priced read-only, matching the Web UI calculator for the
same file; an unsliced 3MF gets the explicit not-sliced answer; the ask
creates no job, no print confirmation and no printer action; and a
bare file drop still belongs to the print intake flow alone.
"""
import asyncio
import copy
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from print_handy import settings
from print_handy.bot import matrix, runtime
from print_handy.bot.handlers import cost as cost_handler
from print_handy.bot.router import Router
from print_handy.core import cost as cost_engine
from print_handy.core import cost_parse
from print_handy.runtime import _config
from print_handy.webui import cost_page

from corpus import fake_discord
from . import test_matrix as tm
from . import test_telegram as tt

GCODE = ("; total filament used [g] = 12.5\n"
         "; estimated printing time (normal mode) = 1h 30m 0s\n"
         "G1 X10 Y10 E1.0\n")

SLICE_INFO = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              "<config><plate>"
              '<metadata key="prediction" value="5400"/>'
              '<metadata key="weight" value="12.5"/>'
              '<filament id="1" type="PLA" used_g="12.5"/>'
              "</plate></config>")

DATA = {"cost": {}, "paths": {}}


def run(coro):
    return asyncio.run(coro)


def _sliced_3mf_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("Metadata/slice_info.config", SLICE_INFO)
        archive.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _unsliced_3mf_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


def _webui_answer(path, data):
    """The Web UI calculator's own answer for the same dropped file."""
    usage = cost_parse.usage_from_file(path)
    job, profile, workspace = cost_page.job_from_usage(usage, data)
    result = cost_engine.compute(job, profile, workspace)
    return cost_engine.format_chat_summary(
        result, title=f"{path.name} (estimate before printing)")


# ------------------------------------------------------------ the answer itself

def test_sliced_gcode_attachment_matches_the_webui_calculator(tmp_path):
    path = tmp_path / "benchy.gcode"
    path.write_text(GCODE)
    text = cost_handler.attachment_cost_answer(path, DATA)
    assert text == _webui_answer(path, DATA)
    assert "benchy.gcode (estimate before printing)" in text
    assert "Priced from:" in text and "never blocks a print" in text


def test_sliced_3mf_attachment_matches_the_webui_calculator(tmp_path):
    path = tmp_path / "widget.3mf"
    path.write_bytes(_sliced_3mf_bytes())
    text = cost_handler.attachment_cost_answer(path, DATA)
    assert text == _webui_answer(path, DATA)
    assert "widget.3mf (estimate before printing)" in text


def test_unsliced_3mf_gets_the_explicit_not_sliced_answer(tmp_path):
    path = tmp_path / "widget.3mf"
    path.write_bytes(_unsliced_3mf_bytes())
    text = cost_handler.attachment_cost_answer(path, DATA)
    assert text.startswith("Cannot estimate widget.3mf yet:")
    assert "not sliced yet" in text
    assert "0.00" not in text


def test_gcode_naming_no_usage_cannot_estimate(tmp_path):
    path = tmp_path / "moves.gcode"
    path.write_text("G1 X10 Y10\nG1 X20 Y20\n")
    text = cost_handler.attachment_cost_answer(path, DATA)
    assert text.startswith("Cannot estimate moves.gcode yet:")
    assert "no filament usage, resin volume or print time" in text
    assert "0.00" not in text


def test_saved_upload_prefix_is_stripped_from_the_answer(tmp_path):
    path = tmp_path / "doc_0123456789ab_benchy.gcode"
    path.write_text(GCODE)
    text = cost_handler.attachment_cost_answer(path, DATA)
    assert "benchy.gcode (estimate before printing)" in text
    assert "doc_0123456789ab" not in text


# ------------------------------------------------------------ dispatch decisions

def test_gate_matches_the_command_and_file_phrases_only():
    gate = cost_handler.match
    assert gate("/print-cost") and gate("/print-cost 50 g")
    assert gate("how much will this file cost")
    assert gate("how much will this cost")
    assert not gate("did that print finish")
    assert not gate("how much filament will it use")
    assert not gate("")
    assert gate.command == "/print-cost" and gate.with_args is True


def test_amounts_in_the_ask_still_win_over_an_attachment(tmp_path):
    path = tmp_path / "benchy.gcode"
    path.write_text(GCODE)
    event = SimpleNamespace(media_urls=[str(path)])
    text = cost_handler._dispatch_answer("/print-cost 50 g, 3 h", event)
    assert text.startswith("Out of pocket") and "benchy.gcode" not in text


def test_a_month_ask_ignores_an_attachment(tmp_path, monkeypatch):
    path = tmp_path / "benchy.gcode"
    path.write_text(GCODE)
    data = {"cost": {}, "paths": {"records_vault": str(tmp_path / "vault")}}
    monkeypatch.setattr(settings, "_override", data)
    event = SimpleNamespace(media_urls=[str(path)])
    text = cost_handler._dispatch_answer("/print-cost this month", event)
    assert text.startswith("No priced prints in") and "benchy.gcode" not in text


# ------------------------------------------------------------ Discord end to end

@pytest.fixture
def discord_cfg(cfg, tmp_path, monkeypatch):
    cfg["paths"]["archive"] = str(tmp_path / "archive")
    cfg["paths"]["records_vault"] = str(tmp_path / "vault")
    monkeypatch.setattr(settings, "_override", cfg)
    return cfg


def _discord_core(cfg):
    discord = fake_discord.FakeDiscord(chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    core = runtime.BotCore(discord.client, cfg, router=Router())
    return discord, core


def _drop(tmp_path, name="benchy.gcode", text=GCODE):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_discord_command_with_a_file_prices_the_file(discord_cfg, tmp_path):
    discord, core = _discord_core(discord_cfg)
    message = discord.message("/print-cost",
                              attachments=[fake_discord.FakeAttachment(_drop(tmp_path))])
    run(core.handle_message(message))
    assert any("benchy.gcode (estimate before printing)" in s
               for s in discord.sent)


def test_discord_phrase_with_a_file_prices_the_file(discord_cfg, tmp_path):
    discord, core = _discord_core(discord_cfg)
    message = discord.message("how much will this file cost",
                              attachments=[fake_discord.FakeAttachment(_drop(tmp_path))])
    run(core.handle_message(message))
    assert any("benchy.gcode (estimate before printing)" in s
               for s in discord.sent)
    assert not any("Did you mean" in s for s in discord.sent)


def test_discord_cost_ask_creates_no_job_and_no_prompt(discord_cfg, tmp_path):
    """Criterion 3: the ask is read-only — no job record, no archived
    job folder, no confirmation question, and the intake's own G-code
    entry point is never called."""
    discord, core = _discord_core(discord_cfg)
    message = discord.message("how much will this file cost",
                              attachments=[fake_discord.FakeAttachment(_drop(tmp_path))])
    with mock.patch("print_handy.core.prepared_intake.print_gcode_file") as intake:
        run(core.handle_message(message))
    assert intake.call_count == 0
    assert len(discord.sent) == 1
    assert "benchy.gcode (estimate before printing)" in discord.sent[0]
    archive = Path(discord_cfg["paths"]["archive"])
    assert not archive.exists() or not list(archive.glob("*/JOB.json"))
    vault = Path(discord_cfg["paths"]["records_vault"])
    assert not vault.exists() or not list(vault.rglob("*.json"))


def test_discord_bare_file_drop_still_belongs_to_intake(discord_cfg, tmp_path):
    """The other direction of the separation: no cost ask, no cost
    answer — the drop reaches the intake's G-code entry as before."""
    discord, core = _discord_core(discord_cfg)
    message = discord.message("", attachments=[fake_discord.FakeAttachment(_drop(tmp_path))])
    seen = []

    async def fake_print(event, adapter, root, gcode_drop, **kwargs):
        seen.append(gcode_drop)
        return {"action": "skip", "reason": "test intake"}

    with mock.patch("print_handy.core.prepared_intake.print_gcode_file", fake_print), \
            mock.patch("print_handy.core.printer_state.load", return_value={}):
        run(core.handle_message(message))
    assert len(seen) == 1
    assert not any("Priced from:" in s for s in discord.sent)


def test_discord_unsliced_3mf_drop_answers_not_sliced(discord_cfg, tmp_path):
    discord, core = _discord_core(discord_cfg)
    path = tmp_path / "widget.3mf"
    path.write_bytes(_unsliced_3mf_bytes())
    message = discord.message("/print-cost",
                              attachments=[fake_discord.FakeAttachment(path)])
    run(core.handle_message(message))
    assert any(s.startswith("Cannot estimate widget.3mf yet:") and "not sliced yet" in s
               for s in discord.sent)


# ------------------------------------------------------------ Telegram end to end

@pytest.fixture
def telegram_cfg(tmp_path, monkeypatch):
    data = copy.deepcopy(_config.cfg())
    data["bot"].update(mode="standalone", platform="telegram")
    data["telegram"].update(chat_id=tt.CHAT, allowed_user_ids=tt.USER, approval_user_id="")
    data["paths"]["cache"] = tmp_path / "cache"
    data["paths"]["archive"] = str(tmp_path / "archive")
    data["paths"]["records_vault"] = str(tmp_path / "vault")
    data["llm"].update(chat_url="", model="")
    monkeypatch.setattr(settings, "_override", data)
    return data


def _telegram_update(caption):
    return {"update_id": 1, "message": {
        "message_id": 7, "from": {"id": int(tt.USER), "is_bot": False},
        "chat": {"id": int(tt.CHAT)}, "caption": caption,
        "document": {"file_id": "f1", "file_name": "benchy.gcode",
                     "file_size": len(GCODE)}}}


def test_telegram_caption_command_prices_the_file(telegram_cfg):
    stub = tt.BotApiStub(file_bytes=GCODE.encode())
    client, core = tt.make_core(telegram_cfg, stub)
    run(client.handle_update(_telegram_update("/print-cost")))
    texts = [p.get("text", "") for p in stub.payloads("sendMessage")]
    assert any("benchy.gcode (estimate before printing)" in t for t in texts)


def test_telegram_caption_phrase_prices_the_file(telegram_cfg):
    stub = tt.BotApiStub(file_bytes=GCODE.encode())
    client, core = tt.make_core(telegram_cfg, stub)
    run(client.handle_update(_telegram_update("how much will this file cost")))
    texts = [p.get("text", "") for p in stub.payloads("sendMessage")]
    assert any("benchy.gcode (estimate before printing)" in t for t in texts)


# ------------------------------------------------------------ Matrix end to end

@pytest.fixture
def matrix_cfg(tmp_path, monkeypatch):
    data = copy.deepcopy(_config.cfg())
    data["bot"].update(mode="standalone", platform="matrix")
    data["matrix"].update(homeserver_url=tm.HOMESERVER, room_id=tm.ROOM,
                          allowed_user_ids=tm.USER, approval_user_id="", bot_user_id=tm.BOT)
    data["paths"]["cache"] = tmp_path / "cache"
    data["paths"]["archive"] = str(tmp_path / "archive")
    data["paths"]["records_vault"] = str(tmp_path / "vault")
    data["llm"].update(chat_url="", model="")
    monkeypatch.setattr(settings, "_override", data)
    return data


def _matrix_file_event(body, filename="benchy.gcode"):
    content = {"msgtype": "m.file", "body": body, "url": tm.MXC,
               "info": {"size": len(GCODE)}}
    if filename is not None:
        content["filename"] = filename
    return {"type": "m.room.message", "sender": tm.USER, "event_id": "$e1661",
            "origin_server_ts": 1, "content": content}


def test_matrix_file_caption_is_the_message_text():
    """MSC2530: a distinct filename makes the body the caption — the
    Matrix shape of a Telegram caption, and the only way a Matrix file
    event can carry an ask."""
    parsed = matrix.parse_event(_matrix_file_event("/print-cost"), tm.ROOM, tm.BOT)
    assert parsed.content == "/print-cost"
    assert parsed.attachments[0].filename == "benchy.gcode"


def test_matrix_file_without_a_filename_keeps_the_body_as_the_name():
    parsed = matrix.parse_event(_matrix_file_event("benchy.gcode", filename=None),
                                tm.ROOM, tm.BOT)
    assert parsed.content == ""
    assert parsed.attachments[0].filename == "benchy.gcode"


def test_matrix_file_with_a_caption_prices_the_file(matrix_cfg, tmp_path):
    stub = tm.HomeserverStub(file_bytes=GCODE.encode())
    client, core = tm.make_core(matrix_cfg, stub, tmp_path)
    run(client.handle_event(_matrix_file_event("how much will this file cost"), tm.ROOM))
    assert any("benchy.gcode (estimate before printing)" in b
               for b in stub.sent_bodies())


def test_hours_only_manual_estimate_wins_over_attachment(tmp_path):
    path = tmp_path / "benchy.gcode"
    path.write_text(GCODE)
    event = SimpleNamespace(media_urls=[str(path)])
    text = cost_handler._dispatch_answer("/print-cost 3 h", event)
    assert "benchy.gcode" not in text
    assert text.startswith("Cannot estimate yet")
