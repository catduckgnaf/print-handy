"""The edit chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""

from .common import _EditHost


async def edit_dispatch(event, gateway, **kwargs):
    from print_handy.bot import edit_chat
    return await edit_chat.edit_dispatch(event, gateway, host=_EditHost(), **kwargs)


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = edit_dispatch
