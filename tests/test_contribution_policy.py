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
                  .replace("Variables reason:\n", "Variables reason: This change adds or changes no variables at all.\n"))
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

if __name__ == "__main__":
    unittest.main()
