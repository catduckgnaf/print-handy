"""#467: plain-language failure/refusal posts.

Corpus pins on the rewritten surfaces plus the negative pin: no post in
the inventoried set may contain a raw exception string, a traceback
fragment, or an ALL-CAPS failure prefix (``REFUSED:`` / ``UPLOAD
FAILED`` / ``START FAILED`` / ``G-CODE REFUSED``). Those strings still
exist *inside* the pipeline as transport (print_next parses a trailing
``REFUSED:`` line; the receipt and the log keep the full detail) — the
pin is on what a person in chat is shown, which is the translation
layer's output.
"""
import re

from print_handy import diagnostics
from print_handy.bot import guided, runtime
from print_handy.core import prepared_intake

RAW_MARKERS = ("Slicing failed:", "UPLOAD FAILED", "START FAILED", "REFUSED:",
               "G-CODE REFUSED", "Traceback", "RuntimeError", "OSError",
               "opaque_code", "QX-19", "stderr tail")

POSTS = []


def collect(post):
    POSTS.append(post)
    return post


# --------------------------------------------------------------------- helper

def test_plain_post_shape_plain_first_line_marker_last():
    post = diagnostics.plain_post(
        "Slicing this model did not work.",
        not_happened="Nothing was uploaded or started.",
        next_step="Try again.",
        subsystem="slicing", exc=RuntimeError("secret raw detail"))
    lines = post.splitlines()
    assert lines[0] == "Slicing this model did not work."
    assert "Nothing was uploaded or started." in post and "Try again." in post
    assert lines[-1].startswith("Reference: [PH_SLICING_")
    assert "secret raw detail" not in post and "RuntimeError" not in post


def test_plain_post_without_subsystem_has_no_marker():
    post = diagnostics.plain_post("That did not work.", next_step="Try again.")
    assert "Reference:" not in post


# --------------------------------------------------------------------- unmatched fallback

def test_unmatched_reply_is_short_plain_and_offers_help_on_request():
    post = collect(runtime.unmatched_text())
    assert post.startswith("I didn't understand that, so nothing happened.")
    assert "/print-help" in post
    assert len(post.splitlines()) <= 4
    # It is not the command list: no command dump lines.
    assert "/print-next" not in post and "/print-stop" not in post


def test_explicit_help_still_returns_the_full_list():
    # The no-dump rule is about the *error* path only; asking for help works.
    from print_handy.bot import commands
    assert "/print-next" in commands.help_text()


# --------------------------------------------------------------------- start-failure translation

def test_plain_start_failure_translates_refused_tails():
    tail = ("TIMING {\"stage\": \"print_now\"}\n"
            "REFUSED: idle guard failed: native rightTargetTemp == 0 (observed 210.0). "
            "Nothing uploaded or started.")
    post = collect(prepared_intake.plain_start_failure(tail))
    assert post.startswith("Didn't start:")
    assert "210" in post and "Retry" in post


def test_plain_start_failure_hides_unknown_refusal_detail():
    tail = "REFUSED: internal probe 7 failed with opaque_code=QX-19. Nothing uploaded or started."
    post = collect(prepared_intake.plain_start_failure(tail))
    assert "opaque_code" not in post and "QX-19" not in post
    assert "could not confirm it was ready" in post


def test_plain_start_failure_upload_and_start_tails_become_uncertain_wording():
    for tail in ("The file could not be uploaded to the printer, so nothing started.\nDetail: boom",
                 "print-now exited 1 with no output",
                 "UNCERTAIN: print-now timed out. The plate may already have uploaded."):
        post = collect(prepared_intake.plain_start_failure(tail))
        assert "did not confirm whether the print started" in post
        assert "Check the printer" in post


# --------------------------------------------------------------------- printer_state warnings

def test_printer_state_warnings_are_plain_and_keep_the_fail_soft_posture(monkeypatch):
    from print_handy import printers, settings
    from print_handy.core import printer_state, spoolman_client
    config = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"})
    adapter = printers.get("klipper.generic", config)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)
    monkeypatch.setattr(settings, "current",
                        lambda: {**config, "services": {"spoolman_url": "http://configured-spoolman"}})

    def get(url, data=None, method=None, timeout=6):
        if "configfile" in url:
            return {"result": {"status": {"configfile": {"settings": {
                "extruder": {"nozzle_diameter": "0.4", "pressure_advance": {"value": 0.04}}}}}}}
        if "spool_id" in url:
            raise OSError("raw spool boom")
        raise AssertionError(url)

    def spoolman_down(method, path, body=None, *, base_url=None, timeout=5.0):
        raise spoolman_client.SpoolmanUnavailable("raw spoolman boom")

    monkeypatch.setattr(printer_state, "_get", get)
    monkeypatch.setattr(spoolman_client, "request", spoolman_down)
    monkeypatch.setattr(printer_state, "STATE", "/nonexistent-for-check")
    state = {"moonraker_url": "http://printer.test", "spoolman_url": "http://configured-spoolman",
             "nozzle": {"diameter_mm": 0.4},
             "spool": {"spoolman_id": 7, "name": "Black PLA", "material": "PLA"},
             "temps_c": {"nozzle": 210, "bed": 60}}
    ok, problems, facts = printer_state.check(state)
    assert ok and problems == []  # #235: warnings never block
    for warning in facts["warnings"]:
        collect(warning)
        assert "boom" not in warning and "failed:" not in warning
    assert any("recorded spool was used" in w for w in facts["warnings"])


def test_printer_state_unreachable_problem_is_plain(monkeypatch):
    from print_handy import printers, settings
    from print_handy.core import printer_state
    config = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"})
    adapter = printers.get("klipper.generic", config)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)

    def get(url, data=None, method=None, timeout=6):
        raise OSError("raw network boom")

    monkeypatch.setattr(printer_state, "_get", get)
    monkeypatch.setattr(printer_state, "STATE", "/nonexistent-for-check")
    state = {"moonraker_url": "http://printer.test", "spoolman_url": "",
             "nozzle": {"diameter_mm": 0.4},
             "spool": {"spoolman_id": None, "name": "Black PLA", "material": "PLA"},
             "temps_c": {"nozzle": 210, "bed": 60}}
    ok, problems, facts = printer_state.check(state)
    assert not ok
    assert problems and all("boom" not in p for p in problems)
    assert any("could not be reached" in p for p in problems)


# --------------------------------------------------------------------- guided debugging findings

def test_guided_debugging_findings_do_not_carry_raw_exceptions(tmp_path):
    import os
    from pathlib import Path
    from print_handy import settings
    cfg = settings.load(local=Path(os.devnull), environ={})
    cfg["paths"]["state_home"] = tmp_path / "profile"
    (tmp_path / "profile" / "state").mkdir(parents=True, exist_ok=True)
    cfg["bot"]["mode"] = "standalone"
    cfg["discord"].update(chat_id="123456", allowed_user_ids="789")
    cfg["services"].update(moonraker_url="http://printer.test:7125", spoolman_url="")

    def probe(url):
        raise OSError("raw probe boom")

    def login(token):
        raise OSError("raw login boom")

    findings = guided.run_diagnostics(loader=lambda: cfg, token_getter=lambda cfg: "x9f8-token-value-zz",
                                      probe=probe, login_probe=login,
                                      which=lambda exe: "/usr/bin/" + str(exe),
                                      version_runner=lambda command: "OrcaSlicer 2.0",
                                      environ={})
    texts = [f.detail + " " + f.action for f in findings]
    joined = "\n".join(texts)
    assert "raw probe boom" not in joined and "raw login boom" not in joined
    assert "login check did not pass" in joined
    assert "the health check did not answer" in joined
    for text in texts:
        collect(text)


# --------------------------------------------------------------------- model_job pipeline exits

def test_model_job_detail_tail_demotes_raw_text():
    from print_handy.core import model_job
    assert model_job._detail_tail("") == ""
    tail = model_job._detail_tail("some\n  raw   stderr tail")
    assert tail == "\nDetail: some raw stderr tail"
    assert len(model_job._detail_tail("x" * 1000)) <= len("\nDetail: ") + 300


# --------------------------------------------------------------------- the negative pin

def test_no_inventoried_post_carries_raw_machinery():
    # Rebuild the corpus inside this test so the pin stands alone.
    corpus = [
        runtime.unmatched_text(),
        runtime.REFUSAL_NOTICE,
        prepared_intake.plain_start_failure(
            "REFUSED: internal probe 7 failed with opaque_code=QX-19. Nothing uploaded or started."),
        prepared_intake.plain_start_failure("UNCERTAIN: print-now timed out."),
        diagnostics.plain_post("Slicing this model did not work.",
                               not_happened="No G-code was kept.",
                               next_step="Retry once from the saved model?",
                               subsystem="slicing", exc=RuntimeError("raw slice boom")),
    ]
    caps_prefix = re.compile(r"^(REFUSED|UPLOAD FAILED|START FAILED|G-CODE REFUSED|FAILED|ERROR)\b", re.M)
    for post in corpus:
        for marker in RAW_MARKERS:
            assert marker not in post, (marker, post)
        assert not caps_prefix.search(post), post
        assert "raw slice boom" not in post
