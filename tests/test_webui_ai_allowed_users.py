"""The AI edit users field survives an unchanged Settings page save.

The field once held a list, the page drew the list's printed form ("[]")
in its text input, and a save with no edits wrote ``['[]']`` back to the
settings file: the AI edit lane then allowed only a user named "[]", and
every later save nested it further. The value is now one ID string, and
an earlier save's printed list reads back as the IDs it held.
"""
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from print_handy import settings
from print_handy.core import edit_plan
from print_handy.webui import read_fields, write_changes
from print_handy.webui import pages

FIELD = "ai.allowed_users"


class _Form(HTMLParser):
    """Every value the browser would submit for the rendered form."""

    def __init__(self):
        super().__init__()
        self.values = {}
        self._select = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name")
        if tag == "input" and name:
            if attrs.get("type") == "checkbox" and "checked" not in attrs:
                return
            self.values.setdefault(name, []).append(attrs.get("value", ""))
        elif tag == "select":
            self._select = name
        elif tag == "option" and self._select and "selected" in attrs:
            self.values.setdefault(self._select, []).append(attrs.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "select":
            self._select = None


def submitted_form(field_values, data, section):
    page = pages.render_config_page(field_values, data=data, form_token="t", section=section)
    form = _Form()
    form.feed(page)
    return form.values


class AllowedUsersSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / "local.yaml"

    def unchanged_submit(self, text):
        self.local.write_text(text)
        before = self.local.read_bytes()
        data = settings.load(local=self.local, environ={})
        field_values = read_fields(local=self.local, environ={})
        form = submitted_form(field_values, data, "ai")
        whole, _submitted, errors = pages.parse_form(form, field_values)
        single, _submitted, single_errors = pages.parse_form(form, field_values, only={FIELD})
        return form, whole, errors, single, single_errors, before

    def test_the_default_renders_empty_and_an_unchanged_save_changes_nothing(self):
        form, whole, errors, single, single_errors, before = self.unchanged_submit(
            "printer:\n  model: klipper.generic\n")
        self.assertEqual(form[FIELD], [""])  # never the printed list "[]"
        self.assertEqual(errors, {})
        self.assertEqual(single_errors, {})
        self.assertNotIn(FIELD, {field.id for field in whole})
        self.assertEqual(single, {})
        self.assertEqual(write_changes({f.id: v for f, v in whole.items()},
                                       config_path=self.local, environ={}), [])
        self.assertEqual(self.local.read_bytes(), before)

    def test_a_stored_list_of_ids_round_trips_as_no_change(self):
        form, whole, _e, single, _se, _before = self.unchanged_submit(
            "ai:\n  allowed_users: ['123', '@name:matrix.org']\n")
        self.assertEqual(form[FIELD], ["123, @name:matrix.org"])
        self.assertNotIn(FIELD, {field.id for field in whole})
        self.assertEqual(single, {})

    def test_an_earlier_printed_list_is_read_as_empty_and_never_nested_again(self):
        for stored in ("['[]']", "[\"['[]']\"]", "'[]'"):
            with self.subTest(stored=stored):
                form, whole, _e, single, _se, _before = self.unchanged_submit(
                    f"ai:\n  allowed_users: {stored}\n")
                data = settings.load(local=self.local, environ={})
                self.assertEqual(data["ai"]["allowed_users"], "")
                self.assertEqual(form[FIELD], [""])
                self.assertEqual(single, {})
                # The lane is open again, not locked to a user named "[]".
                self.assertEqual(edit_plan.ai_lane_allowed("42", data["ai"], {}, 0)[0], True)

    def test_a_typed_id_list_saves_and_gates_the_lane(self):
        self.local.write_text("printer:\n  model: klipper.generic\n")
        changed = write_changes({FIELD: "123 456,789"}, config_path=self.local, environ={})
        self.assertEqual(changed, [("ai", "allowed_users")])
        data = settings.load(local=self.local, environ={})
        self.assertEqual(data["ai"]["allowed_users"], "123, 456, 789")
        self.assertEqual(settings.ai_allowed_users(data["ai"]["allowed_users"]), ["123", "456", "789"])
        self.assertEqual(edit_plan.ai_lane_allowed("456", data["ai"], {}, 0)[0], True)
        self.assertEqual(edit_plan.ai_lane_allowed("42", data["ai"], {}, 0),
                         (False, "not on the AI allowlist"))

    def test_the_environment_spelling_is_accepted(self):
        data = settings.load(local=False, environ={"PRINT_HANDY__AI__ALLOWED_USERS": "7, 8"})
        self.assertEqual(data["ai"]["allowed_users"], "7, 8")


if __name__ == "__main__":
    unittest.main()
