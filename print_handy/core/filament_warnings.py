"""The structured filament-warning outcome of a slice gate.

Owner ruling (catduck, 2026-11-10): "You should never block a print for
that" and "I actually really don't want anything filament to block a print,
allow the user to accept the warning." Filament conditions are therefore
warnings the operator can accept, never hard blocks. When a slice gate
finds filament warnings and no recorded acceptance covers them, it raises
FilamentWarningPending: the slice does not proceed, the warnings ride on
the job as ``filament_warnings_pending``, and a surface (the chat Ready
flow, the Discord color flow, the CLI) presents them and records the
operator's explicit acceptance — bound to the exact warning texts, the
source hash and the selected plates — before the slice resumes.

The exception crosses the heavy-worker process boundary through
worker_errors, following the SliceFailure as_dict/from_dict pattern: an
explicit JSON-only protocol, never a pickled exception type.
"""


class FilamentWarningPending(RuntimeError):
    """Filament warnings await the operator's explicit acceptance."""

    def __init__(self, warnings, source_sha256=None, selected_plates=None):
        self.warnings = [str(w) for w in warnings]
        self.source_sha256 = source_sha256
        self.selected_plates = [str(p) for p in selected_plates or []]
        super().__init__('filament warnings need your acceptance before slicing: '
                         + '; '.join(self.warnings))

    def as_dict(self):
        return {'warnings': list(self.warnings),
                'source_sha256': self.source_sha256,
                'selected_plates': list(self.selected_plates)}

    @classmethod
    def from_dict(cls, data):
        return cls(list(data.get('warnings') or []),
                   source_sha256=data.get('source_sha256'),
                   selected_plates=list(data.get('selected_plates') or []))
