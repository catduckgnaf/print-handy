"""Barcode / QR decoding for spool label photos (#514).

The first extraction route for a label photo: the code the manufacturer
printed is deterministic data, readable offline with no endpoint and no
OCR guessing. Decoding is zxing-cpp (a small prebuilt wheel, imported
lazily — an install without it simply does not offer this route), fed
through Pillow, which Print Handy already depends on.

A payload is data, not authority: a JSON payload goes through the same
validation as a vision answer (spool_label.validate_label_answer), a
text payload through the same constrained label-text matching as OCR
(spool_label.spec_from_label_text), and either way the walkthrough
shows the result for confirmation before anything is created. A bare
product number (a plain EAN/UPC) carries no spool spec and fails like
any unreadable label, so the chain falls through to the next route.
"""
from __future__ import annotations

import io
from typing import Any, Callable

from . import spool_label
from .spool_label import SpoolLabelError


def codes_available() -> bool:
    """Whether the code route can run: the zxing-cpp wheel is importable."""
    try:
        import zxingcpp  # noqa: F401
        return True
    except Exception:
        return False


def decode_payloads(image: bytes, reader: Callable[[bytes], list[str]] | None = None) -> list[str]:
    """Every distinct barcode/QR payload in one photo, in reading order.

    ``reader`` injects the decoder in tests; by default the photo is
    decoded with Pillow and read with zxing-cpp (all formats it knows:
    QR, EAN/UPC, Code 128, Data Matrix, ...).
    """
    if reader is not None:
        return list(reader(image))
    import zxingcpp
    from PIL import Image

    with Image.open(io.BytesIO(image)) as im:
        results = zxingcpp.read_barcodes(im.convert("RGB"))
    payloads: list[str] = []
    for result in results:
        text = str(getattr(result, "text", "") or "")
        if text and text not in payloads:
            payloads.append(text)
    return payloads


def extract_via_codes(image: bytes, known_types: list[dict[str, Any]] | None = None,
                      reader: Callable[[bytes], list[str]] | None = None) -> dict[str, Any]:
    """Read one spool label photo's barcode/QR code into a spec dict.

    Each payload is tried as a JSON answer first, then as label text;
    the first payload that yields a valid spec wins. Raises
    SpoolLabelError when the photo holds no code, or no payload carries
    a usable spec — the route chain then falls through to the next route.
    """
    if not image:
        raise SpoolLabelError("unreadable")
    try:
        payloads = decode_payloads(image, reader)
    except SpoolLabelError:
        raise
    except Exception:
        raise SpoolLabelError("unreadable") from None
    if not payloads:
        raise SpoolLabelError("unreadable")
    failure: SpoolLabelError | None = None
    for payload in payloads:
        for attempt in (lambda: spool_label.validate_label_answer(payload),
                        lambda: spool_label.spec_from_label_text(payload, known_types)):
            try:
                return attempt()
            except SpoolLabelError as exc:
                failure = exc
    raise failure or SpoolLabelError("unreadable")
