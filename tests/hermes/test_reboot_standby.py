"""Offline dispatch regression: standby is idle, active/unknown states fail closed."""
import ast
import asyncio
import logging
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as S
from unittest.mock import AsyncMock, Mock, patch

import print_handy.runtime as hermes_pkg


class RebootStandbyTests(unittest.TestCase):
    def test_standby_offers_confirmation_but_active_states_do_not(self):
        from handler_source import handler_source  # the handlers moved into print_handy/bot/handlers (#1323)
        tree = ast.parse(handler_source())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'print_reboot_dispatch')
        for state, klippy, allowed in [('standby', 'ready', True), ('printing', 'ready', False),
                                      ('paused', 'ready', False), ('unknown', 'ready', False),
                                      ('standby', 'startup', False)]:
            with self.subTest(state=state, klippy=klippy):
                adapter = S(send=AsyncMock())
                source = S(chat_id='c', thread_id=None)
                pc = S(status=Mock(return_value={'state': state, 'klippy': klippy}),
                       force_reboot=Mock(), REBOOT_LABEL='yes', CANCEL_REBOOT_LABEL='no')
                confirm = AsyncMock(return_value='no')
                ns = {'asyncio': asyncio, 'time': time, '_seen': {}, 'logger': logging.getLogger('t'),
                      '_control_preamble': lambda *a: (source, adapter), '_confirm': confirm}
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<dispatch>', 'exec'), ns)
                with patch.object(hermes_pkg, 'print_control', pc):
                    asyncio.run(ns['print_reboot_dispatch'](
                        S(), S(_session_key_for_source=lambda s: 'session-key')))
                self.assertEqual(confirm.await_count, int(allowed))
                pc.force_reboot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
