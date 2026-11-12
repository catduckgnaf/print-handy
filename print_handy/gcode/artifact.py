"""A G-code file read once: its bytes, its text and its digest (#1323).

``file_sha256`` and ``_text_like_read_text`` moved verbatim from
``print_handy.core.model_job``; the old names are still importable from
there for one release.
"""
import contextvars, hashlib
from pathlib import Path


def file_sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source,"sha256").hexdigest()


def _text_like_read_text(raw, errors=None):
    """``raw`` decoded exactly as ``Path.read_text(errors=errors)`` decodes the file holding it.

    Same default encoding and the same universal-newline translation, so a
    caller that already read the bytes gets the identical text without a
    second read.
    """
    import io
    with io.TextIOWrapper(io.BytesIO(raw), encoding=io.text_encoding(None), errors=errors) as stream:
        return stream.read()


# The artifacts of the innermost GcodeArtifact.read_once call, keyed by
# absolute path; None outside one.
_READING = contextvars.ContextVar("print_handy_gcode_reading", default=None)


class GcodeArtifact:
    """One G-code file whose bytes are read from disk at most once.

    Verification asks one file several questions: its bytes for the ZMod
    checksum and the bounds cache, its text for the line checks, its digest
    for the verified-artifact marker. Each used to re-read the file, so the
    answers could describe different bytes if the file was replaced
    mid-check, and the marker could bind a digest of bytes nobody verified.
    Inside a ``read_once`` call every ``GcodeArtifact.of(path)`` for the same
    path returns one artifact, so every answer is about the same bytes.

    Outside ``read_once`` each ``of`` call is a fresh artifact, which reads
    the file when first asked, exactly as the old per-question reads did.
    Use ``read_once`` only around read-only checks: a step that rewrites the
    file inside it would see the bytes from before the rewrite.
    """

    __slots__ = ("path", "_raw", "_text")

    def __init__(self, path):
        self.path = Path(path)
        self._raw = None
        self._text = None

    @classmethod
    def of(cls, path):
        """The artifact for ``path`` (a path, or an artifact, returned as is)."""
        if isinstance(path, cls):
            return path
        open_now = _READING.get()
        if open_now is None:
            return cls(path)
        key = str(Path(path).absolute())
        artifact = open_now.get(key)
        if artifact is None:
            artifact = open_now[key] = cls(path)
        return artifact

    @staticmethod
    def read_once(func):
        """Decorator: within one call of ``func`` each G-code file is read once.

        Nested calls share the outermost call's artifacts.
        """
        import functools

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if _READING.get() is not None:
                return func(*args, **kwargs)
            token = _READING.set({})
            try:
                return func(*args, **kwargs)
            finally:
                _READING.reset(token)
        return wrapper

    @property
    def raw(self):
        """The file's bytes. A failed read (OSError) is raised and not remembered."""
        if self._raw is None:
            self._raw = self.path.read_bytes()
        return self._raw

    @property
    def text(self):
        """The bytes decoded as UTF-8, undecodable bytes dropped (``raw.decode(errors="ignore")``)."""
        if self._text is None:
            self._text = self.raw.decode(errors="ignore")
        return self._text

    def read_text(self, errors=None):
        """The bytes decoded exactly as ``Path.read_text(errors=errors)`` would decode the file."""
        return _text_like_read_text(self.raw, errors)

    @property
    def sha256(self):
        """Hex SHA-256 of the bytes, the value ``file_sha256`` returns for the file."""
        return hashlib.sha256(self.raw).hexdigest()
