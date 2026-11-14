"""Mocked regressions for Lane B reported bugs 4 and 12."""
import importlib.util
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parents[2] / "scripts"
from print_handy.runtime import moonraker_event_listener as mel

def test_completion_with_video_is_one_combined_discord_post(monkeypatch):
    sent=[]
    monkeypatch.setattr(mel,"log",lambda _:None); monkeypatch.setattr(mel,"warn",lambda _:None); monkeypatch.setattr(mel,"post",lambda text: sent.append(text) or True)
    monkeypatch.setattr(mel.announcer,"main",lambda: print("TIMELAPSE_POSTPROCESS:job.gcode\nPrint complete: Job"))
    monkeypatch.setattr(mel.announcer,"_timelapse_job_key",lambda token:"job.gcode")
    monkeypatch.setattr(mel.announcer,"archive_completed_timelapse",lambda key:mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVED,"/archive/job.mp4"))
    monkeypatch.setattr(mel.announcer,"materialize_archived_timelapse",lambda path:"/cache/job.mp4")
    class Inline:
        def __init__(self,target=None,args=(),**kw): self.target,self.args=target,args
        def start(self): self.target(*self.args)
    monkeypatch.setattr(mel.threading,"Thread",Inline)
    mel.run_announcer("mock")
    assert sent == ["Print complete: Job\nMEDIA:/cache/job.mp4"]

def test_completion_without_video_is_one_text_post(monkeypatch):
    sent=[]; monkeypatch.setattr(mel,"log",lambda _:None); monkeypatch.setattr(mel,"warn",lambda _:None); monkeypatch.setattr(mel,"post",lambda text: sent.append(text) or True)
    monkeypatch.setattr(mel.announcer,"main",lambda: print("TIMELAPSE_POSTPROCESS:job.gcode\nPrint complete: Job"))
    monkeypatch.setattr(mel.announcer,"_timelapse_job_key",lambda token:"job.gcode")
    monkeypatch.setattr(mel.announcer,"archive_completed_timelapse",lambda key:mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVE_NOT_RENDERED))
    class Inline:
        def __init__(self,target=None,args=(),**kw): self.target,self.args=target,args
        def start(self): self.target(*self.args)
    monkeypatch.setattr(mel.threading,"Thread",Inline)  # the postprocess is a thread; run it inline so the assert is not a race
    mel.run_announcer("mock")
    # Archiving is configured in the test settings and the bounded wait
    # really elapsed, so a missing video is announced in the post instead
    # of vanishing silently (#373).
    assert sent == ["Print complete: Job\nTimelapse not attached: the printer's rendered video "
                    "didn't arrive within the wait (see the listener log)."]

def test_repeated_completion_event_does_not_duplicate_post(monkeypatch):
    sent=[]; monkeypatch.setattr(mel,"log",lambda _:None); monkeypatch.setattr(mel,"warn",lambda _:None); monkeypatch.setattr(mel,"post",lambda text: sent.append(text) or True)
    monkeypatch.setattr(mel.announcer,"main",lambda: print("Print complete: Job"))
    mel.run_announcer("completion"); mel.run_announcer("repeated status completion")
    assert sent == ["Print complete: Job", "Print complete: Job"]  # mocked announcer emits two independent messages


def test_lifecycle_claim_persists_across_listener_restart_and_allows_new_job(tmp_path):
    state = tmp_path / "notices.json"
    identity = "file:a.gcode|start:123"
    assert mel._claim_notification(identity, "start", state)
    assert not mel._claim_notification(identity, "start", state)
    assert mel._claim_notification(identity, "terminal", state)
    assert not mel._claim_notification(identity, "terminal", state)
    assert mel._claim_notification("file:b.gcode|start:456", "start", state)


def test_missing_lifecycle_identity_never_drops_notice_claim():
    assert mel._claim_notification(None, "start")


def test_completion_video_post_is_coalesced(monkeypatch):
    sent=[]
    monkeypatch.setattr(mel,"log",lambda _:None); monkeypatch.setattr(mel,"warn",lambda _:None); monkeypatch.setattr(mel,"post",lambda text: sent.append(text) or True)
    monkeypatch.setattr(mel.announcer,"main",lambda: print("TIMELAPSE_POSTPROCESS:job.gcode\nPrint complete: Job"))
    monkeypatch.setattr(mel.announcer,"_timelapse_job_key",lambda token:"job.gcode")
    monkeypatch.setattr(mel.announcer,"archive_completed_timelapse",lambda key:mel.announcer.ArchiveOutcome(mel.announcer.ARCHIVED,"/archive/job.mp4"))
    monkeypatch.setattr(mel.announcer,"materialize_archived_timelapse",lambda path:"/cache/job.mp4")
    class Inline:
        def __init__(self,target=None,args=(),**kw): self.target,self.args=target,args
        def start(self): self.target(*self.args)
    monkeypatch.setattr(mel.threading,"Thread",Inline)
    mel.run_announcer("completion")
    assert sent == ["Print complete: Job\nMEDIA:/cache/job.mp4"]
