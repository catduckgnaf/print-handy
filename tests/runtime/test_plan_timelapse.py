"""Issue #41: one combined timelapse per multi-plate plan, posted after the last plate or on cancel."""
import pytest

from print_handy.runtime import _config
from print_handy.runtime import moonraker_discord_announcements as announcer
from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import plan_timelapse


def plan(*states):
    return {"plan_id": "trio", "parts": [
        {"id": f"p{i}", "label": f"p{i}", "quantity": 1, "completed_quantity": int(s), "status": "completed" if s else "planned"}
        for i, s in enumerate(states, 1)]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    sent, pickers, ffmpeg = [], [], []
    monkeypatch.setattr(plan_timelapse, "_root", lambda: tmp_path / "plans")
    monkeypatch.setattr(mel, "log", lambda _: None)
    monkeypatch.setattr(mel, "post", lambda text: sent.append(text) or True)
    monkeypatch.setattr(mel, "post_plate_picker", lambda plan_id: pickers.append(plan_id) or True)

    class Inline:
        def __init__(self, target=None, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(mel.threading, "Thread", Inline)
    monkeypatch.setattr(announcer, "archive_local_timelapse", lambda plan_id, path: "/archive/combined.mp4")

    def fake_run(cmd, **kw):
        listing = cmd[cmd.index("-i") + 1]
        ffmpeg.append(open(listing).read().splitlines())
        open(cmd[-1], "wb").write(b"combined")
        return None
    monkeypatch.setattr(plan_timelapse.subprocess, "run", fake_run)

    def render(name):
        """Make the announcer 'render' a plate's clip: archive + materialize hand back this file."""
        clip = tmp_path / name
        clip.write_bytes(name.encode())
        monkeypatch.setattr(announcer, "archive_completed_timelapse",
                            lambda key: announcer.ArchiveOutcome(announcer.ARCHIVED, "/archive/" + name))
        monkeypatch.setattr(announcer, "materialize_archived_timelapse", lambda path: clip)

    def announce(*lines):
        monkeypatch.setattr(announcer, "main", lambda: print("\n".join(lines)))
        monkeypatch.setattr(announcer, "_timelapse_job_key", lambda token: "plate.gcode")
        mel.run_announcer("completion")
    return type("W", (), {"sent": sent, "pickers": pickers, "ffmpeg": ffmpeg, "render": staticmethod(render),
                          "announce": staticmethod(announce), "tmp": tmp_path})


def test_stage_single_plate_plan_is_unchanged():
    assert plan_timelapse.plan_stage(plan(1)) is None
    assert plan_timelapse.plan_stage({"parts": []}) is None


def test_stage_more_then_final_and_order_follows_plan_position():
    assert plan_timelapse.plan_stage(plan(1, 0, 0), "p1") == ("more", 0)
    assert plan_timelapse.plan_stage(plan(1, 1, 1), "p3") == ("final", 2000)
    # Plates need not finish in plan order: ordering comes from the plan position.
    assert plan_timelapse.plan_stage(plan(0, 1, 0), "p2")[1] > plan_timelapse.plan_stage(plan(1, 0, 0), "p1")[1]


def test_non_final_plate_posts_no_video_but_keeps_text_and_picker(world):
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done", "PLATE_PICKER:trio")
    assert world.sent == ["Plate 1 done"]
    assert world.pickers == ["trio"]
    assert len(plan_timelapse.clips("trio")) == 1  # held for the combined video
    assert world.ffmpeg == []


def test_final_plate_posts_one_combined_video_in_plate_order(world):
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done", "PLATE_PICKER:trio")
    world.render("two.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:1000", "Plate 2 done", "PLATE_PICKER:trio")
    world.render("three.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:final:2000", "Job complete")
    videos = [m for m in world.sent if "MEDIA:" in m]
    assert len(videos) == 1 and videos[0].startswith("Job complete\nMEDIA:") and "Partial" not in videos[0]
    assert [line.rsplit("-", 1)[1].rstrip("'") for line in world.ffmpeg[0]] == ["one.mp4", "two.mp4", "three.mp4"]
    assert world.sent[:2] == ["Plate 1 done", "Plate 2 done"]
    assert world.pickers == ["trio", "trio"]  # none after the final plate
    assert plan_timelapse.clips("trio") == []  # stored clips cleaned up


def test_clips_join_in_plan_order_when_plates_finish_out_of_order(world):
    world.render("b.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:1000", "Plate 2 done")
    world.render("a.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:final:0", "Job complete")
    assert [line.rsplit("-", 1)[1].rstrip("'") for line in world.ffmpeg[0]] == ["a.mp4", "b.mp4"]


def test_cancel_posts_what_exists_labeled_partial_without_picker(world):
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done", "PLATE_PICKER:trio")
    world.render("two.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:1000", "Plate 2 done", "PLATE_PICKER:trio")
    world.sent.clear()
    world.announce("TIMELAPSE_PLAN:trio:partial:0", "Print stopped: Trio")
    assert len(world.sent) == 1 and "MEDIA:" in world.sent[0] and "Partial timelapse: 2 plate(s)" in world.sent[0]
    assert len(world.ffmpeg[0]) == 2
    assert world.pickers == ["trio", "trio"]
    assert plan_timelapse.clips("trio") == []


def test_cancel_with_no_clips_posts_text_only(world):
    world.announce("TIMELAPSE_PLAN:trio:partial:0", "Print stopped: Trio")
    assert world.sent == ["Print stopped: Trio"]


def test_final_plate_whose_clip_is_missing_posts_earlier_plates_as_partial(world, monkeypatch):
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done")
    world.sent.clear()
    world.render("two.mp4")
    monkeypatch.setattr(announcer, "archive_completed_timelapse",
                        lambda key: announcer.ArchiveOutcome(announcer.ARCHIVE_NOT_RENDERED))
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:final:1000", "Job complete")
    assert len(world.sent) == 1 and "Partial timelapse: 1 plate(s)" in world.sent[0]


def test_single_plate_job_keeps_todays_one_post_with_video(world):
    world.render("solo.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "Print complete: Solo")
    assert world.sent == [f"Print complete: Solo\nMEDIA:{world.tmp / 'solo.mp4'}"]
    assert plan_timelapse.clips("trio") == [] and world.ffmpeg == []


def test_concat_falls_back_to_reencode_when_stream_copy_fails(world, monkeypatch):
    import subprocess
    (world.tmp / "x.mp4").write_bytes(b"x")
    plan_timelapse.add_clip("trio", 0, world.tmp / "x.mp4")
    plan_timelapse.add_clip("trio", 1000, world.tmp / "x.mp4")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if "copy" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        open(cmd[-1], "wb").write(b"v")
    monkeypatch.setattr(plan_timelapse.subprocess, "run", run)
    assert plan_timelapse.combine("trio").name == "combined.mp4"
    assert "libx264" in calls[1] and "copy" not in calls[1]


def test_partial_notice_only_when_clips_exist(world, capsys):
    assert announcer.partial_timelapse_notice("trio", "trio.gcode") is None
    (world.tmp / "c.mp4").write_bytes(b"c")
    plan_timelapse.add_clip("trio", 0, world.tmp / "c.mp4")
    assert "Print stopped" in announcer.partial_timelapse_notice("trio", "trio.gcode")
    assert "TIMELAPSE_PLAN:trio:partial:0" in capsys.readouterr().out


def test_stale_plan_clips_are_pruned(world):
    (world.tmp / "c.mp4").write_bytes(b"c")
    plan_timelapse.add_clip("trio", 0, world.tmp / "c.mp4")
    assert plan_timelapse.prune() == []
    days = _config.cfg()["timelapse"]["plan_clip_retention_days"]
    assert plan_timelapse.prune(now=__import__("time").time() + (days + 1) * 86400) == ["trio"]
    assert plan_timelapse.clips("trio") == []


def test_final_video_is_partial_when_an_earlier_plate_clip_is_missing(world, monkeypatch):
    monkeypatch.setattr(plan_timelapse, "_load_plan", lambda plan_id: plan(1, 1, 1))
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done")
    monkeypatch.setattr(announcer, "archive_completed_timelapse",
                        lambda key: announcer.ArchiveOutcome(announcer.ARCHIVE_NOT_RENDERED))  # plate 2 never rendered
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:1000", "Plate 2 done")
    world.render("three.mp4")
    world.sent.clear()
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:final:2000", "Job complete")
    assert len(world.sent) == 1 and "Partial timelapse: 2 plate(s)" in world.sent[0]


def test_complete_plan_with_every_clip_is_not_partial(world, monkeypatch):
    monkeypatch.setattr(plan_timelapse, "_load_plan", lambda plan_id: plan(1, 1))
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done")
    world.render("two.mp4")
    world.sent.clear()
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:final:1000", "Job complete")
    assert "Partial" not in world.sent[0] and "MEDIA:" in world.sent[0]


def test_non_final_plates_prune_stale_plan_stores(world, monkeypatch):
    pruned = []
    monkeypatch.setattr(plan_timelapse, "prune", lambda now=None: pruned.append(True))
    world.render("one.mp4")
    world.announce("TIMELAPSE_POSTPROCESS:plate.gcode", "TIMELAPSE_PLAN:trio:more:0", "Plate 1 done")
    assert pruned == [True]
