"""#1536 checklist item 6: guided setup asks who counts filament usage
when a Spoolman URL was given, and writes the choice where the settings
read it back."""
from print_handy import settings
from print_handy.bot import guided

from tests.bot.test_guided import say, store


def _drive_to_spoolman(tmp_path):
    st = store(tmp_path)
    say(st, "/print-handy setup")
    for answer in ("discord", "flashforge.ad5x", "100000000000000001",
                   "200000000000000002", "skip"):
        say(st, answer)
    return st


def test_accounting_step_follows_a_spoolman_url_and_explains_the_choice(tmp_path):
    st = _drive_to_spoolman(tmp_path)
    prompt = say(st, "http://spoolman.test:7912")
    assert "Spoolman accounting" in prompt
    assert "printer" in prompt and "print_handy" in prompt and "auto" in prompt
    # The prompt recommends auto and names the OpenCreator case.
    assert "recommended" in prompt and "OpenCreator" in prompt


def test_accounting_step_is_skipped_without_a_spoolman_url(tmp_path):
    st = _drive_to_spoolman(tmp_path)
    prompt = say(st, "skip")
    assert "Spoolman accounting" not in prompt


def test_accounting_validator_accepts_the_spellings_and_refuses_others(tmp_path):
    validate = guided._validate_spoolman_accounting
    assert validate("printer", {}) == "printer"
    assert validate("Moonraker", {}) == "printer"
    assert validate("print handy", {}) == "print_handy"
    assert validate("skip", {}) == "auto"
    assert validate("", {}) == "auto"
    try:
        validate("sometimes", {})
    except ValueError as exc:
        assert "printer" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a bad accounting value was accepted")


def test_apply_writes_the_accounting_choice_and_review_shows_it(tmp_path):
    st = _drive_to_spoolman(tmp_path)
    say(st, "http://spoolman.test:7912")
    say(st, "printer")
    review = ""
    for answer in ("skip", "skip", "stored", "skip"):
        review = say(st, answer)
    assert "Spoolman accounting: printer" in review
    target = tmp_path / "local.yaml"
    say(st, "apply", apply_config=lambda answers: guided._apply_answers(answers, target))
    written = target.read_text()
    assert "accounting: printer" in written
    data = settings.load(local=target, environ={}, user=False)
    assert data["spool"]["accounting"] == "printer"
