"""Discord slash commands as real slash commands (#1475).

One visible response per command (defer + the answer as the interaction's
own followup), real subcommands with typed options that map back to the
typed text forms the handlers have always parsed, autocomplete fed by the
bot's own lists, no command asking for a file path, /print-status with its
Pause/Stop buttons, and the short /print-help with a menu.
"""
import asyncio
import json
from unittest import mock

import pytest

from corpus import fake_discord
from print_handy.bot import client, commands, runtime
from print_handy.bot.handlers import color_selection, print_status
from print_handy.runtime import print_stop


def make(cfg, auto=False):
    discord = fake_discord.FakeDiscord(auto=auto, chat_id=int(cfg["discord"]["chat_id"]),
                                       operator_id=int(cfg["discord"]["allowed_user_ids"]))
    return discord, runtime.BotCore(discord.client, cfg)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- typed mapping

@pytest.mark.parametrize("name,values,expected", [
    ("spool", {"subcommand": "guided"}, "/spool"),
    ("spool", {"subcommand": "list"}, "/spool list"),
    ("spool", {"subcommand": "change", "slot": 1, "spool": 12}, "/spool change 1 12"),
    ("spool", {"subcommand": "new", "brand": "Sunlu", "material": "PLA+",
               "color": "Matte White", "grams": 1000}, "/spool new Sunlu, PLA+, Matte White, 1000"),
    ("spool", {"subcommand": "weight", "spool": 19, "grams": 930}, "/spool weight 19 930"),
    ("spool", {"subcommand": "sync"}, "/spool sync"),
    ("print-calibration", {"subcommand": "mesh"}, "/print-calibration mesh"),
    ("print-calibration", {"subcommand": "pid", "target": "extruder", "temp": 210},
     "/print-calibration pid extruder 210"),
    ("print-calibration", {"subcommand": "pid", "target": "bed"}, "/print-calibration pid bed"),
    ("print-calibration", {"subcommand": "records"}, "/print-calibration records"),
    ("print-firmware", {"subcommand": "versions"}, "/print-firmware"),
    ("print-firmware", {"subcommand": "update", "component": "zmod"}, "/print-firmware update zmod"),
    ("print-firmware", {"subcommand": "guide", "flavor": "stock"}, "/print-firmware guide stock"),
    ("print-macros", {"subcommand": "list"}, "/print-macros"),
    ("print-macros", {"subcommand": "run", "name": "CLEAN_NOZZLE", "args": "TEMP=200"},
     "/print-macros run CLEAN_NOZZLE TEMP=200"),
    ("nozzle", {"subcommand": "change"}, "/nozzle change"),
    ("print-next", {"plan_id": "benchy", "part_id": "plate-2"}, "/print-next benchy plate-2"),
    ("print-colors", {"job": "57"}, "/print-colors 57"),
    ("print-status", {}, "/print-status"),
    ("print-status", {"printer": "workshop-ad5x"}, "/print-status workshop-ad5x"),
])
def test_picked_options_map_to_the_typed_form(name, values, expected):
    assert commands.as_text(name, values) == expected


def test_no_command_asks_for_a_file_path():
    def options_of(command):
        yield from command.options
        for sub in command.subcommands:
            yield from sub.options

    offenders = []
    for command in commands.COMMANDS:
        for option in options_of(command):
            text = f"{option.name} {option.description}".casefold()
            if "path" in text or "job.json" in text or ".json" in option.description:
                offenders.append((command.name, option.name))
        if "path" in command.usage.casefold() or "JOB.json" in command.usage:
            offenders.append((command.name, "usage"))
    assert offenders == []


# ------------------------------------------------------------- registration

def _tree():
    import discord
    from discord import app_commands
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.default()))
    core_ref = lambda: None  # noqa: E731
    for name, command in commands.registrations():
        tree.add_command(client._slash(core_ref, command, name))
    return tree


def test_subcommand_commands_register_as_groups_with_typed_options():
    tree = _tree()
    spool = tree.get_command("spool")
    assert [c.name for c in spool.commands] == ["guided", "list", "change", "new", "weight", "sync"]
    change = spool.get_command("change")
    params = {p.name: p for p in change.parameters}
    assert params["slot"].type.name == "integer" and params["slot"].required
    assert params["spool"].type.name == "integer" and params["spool"].required
    assert params["slot"].autocomplete and params["spool"].autocomplete
    for name, subs in (("print-calibration", ["mesh", "screws", "pid", "shaper", "belts", "zoffset", "records"]),
                       ("print-firmware", ["versions", "update", "guide"]),
                       ("print-macros", ["list", "run"]),
                       ("nozzle", ["change"])):
        assert [c.name for c in tree.get_command(name).commands] == subs


def test_autocomplete_and_choices_on_flat_options():
    tree = _tree()
    nxt = {p.name: p for p in tree.get_command("print-next").parameters}
    assert nxt["plan_id"].autocomplete and nxt["plan_id"].required
    assert nxt["part_id"].autocomplete
    colors = {p.name: p for p in tree.get_command("print-colors").parameters}
    assert colors["job"].autocomplete and colors["job"].required
    status = {p.name: p for p in tree.get_command("print-status").parameters}
    assert status["printer"].autocomplete
    macros_run = tree.get_command("print-macros").get_command("run")
    assert {p.name: p for p in macros_run.parameters}["name"].autocomplete


def test_autocomplete_providers_offer_the_bots_own_lists(cfg, tmp_path, monkeypatch):
    interaction = mock.Mock()
    interaction.namespace.plan_id = None
    with mock.patch("print_handy.runtime.print_control.printed_jobs",
                    return_value=[{"index": 57, "title": "Benchy", "job_json": "x"},
                                  {"index": 58, "title": "Gear", "job_json": "y"}]):
        choices = run(client._ac_jobs(interaction, ""))
    assert [(c.name, c.value) for c in choices] == [("#57 Benchy", "57"), ("#58 Gear", "58")]
    slots = run(client._ac_slots(interaction, ""))
    assert [c.value for c in slots] == ["1", "2", "3", "4"]
    with mock.patch("print_handy.runtime.spool_sync.shelf_spools",
                    return_value=[{"spool_id": 9, "name": "Sunlu PLA", "material": "PLA",
                                   "remaining_grams": 900.0}]):
        spools = run(client._ac_spools(interaction, ""))
    assert [(c.name, c.value) for c in spools] == [("#9 Sunlu PLA", "9")]


# ------------------------------------------------------------- one response

def test_read_only_slash_defers_ephemerally_and_answers_in_the_interaction(cfg):
    discord, core = make(cfg)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    with mock.patch("print_handy.runtime.spool_sync.list_text", return_value="Slot 1: #7 black PLA"):
        # /print-spools is read-only: its handler answers via /spool list's twin path
        run(core.run_slash(interaction, "print-spools", {}))
    assert interaction.acks == [("defer", {"ephemeral": True})]
    assert interaction.replies and all(r.get("followup") for r in interaction.replies)
    assert not any("`/print-spools`" in (r.get("content") or "") for r in interaction.replies)


def test_slash_help_is_short_with_a_menu_not_the_wall(cfg):
    discord, core = make(cfg)
    interaction = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    out = run(core.run_slash(interaction, "print-help", {}, view=object()))
    assert out == {"action": "skip", "reason": "help"}
    assert interaction.acks == [("defer", {"ephemeral": True})]
    body = interaction.replies[0]["content"]
    assert "/print-status" in body and "/print-stop" in body and len(body) < 1000
    assert body != core.help  # not the full command wall
    assert discord.sent == []


def test_help_menu_lists_the_rest():
    names = [c.name for c in commands.help_menu_commands()]
    assert "print-help" not in names and "print-status" not in names
    assert "print-macros" in names and len(names) <= 25


# ------------------------------------------------------------- /print-status

def _status(state="printing", **over):
    st = {"state": state, "filename": "benchy.gcode", "layer": 42, "total_layer": 100,
          "progress": 0.42, "print_duration": 3600, "hotend_c": 210.4, "bed_c": 60.2,
          "hotend_target_c": 210, "bed_target_c": 60, "camera_image_path": None}
    st.update(over)
    return st


def test_print_status_text_has_state_progress_time_layer_and_temps():
    text = print_status.status_text(_status(), "Garage AD5X")
    assert "printing" in text and "benchy.gcode" in text
    assert "42% done" in text and "layer 42/100" in text
    assert "left" in text  # estimated from progress + duration
    assert "Nozzle 210°C" in text and "Bed 60°C" in text


def test_print_status_handler_answers_and_keep_printing_changes_nothing(cfg):
    discord, core = make(cfg, auto=False)

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-status")))
        for _ in range(200):
            message = discord.last_question()
            if message is not None:
                buttons = discord.prompt_buttons(message)
                label, custom_id = next((t, c) for t, c in buttons if t == print_status.KEEP_LABEL)
                await discord.press(message, custom_id)
                break
            await asyncio.sleep(0.01)
        return await task

    from print_handy import printer_control
    with mock.patch.object(print_stop, "status", return_value=_status()), \
            mock.patch.object(printer_control.PrinterControl, "cancel") as cancel, \
            mock.patch.object(printer_control.PrinterControl, "pause") as pause:
        out = run(flow())
    assert out == {"action": "skip", "reason": "print status shown"}
    assert any("42% done" in text for text in discord.sent)
    cancel.assert_not_called()
    pause.assert_not_called()


def test_print_status_stop_button_asks_its_own_confirm_first(cfg):
    discord, core = make(cfg, auto=False)
    answers = iter(["Stop", "Stop the print"])
    seen_questions = []

    async def flow():
        task = asyncio.create_task(core.handle_message(discord.message("/print-status")))
        for _ in range(400):
            message = discord.last_question()
            if message is not None:
                from print_handy.bot.adapter import parse_custom_id
                from print_handy.core import prompts
                buttons = discord.prompt_buttons(message)
                fresh = [(t, c) for t, c in buttons
                         if prompts.is_open(parse_custom_id(c)[0])]
                if fresh:
                    want = next(answers)
                    label, custom_id = next((t, c) for t, c in fresh if t == want)
                    seen_questions.append(message.content)
                    await discord.press(message, custom_id)
                    if len(seen_questions) == 2:
                        break
            await asyncio.sleep(0.01)
        return await task

    from print_handy import printer_control
    with mock.patch.object(print_stop, "status", return_value=_status()), \
            mock.patch.object(printer_control.PrinterControl, "cancel",
                              return_value=(True, "Stopped.")) as cancel:
        out = run(flow())
    assert out == {"action": "skip", "reason": "print status shown"}
    assert len(seen_questions) == 2  # the action question, then the stop confirm
    cancel.assert_called_once()
    assert any("Stopped." in text for text in discord.sent)


def test_print_status_unreadable_printer_is_an_honest_answer(cfg):
    discord, core = make(cfg)
    with mock.patch.object(print_stop, "status", side_effect=RuntimeError("down")):
        out = run(core.handle_message(discord.message("/print-status")))
    assert out == {"action": "skip", "reason": "print status read failed"}
    assert any("Could not read the printer" in text for text in discord.sent)


# ------------------------------------------------------------- /print-colors

def test_print_colors_resolves_a_job_reference_not_a_path(tmp_path):
    job_dir = tmp_path / "benchy-job"
    job_dir.mkdir()
    job_file = job_dir / "JOB.json"
    job_file.write_text(json.dumps({"title": "Benchy"}))
    assert color_selection.resolve_job_ref("benchy-job", tmp_path) == job_file.resolve()
    assert color_selection.resolve_job_ref("Benchy", tmp_path) == job_file.resolve()
    assert color_selection.resolve_job_ref(str(job_file), tmp_path) == job_file.resolve()
    assert color_selection.resolve_job_ref("no-such-job", tmp_path) is None
    assert color_selection.resolve_job_ref("", tmp_path) is None


# ------------------------------------------------------------- review fixes

def test_overlapping_slash_commands_keep_their_own_delivery(cfg):
    """A slash command awaiting a button never lends its interaction to another
    command or to a plain message handled meanwhile, and the shared adapter is
    intact afterwards (review of #1484: the adapter was swapped across an await)."""
    discord, core = make(cfg)
    real = core.adapter
    first = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    second = fake_discord.FakeInteraction(discord, discord.operator, channel=discord.channel)
    seen = {}

    async def scenario():
        release = asyncio.Event()
        started = asyncio.Event()

        async def process(event, allow_llm=False):
            which = "first" if not started.is_set() else "second"
            if which == "first":
                started.set()
                await release.wait()  # a button prompt left open
            seen[which] = core.adapter
            await core.adapter.send(chat_id=core.chat_id, content=f"answer for {which}")
            return {"action": "skip"}

        with mock.patch.object(core, "process", side_effect=process):
            one = asyncio.create_task(core.run_slash(first, "print-spools", {}))
            await started.wait()
            # A plain message's task, while the first command is still open.
            seen["message"] = await asyncio.create_task(_adapter_of(core))
            await core.run_slash(second, "print-spools", {})
            seen["between"] = core.adapter
            release.set()
            await one

    async def _adapter_of(c):
        return c.adapter

    run(scenario())
    assert seen["message"] is real and seen["between"] is real
    assert seen["first"] is not real and seen["second"] is not real and seen["first"] is not seen["second"]
    assert [r["content"] for r in first.replies] == ["answer for first"]
    assert [r["content"] for r in second.replies] == ["answer for second"]
    assert core.adapter is real and core.gateway.adapter is real


def test_a_zero_option_value_is_kept_in_the_typed_form():
    assert commands.as_text("spool", {"subcommand": "weight", "spool": 19, "grams": 0}) == "/spool weight 19 0"


def test_print_colors_does_not_resolve_a_job_file_outside_the_archive(tmp_path):
    archive = tmp_path / "archive"
    inside = archive / "job-a" / "JOB.json"
    inside.parent.mkdir(parents=True)
    inside.write_text(json.dumps({"title": "A"}))
    outside = tmp_path / "elsewhere" / "JOB.json"
    outside.parent.mkdir()
    outside.write_text(json.dumps({"title": "B"}))
    assert color_selection.resolve_job_ref(str(inside), archive) == inside.resolve()
    assert color_selection.resolve_job_ref(str(outside), archive) is None


def test_print_status_keys_on_the_printer_the_command_named():
    gateway = mock.Mock()
    with mock.patch("print_handy.bot.handlers.common._channel_printer_id", return_value="channel-printer"):
        assert print_status._channel_pid(mock.Mock(), gateway, "named-printer") == "named-printer"
        assert print_status._channel_pid(mock.Mock(), gateway, None) == "channel-printer"
