"""Bot tests: a fake discord.py client, no token, no network.

The bot switches a process global on (the plugin's Stop sender). Each test gets
it back off.
"""
import copy
import time

import pytest

from print_handy.core import prepared_intake
from print_handy.runtime import _config


@pytest.fixture(autouse=True)
def _restore_bot_globals(monkeypatch):
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", None)
    yield


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["bot"]["mode"] = "standalone"
    data["discord"].update(chat_id="100000000000000001", allowed_user_ids="200000000000000002",
                           approval_user_id="")
    data["paths"]["cache"] = tmp_path / "cache"
    data["llm"].update(chat_url="", model="")
    return data


# ---------------------------------------------------------------- print posts to web chat

class PrintEventRig:
    """The listener's real post path on one install, and web chat reading its feed.

    The platform REST senders are recorders (no network), the archive holds
    one job on a two-printer roster, and a WebChatService on the same
    settings reads the feed the listener writes (bot/web_feed.py). Shared
    by the Web UI parity guard and the per-family print post tests.
    """

    STARTER = "111222333444555666"
    WORKSHOP = "workshop-ad5x"

    def __init__(self, cfg, tmp_path, monkeypatch):
        import json

        from print_handy.bot import matrix_rest, rest, telegram_rest, web
        from print_handy.runtime import discord_print_summary as summary
        from print_handy.runtime import print_alert_buttons as pab
        from print_handy.runtime import starter_pings

        self.cfg = cfg
        folder = tmp_path / "archive" / "widget--abc"
        folder.mkdir(parents=True)
        gcode = folder / "plate.gcode"
        gcode.write_text("; estimated printing time (normal mode) = 1h 0m\n"
                         "; total filament used [g] = 12\n")
        job = {"title": "Widget", "gcode": str(gcode), "printer_id": self.WORKSHOP,
               "started_by": {"platform": "discord", "user_id": self.STARTER}}
        (folder / "JOB.json").write_text(json.dumps(job))
        self.archive = tmp_path / "archive"
        monkeypatch.setattr(summary, "ARCHIVE", self.archive)
        monkeypatch.setattr(summary, "CONTEXT_PATH", tmp_path / "no-context.json")
        monkeypatch.setattr(starter_pings, "STATE", tmp_path / "pings.json")
        monkeypatch.setattr(pab, "STATE", tmp_path / "print_alert_buttons.json")
        base = cfg["printers"][0]
        cfg["printers"] = [{**base, "id": "garage-ad5x", "name": "Garage AD5X", "chat_id": ""},
                           {**base, "id": self.WORKSHOP, "name": "Workshop AD5X", "chat_id": ""}]
        cfg["paths"]["state_home"] = tmp_path / "home"
        cfg.setdefault("firmware", {})["update_alerts"] = False
        cfg["telegram"]["chat_id"] = "-1001"
        cfg["matrix"].update(room_id="!room:example.org", homeserver_url="https://matrix.example.org")
        monkeypatch.setattr(_config, "_override", cfg)
        self.sent = []  # (platform, text, mention)

        def recorder(platform):
            def send(*args, **kwargs):
                self.sent.append((platform, args[-1], kwargs.get("mention_user_id")))
                return True, "1"
            return send

        monkeypatch.setattr(rest, "send", recorder("discord"))
        monkeypatch.setattr(telegram_rest, "send", recorder("telegram"))
        monkeypatch.setattr(matrix_rest, "send", recorder("matrix"))
        self.button_posts = []

        def button_post(name, content_index):
            def post(*args, **kwargs):
                self.button_posts.append((name, args[content_index]))
                return True
            return post

        monkeypatch.setattr(pab, "post_with_buttons", button_post("post_with_buttons", 2))
        for name in ("post_visual_alert", "post_visual_text_alert", "post_visual_dark_notice",
                     "post_telegram_with_buttons", "post_plate_confirm"):
            monkeypatch.setattr(pab, name, button_post(name, 2))
        monkeypatch.setattr(pab, "post_matrix_with_reactions",
                            button_post("post_matrix_with_reactions", 3))
        monkeypatch.setattr(web, "FEED_POLL_S", 0.02)
        monkeypatch.setattr(web, "ALERT_RECHECK_S", 0.05)
        self.install("discord")
        self.service = web.WebChatService(cfg)
        self.view = self.service.view(web.scope_for("print-event-session"))

    def install(self, platform):
        self.cfg["bot"]["platform"] = platform

    def render(self, kind, filename="plate.gcode"):
        from print_handy.runtime import discord_print_summary as summary
        return summary.format_event(kind, filename, archive=self.archive, emit=False)

    def web_texts(self):
        return [entry.get("content") or entry.get("question") or ""
                for entry in self.view.entries() if entry["kind"] in ("message", "prompt")]

    def wait_web(self, predicate, timeout=10.0):
        """The first web chat post matching ``predicate``; fails after ``timeout``."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for text in self.web_texts():
                if predicate(text):
                    return text
            time.sleep(0.02)
        raise AssertionError(f"web chat never showed the post: {self.web_texts()}")

    def wait_prompt(self, needle, timeout=10.0):
        """The open web chat prompt whose question has ``needle``."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            prompt = self.view.open_prompt()
            if prompt and needle in prompt["question"]:
                return prompt
            time.sleep(0.02)
        raise AssertionError(f"web chat never asked about {needle!r}: {self.web_texts()}")


@pytest.fixture
def print_event_rig(cfg, tmp_path, monkeypatch):
    return PrintEventRig(cfg, tmp_path, monkeypatch)
