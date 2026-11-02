"""Optional Web UI support (#286).

Stage 1 is the config core: the declarative field catalog
(:mod:`print_handy.webui.catalog`) and the provenance read / validated
write-back API over the YAML settings file
(:mod:`print_handy.webui.config`). Stage 2 adds the status server
(:mod:`print_handy.webui.status` for the snapshot,
:mod:`print_handy.webui.server` for the stdlib HTTP server and the
``python3 -m print_handy.webui`` entry point). Stage 3 adds the picker
configuration page (:mod:`print_handy.webui.pages`): ``GET /config``
renders the catalog as a form, and saving goes preview -> confirm ->
validated write through the stage 1 API.
"""
from __future__ import annotations

from .catalog import (CATALOG, KINDS, Field, choice_label, choices_for,
                      config_page_visible, field_by_id)
from .config import (DEFAULT, ENVIRONMENT, HOME_CONFIG, MODEL_DEFAULT, SETTINGS_FILE, USER_FILE,
                     FieldValue, WebUIConfigError, read_field, read_fields,
                     settings_file_path, write_changes)

__all__ = [
    "CATALOG", "KINDS", "Field", "choice_label", "choices_for",
    "config_page_visible", "field_by_id",
    "DEFAULT", "ENVIRONMENT", "HOME_CONFIG", "MODEL_DEFAULT", "SETTINGS_FILE", "USER_FILE",
    "FieldValue", "WebUIConfigError", "read_field", "read_fields",
    "settings_file_path", "write_changes",
]
