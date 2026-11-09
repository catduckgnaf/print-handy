"""First-boot parity program, PR-8: the [webui] access-model keys are
Settings-surface complete.

require_auth, controls_enabled and cookie_secure were validated by the
settings loader and consumed by the access/controls/server code, but had
no catalog field, so the Settings page could not show or flip them (the
variables contract carried them as recorded exemptions). They are catalog
fields now; access_token_env stays deliberately non-editable (#286: no
*_env key is a catalog field) and keeps its exemption.

The Unraid template surface for these keys is out of scope here: PR #1108
owns docker/unraid/print-handy.xml (recorded in
tests/test_variables_contract.py next to the exemption it relates to).
"""
import tempfile
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import field_by_id, read_fields, write_changes

NEW_FIELD_IDS = ("webui.require_auth", "webui.controls_enabled", "webui.cookie_secure")


class AccessCatalogFieldTests(unittest.TestCase):
    def test_the_three_access_keys_are_catalog_fields_in_the_webui_group(self):
        for field_id in NEW_FIELD_IDS:
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.kind, "toggle")
                self.assertEqual(field.group, "webui")

    def test_access_token_env_stays_out_of_the_catalog(self):
        self.assertIsNone(field_by_id("webui.access_token_env"))

    def test_the_fields_read_the_loader_defaults(self):
        values = {item.id: item.value for item in read_fields(environ={})}
        self.assertIs(values["webui.require_auth"], True)
        self.assertIs(values["webui.controls_enabled"], False)
        self.assertIs(values["webui.cookie_secure"], False)

    def test_the_fields_write_back_through_the_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "local.yaml"
            local.write_text("webui:\n  enabled: true\n  host: 0.0.0.0\n  port: 8088\n")
            environ = {"PRINT_HANDY_CONFIG": str(local)}
            write_changes({"webui.require_auth": False,
                           "webui.controls_enabled": True,
                           "webui.cookie_secure": True}, environ=environ)
            data = settings.load(local=local, environ={})
            self.assertIs(data["webui"]["require_auth"], False)
            self.assertIs(data["webui"]["controls_enabled"], True)
            self.assertIs(data["webui"]["cookie_secure"], True)


if __name__ == "__main__":
    unittest.main()
