"""An explicit, JSON-only error protocol; never import a reported exception type."""
import traceback

from .. import diagnostics
from .slice_failure import SliceFailure
from .slice_refusal import SliceRefusal


# The tail of a traceback holds the failing frame; keep the reply line bounded.
TRACEBACK_LIMIT = 8000


class RemoteError(RuntimeError):
    def __init__(self, reply):
        super().__init__(reply.get('error') or 'worker failed without an explanation')
        self.worker_error_type = reply.get('type')
        self.diagnostic_category = reply.get('category', 'unknown')
        self.worker_traceback = reply.get('traceback')


def encode(exc):
    reply = {'ok': False, 'type': type(exc).__name__, 'error': str(exc),
             'category': diagnostics.classify(exc).value}
    if isinstance(exc, SliceRefusal):
        reply['refusal'] = exc.as_dict()
    elif isinstance(exc, SliceFailure):
        reply['slice_failure'] = exc.as_dict()
    else:
        # An unexplained error crossed a process boundary. Without the child's
        # traceback the parent log shows only the message (for example
        # "'NoneType' object has no attribute 'group'") and the failing line is
        # lost. It is for the operator log only and never reaches chat.
        text = ''.join(traceback.format_exception(exc))
        reply['traceback'] = text[-TRACEBACK_LIMIT:]
    return reply


def decode(reply):
    if isinstance(reply.get('refusal'), dict):
        from .slice_refusal import from_dict
        return from_dict(reply['refusal'])
    if isinstance(reply.get('slice_failure'), dict):
        return SliceFailure.from_dict(reply['slice_failure'])
    # Keep the existing public behavior of these two worker errors.
    cls = {'ValueError': ValueError, 'FileNotFoundError': FileNotFoundError}.get(reply.get('type'))
    return cls(reply.get('error')) if cls else RemoteError(reply)
