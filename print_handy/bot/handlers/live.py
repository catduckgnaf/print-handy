"""The live chat handler.

Its handler lives in ``common.py`` with the state it shares (#1323).
"""

from .common import live_dispatch
from ._gates import command_gate

match = command_gate('/print-live', True)
handle = live_dispatch
handle.dispatch_gate = match
