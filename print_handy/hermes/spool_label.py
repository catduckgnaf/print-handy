"""Photo spool intake: read a spool label photo into a spool spec (#514).

The conversational new-spool walkthrough (#512) can start from a photo of
the spool's box or manufacturer sticker instead of button picks. Three
extraction routes read the photo, in a documented precedence order (see
``extract_spool_spec_any``): the label's own barcode/QR code
(spool_codes, deterministic and offline), the configured vision endpoint
through the shared vision call (visual_watch.visual_extract: image bytes
+ a JSON-only prompt), and local OCR (tesseract) whose text is matched
against the constrained vocabulary labels actually use — known filament
types, materials, colours and brands — because a spool label only has so
many variables. This module owns the vision prompt, the validation every
route's answer goes through, the OCR text matching, and the route chain.

The extractors are pure and offline-testable: image bytes plus an
injected call in, a validated spec dict out — or a SpoolLabelError naming
why the photo could not be used (unreadable, non-JSON answer, or a
required field missing). The walkthrough refuses with that message and
falls back to the manual steps; nothing is ever created from a photo
without the operator confirming the extracted values first.

Label temperatures are data, not authority: they ride the spec as
``nozzle_temp`` / ``bed_temp`` and are recorded on the Spoolman filament
the spool is created with (spool_sync.create_spool), so the existing
temperature precedence holds for every material — a temperature recorded
on the filament wins over material defaults, and the #512 material
variant adjustments never override recorded data. No material is
special-cased here.

Nothing here stores or forwards the image anywhere: the bytes go to the
one extraction route that reads them (a local decoder, a local OCR
binary, or the configured vision endpoint) and nowhere else.
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
from typing import Any, Callable

logger = logging.getLogger(__name__)

LABEL_PROMPT = (
    "You are reading a photo of a 3D printer filament spool's box or manufacturer label. "
    "Reply with a single JSON object and nothing else, with these keys: "
    '"brand" (the maker\'s name, or null), '
    '"material" (the filament material including any sub-type, e.g. "PLA", "Silk PLA", "PLA+", "PETG"), '
    '"color" (the colour name or multi-colour blend exactly as labelled), '
    '"grams" (the net filament weight in grams as a number), '
    '"nozzle_temp" (the recommended nozzle temperature in °C as a number, or null when not printed), '
    '"bed_temp" (the recommended bed temperature in °C as a number, or null when not printed). '
    "Do not guess: use null for anything the label does not print, and never invent a weight. "
    'If the photo does not show a readable filament label at all, reply {"unreadable": true}.'
)

# The named extraction failures (#514): each carries the plain-language
# message the walkthrough shows before falling back to the manual steps.
FAILURE_MESSAGES = {
    "unreadable": ("I couldn't read that label photo. Try a closer, well-lit shot of the "
                   "spool's box or sticker — or just pick the details by hand below."),
    "non_json": ("The vision answer for that photo wasn't usable, so nothing was read from it. "
                 "You can send another photo, or pick the details by hand below."),
    "missing_fields": ("That label photo didn't show everything a new spool needs ({missing}). "
                       "You can send a clearer photo, or pick the details by hand below."),
}


class SpoolLabelError(Exception):
    """A named label-extraction failure; ``reason`` is one of FAILURE_MESSAGES' keys."""

    def __init__(self, reason: str, missing: tuple[str, ...] = ()):
        self.reason = reason
        self.missing = missing
        message = FAILURE_MESSAGES[reason]
        if missing:
            message = message.format(missing=", ".join(missing))
        super().__init__(message)


def _strip_fence(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    return raw.strip()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:100]


def _numbers(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    return [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]


def _grams(value: Any) -> float | None:
    """Net weight in grams from a number, or a string like '1000', '1000 g', '1 kg'."""
    numbers = _numbers(value)
    if not numbers:
        return None
    grams = numbers[0]
    if isinstance(value, str) and re.search(r"\bkg\b", value, re.I) and grams < 50:
        grams *= 1000
    return round(grams, 1) if grams > 0 else None


def _temp(value: Any, low: float, high: float) -> float | None:
    """One temperature in °C; a printed range ('215-230') reads as its midpoint.

    Out-of-range values are dropped, not refused: the temperatures are
    optional label data, and a misread digit must not sink the whole spec.
    """
    numbers = _numbers(value)
    if not numbers:
        return None
    temp = (numbers[0] + numbers[1]) / 2 if len(numbers) > 1 else numbers[0]
    temp = round(temp, 1)
    return temp if low <= temp <= high else None


def _recorded_temp(value: Any) -> float | None:
    """One label temperature kept only when the downstream contract would
    honour it: a temperature recorded on a Spoolman filament is used only
    when 0 < t <= 300 °C (printer_state.resolve_material_temperatures
    raises for anything else), so a label value outside that contract is
    dropped here instead of being persisted to fail there (P2-199)."""
    temp = _temp(value, 0, 300)
    return temp if temp else None


def validate_label_answer(answer: Any) -> dict[str, Any]:
    """Validate one extraction route's answer into a spec dict.

    Accepts the answer as a parsed JSON object or as a JSON string (fences
    stripped). This is the one validation every route's data passes through
    — the vision endpoint's answer and a barcode/QR payload alike — so a
    code's payload is data to confirm, never authority on its own. Raises
    SpoolLabelError naming the failure.
    """
    if isinstance(answer, str):
        try:
            answer = json.loads(_strip_fence(answer))
        except ValueError:
            raise SpoolLabelError("non_json") from None
    if not isinstance(answer, dict):
        raise SpoolLabelError("non_json")
    if answer.get("unreadable"):
        raise SpoolLabelError("unreadable")
    material = _clean_text(answer.get("material"))
    color = _clean_text(answer.get("color"))
    grams = _grams(answer.get("grams"))
    missing = tuple(name for name, value in
                    (("material", material), ("colour", color), ("weight", grams)) if not value)
    if missing:
        raise SpoolLabelError("missing_fields", missing)
    brand = _clean_text(answer.get("brand")) or None
    return {"brand": brand, "material": material, "color": color, "grams": grams,
            "nozzle_temp": _recorded_temp(answer.get("nozzle_temp")),
            "bed_temp": _recorded_temp(answer.get("bed_temp"))}


def extract_spool_spec(image: bytes, vision_call: Callable[[bytes, str], Any]) -> dict[str, Any]:
    """Read one spool label photo into a validated spec dict (vision route).

    ``vision_call`` is the shared vision call (visual_watch.visual_extract's
    shape): image bytes + prompt, returning the endpoint's parsed JSON, a
    raw string answer, or None when the call could not be completed. The
    returned spec is {brand, material, color, grams, nozzle_temp, bed_temp}
    with brand and the temperatures None when the label does not print
    them. Raises SpoolLabelError with a named reason when the photo cannot
    be used; the caller refuses with its message and falls back to the
    manual walkthrough.
    """
    if not image:
        raise SpoolLabelError("unreadable")
    try:
        answer = vision_call(image, LABEL_PROMPT)
    except Exception:
        raise SpoolLabelError("unreadable") from None
    if answer is None:
        raise SpoolLabelError("unreadable")
    return validate_label_answer(answer)


# ---------------------------------------------------------------------------
# Label text matching (the OCR route, and text payloads from codes)
#
# A spool label only has so many variables, so OCR output does not need
# general understanding: it needs the known filament types first (a type
# already in Spoolman is a wholesale match), then the material / colour /
# brand vocabularies labels are printed from, a weight, and labelled
# temperatures. Matching is phrase-based on the casefolded text, with a
# squashed fallback (all punctuation removed) for compact print and OCR
# spacing noise.

TEXT_MATERIALS = ("Silk PLA", "PLA Plus", "PLA+", "Matte PLA", "PLA-CF", "PETG-CF", "PA-CF",
                  "PA-GF", "PETG", "TPU", "ABS", "ASA", "PLA", "PA", "PC", "HIPS", "PP", "PVA")
_MATERIAL_ALIASES = {"pla plus": "PLA+"}
TEXT_COLORS = ("Rose Red", "Matte White", "Matte Black", "Blue Green", "Fire Engine Red",
               "Black", "White", "Grey", "Gray", "Red", "Blue", "Green", "Orange", "Yellow",
               "Pink", "Purple", "Brown", "Gold", "Silver", "Copper", "Beige", "Natural",
               "Transparent", "Clear", "Teal", "Cyan", "Magenta", "Rainbow", "Galaxy", "Ivory")
TEXT_BRANDS = ("Bambu Lab", "Prusament", "Polymaker", "MatterHackers", "FlashForge", "Creality",
               "Overture", "Hatchbox", "Amolen", "Eryone", "Giantarm", "Geeetech", "Elegoo",
               "Sunlu", "eSun", "Inland", "Anycubic", "Voxelab", "Tinmorry", "Comgrow")


def _phrase_span(phrase: str, low: str, *, squashed: bool = True) -> tuple[int, int] | None:
    """Where ``phrase`` appears in casefolded text: a word-boundary match
    (whose span indexes the original text), or — only when ``squashed`` —
    a punctuation-free substring match, whose span indexes the squashed
    text and so proves presence only, never position."""
    match = re.search(r"(?<![a-z0-9])" + re.escape(phrase.casefold()) + r"(?![a-z0-9+])", low)
    if match:
        return match.start(), match.end()
    if not squashed:
        return None
    flat = re.sub(r"[^a-z0-9+]+", "", low)
    needle = re.sub(r"[^a-z0-9+]+", "", phrase.casefold())
    index = flat.find(needle) if needle else -1
    return (index, index + len(needle)) if index >= 0 else None


def _match_vocabulary(vocabulary: tuple[str, ...], low: str) -> str | None:
    """The longest vocabulary phrase present in the text, canonical spelling.

    Complete-token matches are considered first: the squashed fallback
    tolerates OCR spacing, but a phrase found only inside a longer word
    ('pla' in 'plate') must never outrank a real token printed on the
    label (P2-202). The squashed pass runs only when no token matched.
    """
    for squashed in (False, True):
        best = None
        for phrase in vocabulary:
            if _phrase_span(phrase, low, squashed=squashed) is not None \
                    and (best is None or len(phrase) > len(best)):
                best = phrase
        if best is not None:
            return _MATERIAL_ALIASES.get(best.casefold(), best)
    return None


def _match_color(low: str, original: str, known_colors: list[str]) -> str | None:
    """A colour from the label text: known colours for the material first,
    then the colour vocabulary. Adjacent matches (a multi-colour blend,
    'Rose Red Blue Green') join into the blend exactly as printed; only
    boundary spans are used, so the slice always matches the real text."""
    for vocabulary in (known_colors, list(TEXT_COLORS)):
        hits = []
        for phrase in vocabulary:
            span = _phrase_span(phrase, low, squashed=False)
            if span is not None:
                hits.append(span)
        if not hits:
            continue
        hits.sort()
        runs = [[hits[0]]]
        for start, end in hits[1:]:
            run = runs[-1]
            run_end = max(e for _, e in run)
            if start <= run_end + 1:  # overlapping or one space apart: same blend
                run.append((start, end))
            else:
                runs.append([(start, end)])
        run = max(runs, key=lambda r: max(e for _, e in r) - r[0][0])
        color = _clean_text(original[run[0][0]:max(e for _, e in run)])
        if color:
            return color
    return None


def _text_grams(low: str) -> float | None:
    candidates = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(kg|g)\b", low):
        value = float(match.group(1)) * (1000 if match.group(2) == "kg" else 1)
        if 50 <= value <= 10000:
            candidates.append(value)
    if candidates:
        plausible = [v for v in candidates if v <= 5000]
        return round(max(plausible or candidates), 1)
    match = re.search(r"net\s*(?:wt|weight)?[^0-9]{0,10}(\d{3,5})\b", low)
    return float(match.group(1)) if match else None


def _text_temp(low: str, words: str) -> float | None:
    match = re.search(words + r"[^0-9]{0,25}(\d{2,3})\s*(?:[-–~]\s*(\d{2,3}))?", low)
    if not match:
        return None
    value: Any = match.group(1) if not match.group(2) else f"{match.group(1)}-{match.group(2)}"
    return _temp(value, 0, 400)


def spec_from_label_text(text: Any, known_types: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Read OCR text (or a text code payload) into a validated spec dict.

    ``known_types`` is the walkthrough's Spoolman filament types
    ({brand, material, color}): a type whose material and colour both
    appear is a wholesale match, and its brands/colours join the
    vocabularies. Raises SpoolLabelError: 'unreadable' when nothing at all
    can be found, 'missing_fields' naming what a readable label lacked.
    """
    original = str(text or "")
    if not original.strip():
        raise SpoolLabelError("unreadable")
    low = original.casefold()
    known = [t for t in (known_types or []) if isinstance(t, dict)]
    # The brand printed on the label is matched first (P2-203): when
    # several known types share a material and colour, the wholesale pick
    # below must prefer the type whose brand the label actually names,
    # and must never lend a conflicting brand to a type the label did
    # not print — the created spool reuses that type's filament record.
    known_brands = [str(t.get("brand") or "").strip() for t in known if t.get("brand")]
    label_brand = _match_vocabulary(tuple(dict.fromkeys([*known_brands, *TEXT_BRANDS])), low)

    def _brand_rank(entry):
        entry_brand = str(entry.get("brand") or "").strip()
        return 0 if entry_brand and label_brand and entry_brand.casefold() == label_brand.casefold() else 1

    brand = material = color = None
    for squashed in (False, True):
        for entry in sorted(known, key=_brand_rank):
            entry_brand = str(entry.get("brand") or "").strip()
            if entry_brand and label_brand and entry_brand.casefold() != label_brand.casefold():
                continue  # the label names a different maker: not this type
            entry_material = str(entry.get("material") or "").strip()
            entry_color = str(entry.get("color") or "").strip()
            if (entry_material and entry_color
                    and _phrase_span(entry_material, low, squashed=squashed) is not None
                    and _phrase_span(entry_color, low, squashed=squashed) is not None):
                brand = entry_brand or None
                material, color = entry_material, entry_color
                break
        if material is not None:
            break
    if material is None:
        material = _match_vocabulary(TEXT_MATERIALS, low)
    if brand is None:
        brand = label_brand
    if color is None:
        known_colors = [str(t.get("color") or "").strip() for t in known
                        if t.get("color") and (not material
                                               or str(t.get("material") or "").casefold() == material.casefold())]
        color = _match_color(low, original, known_colors)
    grams = _text_grams(low)
    if material is None and color is None and grams is None and brand is None:
        raise SpoolLabelError("unreadable")
    missing = tuple(name for name, value in
                    (("material", material), ("colour", color), ("weight", grams)) if not value)
    if missing:
        raise SpoolLabelError("missing_fields", missing)
    nozzle = _text_temp(low, r"(?:nozzle|extruder|hotend|print(?:ing)?\s*temp(?:erature)?)")
    bed = _text_temp(low, r"(?:heated\s*bed|bed|platform|build\s*plate)")
    return {"brand": brand, "material": material, "color": color, "grams": grams,
            "nozzle_temp": _recorded_temp(nozzle) if nozzle is not None else None,
            "bed_temp": _recorded_temp(bed) if bed is not None else None}


# ---------------------------------------------------------------------------
# Local OCR route (tesseract), and the route chain

def tesseract_available() -> bool:
    """Whether the local OCR route can run: a tesseract binary on PATH.

    The shipped container installs tesseract-ocr; anywhere else the route
    is simply unavailable and the photo step relies on the other routes.
    No Python OCR dependency is needed — the binary is driven directly.
    """
    return shutil.which("tesseract") is not None


def _tesseract_text(image: bytes) -> str:
    """OCR one image with the tesseract binary: greyscale, upscaled when small."""
    from PIL import Image

    with Image.open(io.BytesIO(image)) as im:
        gray = im.convert("L")
        width, height = gray.size
        if max(width, height) < 1200:
            scale = 1200 / max(width, height)
            gray = gray.resize((round(width * scale), round(height * scale)))
        buffer = io.BytesIO()
        gray.save(buffer, format="PNG")
    proc = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6"],
                          input=buffer.getvalue(), capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise SpoolLabelError("unreadable")
    return proc.stdout.decode("utf-8", "replace")


def extract_via_ocr(image: bytes, known_types: list[dict[str, Any]] | None = None,
                    text_fn: Callable[[bytes], str] | None = None) -> dict[str, Any]:
    """Read one spool label photo with local OCR (tesseract) into a spec.

    ``text_fn`` injects the OCR step in tests; by default it is the
    tesseract binary, and without one the route is unreadable — the chain
    only offers it when ``tesseract_available()`` anyway.
    """
    if not image:
        raise SpoolLabelError("unreadable")
    fn = text_fn or (_tesseract_text if tesseract_available() else None)
    if fn is None:
        raise SpoolLabelError("unreadable")
    try:
        text = fn(image)
    except SpoolLabelError:
        raise
    except Exception:
        raise SpoolLabelError("unreadable") from None
    return spec_from_label_text(text, known_types)


def extract_spool_spec_any(image: bytes,
                           routes: list[tuple[str, Callable[[bytes], dict[str, Any]]]]
                           ) -> tuple[dict[str, Any], str]:
    """Try extraction routes in precedence order; return (spec, route name).

    The caller's order is the contract — the click layer passes: the
    label's barcode/QR code (deterministic data the manufacturer printed),
    then the vision endpoint (the richest reader), then local OCR (the
    offline fallback). The first route that yields a valid spec wins;
    partial results are never merged across routes. When every route
    fails, the failure the operator hears is a 'missing_fields' one when
    any route produced it (a route read the label but incompletely — the
    most actionable answer), else the first route's failure.
    """
    failures: list[SpoolLabelError] = []
    for name, route in routes:
        try:
            return route(image), name
        except SpoolLabelError as exc:
            failures.append(exc)
        except Exception:
            logger.warning("label extraction route %s failed unexpectedly", name, exc_info=True)
            failures.append(SpoolLabelError("unreadable"))
    for exc in failures:
        if exc.reason == "missing_fields":
            raise exc
    if failures:
        raise failures[0]
    raise SpoolLabelError("unreadable")
