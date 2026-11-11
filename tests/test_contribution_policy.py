"""Offline regression coverage for PR documentation policy."""
import importlib.util
import unittest
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_contribution.py"

class PolicyTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("policy", SCRIPT)
        self.policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.policy)

    def body(self):
        return """## Summary and user impact
A new option for operators.
## Verification and test results
Ran the focused offline tests: 4 passed.
## Tests added and regression scoping
Tests added: none
Tests added detail: No new test was added because the existing settings tests in tests/test_settings.py already cover this option's behavior.
Regression scoping: checked
Regression scoping reason: The settings and defaults regression tests were considered; the focused run covers them and CI runs the full suite on this head.
Regression test: not a bug fix
Regression test detail: This adds a new operator option; nothing that worked before was broken.
## Documentation impact
Documentation: updated
Reason: New configuration option documented in docs/CONFIGURATION-YAML.md.
Wiki: not applicable
Wiki reason: Versioned docs are authoritative; no separate wiki page exists.
## Surfaces and variables
Variables: checked
Variables reason: The new option was checked on every surface: defaults tree, environment loader, Web UI catalog, consuming module, docs and templates.
"""

    def test_markdown_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("[ok](guide.md) [remote](https://example.com) [anchor](#heading)")
            (root / "guide.md").write_text("# Guide")
            self.assertEqual([], self.policy.check_links(root, ["README.md", "deleted.md"]))
            (root / "README.md").write_text("[bad](missing.md) [escape](../outside.md)")
            self.assertEqual(2, len(self.policy.check_links(root, ["README.md"])))

    def test_valid_feature(self):
        self.assertEqual([], self.policy.check_pr(self.body(), ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"]))

    def test_feature_without_docs_requires_reasoned_exemption(self):
        body = self.body().replace("Documentation: updated", "Documentation: not needed").replace("New configuration option documented in docs/CONFIGURATION-YAML.md.", "Internal rename only; operator behavior and settings are unchanged.")
        self.assertEqual([], self.policy.check_pr(body, ["print_handy/core/internal.py"]))

    def test_updated_requires_changed_docs(self):
        self.assertTrue(self.policy.check_pr(self.body(), ["print_handy/settings.py"]))

    def test_template_comments_do_not_count(self):
        self.assertTrue(self.policy.check_pr("<!-- " + self.body() + " -->", ["README.md"]))

    def test_wiki_evidence_required(self):
        self.assertTrue(self.policy.check_pr(self.body().replace("Wiki: not applicable", "Wiki: updated"), ["docs/README.md"]))

    def test_placeholder_rejected(self):
        self.assertTrue(self.policy.check_pr(self.body().replace("Reason: New configuration option documented in docs/CONFIGURATION-YAML.md.", "Reason: TODO"), ["docs/README.md"]))

    def test_empty_reason_does_not_capture_the_next_line(self):
        body = self.body().replace("Reason: New configuration option documented in docs/CONFIGURATION-YAML.md.", "Reason:")
        errors = self.policy.check_pr(body, ["docs/README.md"])
        self.assertTrue(any(error.startswith("Reason:") for error in errors))
        self.assertEqual("", self.policy.field("Reason:\nWiki: not applicable", "Reason"))

    def test_unfilled_template_is_rejected_and_filled_template_passes(self):
        template = (Path(__file__).resolve().parents[1] / ".github/PULL_REQUEST_TEMPLATE.md").read_text()
        self.assertTrue(self.policy.check_pr(template, ["docs/README.md"]))
        filled = (template.replace("Documentation: updated | not needed", "Documentation: updated")
                  .replace("Reason:\n", "Reason: Settings reference updated for the new option.\n")
                  .replace("Wiki: updated | not applicable | deferred", "Wiki: not applicable")
                  .replace("Wiki reason:\n", "Wiki reason: Versioned docs are authoritative for this change.\n")
                  .replace("Variables: checked | not applicable", "Variables: not applicable")
                  .replace("Variables reason:\n", "Variables reason: This change adds or changes no variables at all.\n")
                  .replace("Tests added: added | none", "Tests added: none")
                  .replace("Tests added detail:\n", "Tests added detail: No new test: the existing settings tests already cover this option.\n")
                  .replace("Regression scoping: checked | not applicable", "Regression scoping: checked")
                  .replace("Regression scoping reason:\n", "Regression scoping reason: Neighboring regression suites were considered and CI runs the full suite on this head.\n")
                  .replace("Regression test: added | not a bug fix | not possible", "Regression test: not a bug fix")
                  .replace("Regression test detail:\n", "Regression test detail: A new option; it changes nothing that already worked.\n"))
        filled = filled.replace("## Summary and user impact\n", "## Summary and user impact\nA new option for operators.\n")
        filled = filled.replace("## Verification and test results\n", "## Verification and test results\nRan the focused offline tests: 4 passed.\n")
        self.assertEqual([], self.policy.check_pr(filled, ["docs/README.md"]))

    def test_missing_verification_rejected(self):
        self.assertTrue(self.policy.check_pr(self.body().replace("Ran the focused offline tests: 4 passed.", ""), ["docs/README.md"]))

    def test_untouched_verification_prompts_are_not_evidence(self):
        """P2-43: the template's bare prompts ('- Checks run:' / '- Checks not
        run and why:') exceed the length threshold on their own, so an
        untouched verification section passed as if it were evidence."""
        body = self.body().replace("Ran the focused offline tests: 4 passed.",
                                   "- Checks run:\n- Checks not run and why:")
        errors = self.policy.check_pr(body, ["docs/README.md"])
        self.assertTrue(any("Verification" in error for error in errors), errors)

    def test_answered_verification_prompts_pass(self):
        body = self.body().replace("Ran the focused offline tests: 4 passed.",
                                   "- Checks run: pytest tests/test_x.py -q, 4 passed\n"
                                   "- Checks not run and why: live printer checks need the bench printer")
        self.assertEqual([], self.policy.check_pr(body, ["docs/README.md"]))
        body = self.body().replace("Ran the focused offline tests: 4 passed.",
                                   "- Checks not run and why: docs-only change; the offline suite covers it")
        self.assertEqual([], self.policy.check_pr(body, ["docs/README.md"]))

    def test_deferred_wiki_needs_issue_link(self):
        body = self.body().replace("Wiki: not applicable", "Wiki: deferred")
        self.assertTrue(self.policy.check_pr(body, ["docs/README.md"]))
        body = body.replace("Wiki reason: Versioned docs are authoritative; no separate wiki page exists.",
                            "Wiki reason: Versioned docs are authoritative; tracked in https://github.com/catduckgnaf/print-handy/issues/120")
        self.assertEqual([], self.policy.check_pr(body, ["docs/README.md"]))

    def test_missing_testing_section_rejected(self):
        body = self.body().split("## Tests added and regression scoping")[0] + "## Documentation impact" + self.body().split("## Documentation impact", 1)[1]
        errors = self.policy.check_pr(body, ["docs/README.md"])
        self.assertTrue(any("Tests added" in e for e in errors), errors)
        self.assertTrue(any("Regression scoping" in e for e in errors), errors)

    def test_bad_tests_added_literal_rejected(self):
        body = self.body().replace("Tests added: none", "Tests added: some")
        self.assertTrue(any("Tests added: must be" in e for e in self.policy.check_pr(body, ["docs/README.md"])))

    def test_tests_none_without_reason_rejected(self):
        body = self.body().replace(
            "No new test was added because the existing settings tests in tests/test_settings.py already cover this option's behavior.", "TODO")
        errors = self.policy.check_pr(body, ["docs/README.md"])
        self.assertTrue(any("Tests added detail" in e for e in errors), errors)

    def test_tests_none_with_specific_reason_passes(self):
        self.assertEqual([], self.policy.check_pr(self.body(), ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"]))

    def test_tests_added_without_test_file_rejected(self):
        body = self.body().replace("Tests added: none", "Tests added: added").replace(
            "No new test was added because the existing settings tests in tests/test_settings.py already cover this option's behavior.",
            "Added test_option_default in tests/test_settings.py, pinning the new option's default value.")
        errors = self.policy.check_pr(body, ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"])
        self.assertTrue(any("no test file changed" in e for e in errors), errors)

    def test_tests_added_with_test_file_passes(self):
        body = self.body().replace("Tests added: none", "Tests added: added").replace(
            "No new test was added because the existing settings tests in tests/test_settings.py already cover this option's behavior.",
            "Added test_option_default in tests/test_settings.py, pinning the new option's default value.")
        self.assertEqual([], self.policy.check_pr(body, ["print_handy/settings.py", "tests/test_settings.py", "docs/CONFIGURATION-YAML.md"]))

    def test_bad_regression_scoping_literal_rejected(self):
        body = self.body().replace("Regression scoping: checked", "Regression scoping: done")
        self.assertTrue(any("Regression scoping: must be" in e for e in self.policy.check_pr(body, ["docs/README.md"])))

    def test_regression_scoping_without_reason_rejected(self):
        body = self.body().replace(
            "The settings and defaults regression tests were considered; the focused run covers them and CI runs the full suite on this head.", "TODO")
        errors = self.policy.check_pr(body, ["docs/README.md"])
        self.assertTrue(any("Regression scoping reason" in e for e in errors), errors)

    def test_regression_scoping_not_applicable_with_reason_passes(self):
        body = self.body().replace("Regression scoping: checked", "Regression scoping: not applicable").replace(
            "The settings and defaults regression tests were considered; the focused run covers them and CI runs the full suite on this head.",
            "This change touches no code or behavior, so there is no regression surface to scope.")
        self.assertEqual([], self.policy.check_pr(body, ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"]))

    def test_missing_surfaces_section_rejected(self):
        body = self.body().split("## Surfaces and variables")[0]
        self.assertTrue(any("Variables" in e for e in self.policy.check_pr(body, ["docs/README.md"])))

    def test_bad_variables_literal_rejected(self):
        body = self.body().replace("Variables: checked", "Variables: done")
        self.assertTrue(any("Variables: must be" in e for e in self.policy.check_pr(body, ["docs/README.md"])))

    def test_variables_not_applicable_without_reason_rejected(self):
        body = self.body().replace("Variables: checked", "Variables: not applicable").replace(
            "The new option was checked on every surface: defaults tree, environment loader, Web UI catalog, consuming module, docs and templates.", "TODO")
        errors = self.policy.check_pr(body, ["docs/README.md"])
        self.assertTrue(any("Variables reason" in e for e in errors))

    def test_variables_not_applicable_with_reason_passes(self):
        body = self.body().replace("Variables: checked", "Variables: not applicable").replace(
            "The new option was checked on every surface: defaults tree, environment loader, Web UI catalog, consuming module, docs and templates.",
            "This PR changes no configuration or settings variable; it is a test-only change.")
        self.assertEqual([], self.policy.check_pr(body, ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"]))

    # -- multi-line fields ----------------------------------------------------

    def test_a_field_answered_by_the_bullet_list_under_it_is_read(self):
        text = "Tests added detail:\n- `tests/test_a.py`: pins the new path\n- `tests/test_b.py`: pins the refusal\n\nRegression scoping: checked"
        self.assertEqual("- `tests/test_a.py`: pins the new path\n- `tests/test_b.py`: pins the refusal",
                         self.policy.field(text, "Tests added detail"))
        self.assertEqual("", self.policy.field("Reason:\n- Wiki: not applicable", "Reason"))

    # -- regression tests -----------------------------------------------------

    def bug_fix(self, kind, detail, ticked=True):
        body = self.body().replace("Regression test: not a bug fix", f"Regression test: {kind}").replace(
            "This adds a new operator option; nothing that worked before was broken.", detail)
        box = "x" if ticked else " "
        return body.replace("## Verification and test results",
                            f"## Type of change\n- [{box}] Bug fix\n- [ ] User facing feature\n## Verification and test results")

    def test_missing_regression_field_rejected(self):
        body = self.body().replace("Regression test: not a bug fix\n", "")
        self.assertTrue(any("Regression test: must be" in e for e in self.policy.check_pr(body, ["docs/README.md"])))

    def test_a_bug_fix_cannot_claim_not_a_bug_fix(self):
        body = self.bug_fix("not a bug fix", "It only looks like a bug fix from the outside, honestly.")
        self.assertTrue(any("marked Bug fix" in e for e in self.policy.check_pr(body, ["docs/README.md"])))

    def test_not_a_bug_fix_passes_when_bug_fix_is_not_ticked(self):
        body = self.bug_fix("not a bug fix", "A new option; it changes nothing that already worked.", ticked=False)
        self.assertEqual([], self.policy.check_pr(body, ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"]))

    def test_added_regression_test_must_name_the_commit_it_fails_on(self):
        files = ["print_handy/settings.py", "tests/test_settings.py", "docs/CONFIGURATION-YAML.md"]
        vague = self.bug_fix("added", "tests/test_settings.py::test_option_default pins the fix.")
        self.assertTrue(any("commit it fails on" in e for e in self.policy.check_pr(vague, files)))
        proven = self.bug_fix("added", "tests/test_settings.py::test_option_default fails on 1a2b3c4d and passes on this head.")
        self.assertEqual([], self.policy.check_pr(proven, files))

    def test_added_regression_test_needs_a_changed_test_file(self):
        body = self.bug_fix("added", "tests/test_settings.py::test_option_default fails on 1a2b3c4d and passes on this head.")
        self.assertTrue(any("no test file changed" in e for e in self.policy.check_pr(body, ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"])))

    def test_not_possible_needs_the_tracking_issue(self):
        files = ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"]
        unlinked = self.bug_fix("not possible", "Only the stock firmware on a real AD5X shows which bay feeds.")
        self.assertTrue(any("link the issue" in e for e in self.policy.check_pr(unlinked, files)))
        linked = self.bug_fix("not possible", "Only the stock firmware on a real AD5X shows which bay feeds; live check in #1082.")
        self.assertEqual([], self.policy.check_pr(linked, files))

    # -- issue closure ----------------------------------------------------------

    def closing_body(self, check="#1145: all eleven items read against main; each is ticked on the issue with its PR."):
        return self.body().replace("## Surfaces and variables",
                                   f"## Related issues\n- Fixes: #1145\n\nClosure check: {check}\n## Surfaces and variables")

    def test_open_items_skip_ticked_moved_and_form_checkboxes(self):
        body = """## Checklist
- [x] Done in #10
- [ ] Still open
  - [ ] Nested and open
- [ ] Bigger part, moved to #1160
- [ ] Hardware proof deferred to https://github.com/o/r/issues/1082
* [ ] Starred and open
<!-- - [ ] commented out -->
```
- [ ] in a code block
```
### Before submitting
- [ ] I searched open and closed issues for this problem.
### Hardware checks you ran and that passed
- [ ] The printer homed.
"""
        self.assertEqual(["Still open", "Nested and open", "Starred and open"], self.policy.open_items(body))

    def test_form_checkbox_sections_cover_every_issue_form(self):
        import re
        forms = Path(__file__).resolve().parents[1] / ".github/ISSUE_TEMPLATE"
        labels = set()
        for form in forms.glob("*.yml"):
            text = form.read_text(encoding="utf-8")
            labels |= {m.strip().casefold() for m in re.findall(
                r"- type: checkboxes\n(?:[ \t]+.*\n)*?[ \t]+label: (.+)", text)}
        self.assertTrue(labels)
        self.assertEqual(set(), labels - self.policy.FORM_CHECKBOX_SECTIONS)

    def test_closing_an_issue_with_open_items_fails_and_names_them(self):
        closing = [{"number": 1145, "body": "- [x] Uploads\n- [ ] Timed-out web chat press can still act later"}]
        errors = self.policy.check_pr(self.closing_body(), ["docs/README.md"], closing)
        self.assertTrue(any("Closes #1145" in e and "Timed-out web chat press" in e for e in errors), errors)

    def test_closing_a_finished_issue_passes(self):
        closing = [{"number": 1145, "body": "- [x] Uploads\n- [x] Web chat press\n- [ ] Host binding, moved to #1192"}]
        self.assertEqual([], self.policy.check_pr(self.closing_body(), ["print_handy/settings.py", "docs/CONFIGURATION-YAML.md"], closing))

    def test_closing_needs_the_closure_check_field(self):
        closing = [{"number": 1145, "body": "- [x] Uploads"}]
        errors = self.policy.check_pr(self.closing_body(check=""), ["docs/README.md"], closing)
        self.assertTrue(any(e.startswith("Closure check:") for e in errors), errors)

    def test_closing_nothing_needs_no_closure_check(self):
        self.assertEqual([], self.policy.check_closing(self.body(), []))

    def test_issue_closure_script_exit_status(self):
        import io, json, contextlib
        spec = importlib.util.spec_from_file_location(
            "closure", Path(__file__).resolve().parents[1] / "scripts/check_issue_closure.py")
        closure = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(closure)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "issue.json"
            for body, status in (("- [x] a\n- [ ] b", 1), ("- [x] a\n- [ ] b, tracked in #9", 0)):
                path.write_text(json.dumps({"body": body}), encoding="utf-8")
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(status, closure.main(["--metadata", str(path)]))
            self.assertIn("0 unticked", out.getvalue())
            # A broken run exits 2, never 1: the workflow reopens the issue only on 1.
            path.write_text("not json", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, closure.main(["--metadata", str(path)]))
                self.assertEqual(2, closure.main(["--metadata", str(Path(directory) / "missing.json")]))

if __name__ == "__main__":
    unittest.main()
