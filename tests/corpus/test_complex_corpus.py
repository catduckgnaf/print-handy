"""Offline contract tests for the #458 complex corpus manifest.

These pin the catalogue, not the models: the model files are fetched (see
fetch_complex_corpus.py) and never committed, so what CI can check is that
the manifest is complete, licence-gated, measured, and domain-covering.
Flow runs against the fetched files are run_complex_corpus.py's job and are
reported there, not faked here.
"""
import hashlib
import http.client
import json
import os
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import fetch_complex_corpus


@pytest.mark.parametrize('error', [RuntimeError('private technical detail'), TimeoutError('private timeout')])
def test_private_case_cancels_current_slice_failure_prompt(error):
    from print_handy.core.slice_failure import post
    # The corpus must answer the actual product prompt, not a retired wording.
    rules = fetch_complex_corpus.CASE_CLICKS['duplo-swing-two-seats'] + fetch_complex_corpus.GENERIC_CLICKS
    question = post(error)
    picked = next(row['button'] for row in rules if row['question'].casefold() in question.casefold())
    assert picked == 'Cancel'

MANIFEST = Path(__file__).resolve().parent / "complex_cases.toml"
CASES = tomllib.loads(MANIFEST.read_text())["case"]
PUBLIC = [c for c in CASES if c["tier"] == "public"]
PRIVATE = [c for c in CASES if c["tier"] == "private"]
READY = [c for c in CASES if c["status"] == "ready"]

# Licence names recorded at download time that permit redistribution in the
# public corpus: Printables GraphQL licence names and the NASA wording.
PUBLIC_LICENSES = {
    "Creative Commons — Public Domain",
    "Creative Commons — Attribution",
    "Creative Commons — Attribution — Share Alike",
    "Creative Commons — Attribution  — Share Alike",
}
DOMAINS = {"moonraker", "bambulab_lan", "prusalink", "elegoo_sdcp",
           "octoprint", "flashforge_stock", "offline"}


def test_manifest_covers_all_twelve_catalogue_cases_and_four_private():
    assert len(PUBLIC) == 12, [c["slug"] for c in PUBLIC]
    assert len(PRIVATE) == 4, [c["slug"] for c in PRIVATE]
    assert len({c["slug"] for c in CASES}) == 16


def test_every_domain_has_a_case():
    covered = {c["target_domain"] for c in PUBLIC}
    assert DOMAINS <= covered, f"domains without a public case: {DOMAINS - covered}"


def test_public_cases_are_redistributable_and_private_cases_are_flagged():
    for case in PUBLIC:
        if case["site"] == "nasa":
            assert case["license"].startswith("Public domain"), case["slug"]
        elif case["site"] == "smithsonian":
            assert case["license"].startswith("Public domain"), case["slug"]
        else:
            assert case["license"] in PUBLIC_LICENSES, (case["slug"], case["license"])
    for case in PRIVATE:
        assert case["license"] not in PUBLIC_LICENSES, case["slug"]
        assert "PRIVATE" in case.get("notes", "") or case["status"] != "ready", case["slug"]


def test_ready_cases_carry_measured_stats():
    for case in READY:
        assert re.fullmatch(r"[0-9a-f]{64}", case["sha256"]), case["slug"]
        assert case["bytes"] > 0 and case["triangles"] > 0, case["slug"]
        assert len(case["dims_mm"]) == 3 and all(d > 0 for d in case["dims_mm"]), case["slug"]
        assert case["file"].endswith((".stl", ".3mf")), case["slug"]
        for extra in case.get("extra_file", []):
            assert re.fullmatch(r"[0-9a-f]{64}", extra["sha256"]), (case["slug"], extra["name"])
            assert extra["triangles"] > 0 and len(extra["dims_mm"]) == 3


def test_blocked_cases_say_why_and_claim_no_file():
    for case in CASES:
        if case["status"] != "ready":
            assert case.get("blocked_reason"), case["slug"]
            assert "sha256" not in case and "file" not in case, case["slug"]


def test_complex_cases_are_actually_complex():
    """The set, as a whole, must clear the bars #458 set - not 15 variations
    of one small model."""
    triangles = [c["triangles"] for c in READY]
    assert max(triangles) >= 1_000_000, triangles
    assert sum(t >= 100_000 for t in triangles) >= 6, triangles
    assert any(c["bytes"] >= 30_000_000 for c in READY)
    assert any(max(c["dims_mm"]) >= 290 for c in READY), "no near-300mm case"
    assert any("painted" in c.get("colors", "") or "paint" in c.get("colors", "")
               for c in READY), "no painted-colour case"


def test_targets_exist_in_the_printer_registry():
    registry_files = sorted((Path(__file__).resolve().parents[2] / "print_handy" / "data" / "printers-registry").glob("*.toml"))
    assert registry_files, "printer registry directory is missing or empty"
    registry = "\n".join(path.read_text() for path in registry_files)
    keys = set(re.findall(r'^key = "([^"]+)"', registry, re.M))
    for case in CASES:
        assert case["target_printer"] in keys, (case["slug"], case["target_printer"])


def test_case_clicks_take_precedence_over_generic_clicks():
    """The harness answers with the FIRST matching [[click]] rule, so a case's
    own rules must be written before the generic ones - otherwise a case can
    never pick per-part colours or '100% · rearrange 1 plate'."""
    case = next(c for c in CASES if c["slug"] == "duplo-swing-two-seats")
    data = tomllib.loads(fetch_complex_corpus.case_toml(case))
    clicks = data["click"]
    own = fetch_complex_corpus.CASE_CLICKS[case["slug"]]
    assert clicks[:len(own)] == own
    assert clicks[len(own):] == fetch_complex_corpus.GENERIC_CLICKS

    def first_answer(question):
        return next(r["button"] for r in clicks if r["question"].casefold() in question.casefold())

    assert first_answer("Inspected 9 parts: ... Choose colors for these parts or one color "
                        "for everything?") == "Choose colors"
    assert first_answer("Duplo swing - Two seats does not fit the Flashforge AD5X bed full "
                        "size as one plate.") == "rearrange 1 plate"
    # A case without its own rules keeps exactly the generic answers.
    plain = next(c for c in CASES if c["slug"] == "stanford-dragon")
    assert tomllib.loads(fetch_complex_corpus.case_toml(plain))["click"] == \
        fetch_complex_corpus.GENERIC_CLICKS


def test_case_toml_writes_table_expectations_as_toml():
    case = next(c for c in CASES if c["slug"] == "duplo-swing-two-seats")
    expect = tomllib.loads(fetch_complex_corpus.case_toml(case))["expect"]
    assert expect == fetch_complex_corpus.CASE_EXPECT["duplo-swing-two-seats"]


def test_expected_failures_are_error_substring_lists_on_ready_cases():
    for case in CASES:
        if "expected_failure" in case:
            known = case["expected_failure"]
            assert case["status"] == "ready", case["slug"]
            assert isinstance(known, list) and known and all(isinstance(t, str) and t for t in known)


def test_makerworld_link_uses_the_production_download_call(monkeypatch):
    from print_handy.core import model_job
    seen = {}

    def fake_request_json(url, headers=None, **_kw):
        seen.update(url=url, headers=headers)
        return {"name": "x.3mf", "url": "https://example.test/x.3mf"}

    monkeypatch.setattr(model_job, "maker_headers", lambda: {"Authorization": "Bearer t"})
    monkeypatch.setattr(model_job, "request_json", fake_request_json)
    assert fetch_complex_corpus.makerworld_link("976429") == "https://example.test/x.3mf"
    assert seen["url"].endswith("/design-service/instance/976429/f3mf?type=download")
    assert seen["headers"] == {"Authorization": "Bearer t"}


def test_makerworld_robot_check_is_reported_not_bypassed(monkeypatch):
    import urllib.error
    from print_handy.core import model_job

    def robot_check(url, **_kw):
        raise urllib.error.HTTPError(url, 418, "I'm a teapot", None, None)

    monkeypatch.setattr(model_job, "maker_headers", lambda: {})
    monkeypatch.setattr(model_job, "request_json", robot_check)
    with pytest.raises(RuntimeError, match="robot check"):
        fetch_complex_corpus.makerworld_link("976429")


class _FakeResponse:
    """A stand-in for the _open_direct response: read() either returns the
    canned body or raises the canned error (e.g. a truncated read)."""

    def __init__(self, body=None, error=None):
        self._body = body
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if self._error is not None:
            raise self._error
        return self._body


_GOOD_LINK_BODY = json.dumps({"data": {"getDownloadLink": {
    "ok": True, "output": {"link": "https://example.test/model.stl"},
    "errors": None}}}).encode()


def _stub_open_direct(monkeypatch, responses):
    calls = []

    def fake_open(url, data=None, headers=None):
        calls.append(url)
        return responses[len(calls) - 1]

    monkeypatch.setattr(fetch_complex_corpus, "_open_direct", fake_open)
    monkeypatch.setattr(fetch_complex_corpus, "time",
                        SimpleNamespace(sleep=lambda *_args: None))
    return calls


def test_printables_link_retries_a_truncated_read(monkeypatch):
    """A response truncated mid-read (http.client.IncompleteRead, seen once
    in the real fetch smoke run) is retried, not a crash."""
    calls = _stub_open_direct(monkeypatch, [
        _FakeResponse(error=http.client.IncompleteRead(b"partial", 123)),
        _FakeResponse(body=_GOOD_LINK_BODY),
    ])
    assert fetch_complex_corpus.printables_link("11", "22") == \
        "https://example.test/model.stl"
    assert len(calls) == 2


def test_printables_link_gives_up_after_bounded_retries(monkeypatch):
    calls = _stub_open_direct(monkeypatch, [
        _FakeResponse(error=http.client.IncompleteRead(b"partial"))
        for _ in range(3)
    ])
    with pytest.raises(http.client.IncompleteRead):
        fetch_complex_corpus.printables_link("11", "22")
    assert len(calls) == 3


@pytest.mark.skipif(not (os.environ.get("COMPLEX_CORPUS_ROOT")
                         or os.environ.get("PRINT_HANDY_PRIVATE_CORPUS")),
                    reason="complex corpus not fetched here - run fetch_complex_corpus.py; "
                           "the manifest contract above is what CI pins")
def test_fetched_files_match_the_manifest():
    # Accept the same root the model-corpus workflow and
    # run_complex_corpus.py use (PRINT_HANDY_PRIVATE_CORPUS), not only the
    # pytest-only COMPLEX_CORPUS_ROOT name no workflow sets (2026-11-07
    # test-suite cleanup).
    root = Path(os.environ.get("COMPLEX_CORPUS_ROOT")
                or os.environ["PRINT_HANDY_PRIVATE_CORPUS"])
    for case in CASES:
        if case["tier"] != "public" or case["status"] != "ready":
            continue
        path = root / case["slug"] / case["file"]
        assert path.is_file(), f"{case['slug']}: run fetch_complex_corpus.py"
        assert path.stat().st_size == case["bytes"], case["slug"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == case["sha256"], case["slug"]
