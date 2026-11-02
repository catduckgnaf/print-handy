"""Purchase spool intake: propose spools from a shop link or a screenshot (#801).

Extends the #514 photo label intake and the #512 new-spool walkthrough: when
an AI route is configured, a product/order link or a purchase screenshot
dropped in the printer channel earns one confirm-first offer — "Add spools
from this purchase?" — and nothing is ever created without the operator
confirming the exact list. Model links (MakerWorld, Printables, ...) are
never purchase offers; they keep the model intake's routing.

Two input routes produce the same proposed line items
({brand, material, color, hex, grams, quantity, price, nozzle_temp,
bed_temp, purchase_url}):

- **Link:** the page is fetched once, server side, with a short timeout and
  a size cap. Structured data is read first — JSON-LD ``Product`` and Open
  Graph — and when it is complete it resolves with NO AI call at all. Only
  incomplete structured data falls back to the trimmed page text through
  the configured AI route. A blocked or bot-walled page (HTTP 401/403/429,
  a captcha or sign-in wall) refuses plainly and asks for a screenshot.
- **Screenshot:** the vision endpoint with a purchase prompt (not the #514
  label prompt) returns the order's line items.

Pack variants ("4 x 1 kg, Black/White/Gray/Red") split into one line per
color. Validation is the existing spool validation, reused, not copied:
weights and temperatures go through spool_label's cleaners, and a material
must resolve through printer_state.resolve_material_temperatures — unknown
materials are refused per SETTINGS.md "Material labels", and PLA+ is the
PLA family with its +5 C variant adjustment exactly as #512 resolves it.

Creation reuses spool_sync.create_spool (filament matched by brand +
material + color, created only when new; spools land on the Shelf), once
per roll, with the per-roll price and the purchase URL recorded on the
spool. The propose functions are plain services — link text/HTML or image
bytes in, proposed line items out — so the Web UI spools page (#789) can
call them later; this module has no chat or Discord surface of its own.
"""
from __future__ import annotations

import html as _html
import json
import logging
import os
import re
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit

from ..core import link_intake, printer_state
from . import _config, spool_label, spool_sync

logger = logging.getLogger(__name__)

PURCHASE_PROMPT = (
    "You are reading a 3D printer filament purchase: a product page, cart, order confirmation "
    "or receipt, shown as an image or as page text. Reply with a single JSON object and "
    'nothing else: {"items": [...]} with one item per distinct filament product bought, each '
    'with these keys: "brand" (the maker\'s name, or null), '
    '"material" (the filament material including any sub-type, e.g. "PLA", "Silk PLA", "PLA+", "PETG"), '
    '"color" (the color name exactly as shown; for a multi-color pack of separate rolls list every color '
    'separated by "/", e.g. "Black/White/Gray/Red"; for ONE roll that is itself multicolor, '
    'give its colors the same way and add "multicolor": true), '
    '"color_hex" (the color as a hex code like "CD7F32" when shown, else null), '
    '"grams" (the net filament weight per roll in grams as a number), '
    '"quantity" (how many rolls of this item were bought), '
    '"price" (the price per roll as a number when shown, else null), '
    '"nozzle_temp" (the recommended nozzle temperature in C as a number, or null when not shown), '
    '"bed_temp" (the recommended bed temperature in C as a number, or null when not shown). '
    "Do not guess: use null for anything not shown, and never invent a weight or a price. "
    'If the purchase shows no filament at all, reply {"items": []}.'
)

OFFER_QUESTION = ("Add spools from this purchase? I will read it and show you the exact "
                  "spools before anything is created.")
OFFER_YES = "Yes — read it"
OFFER_NO = "No"
CONFIRM_CHOICES = ("Create all", "Edit", "Cancel")

FETCH_TIMEOUT_S = 8
MAX_PAGE_BYTES = 512 * 1024
MAX_AI_TEXT_CHARS = 6000
MAX_QUANTITY = 99

# Hosts/paths that mark a link as a shop or order page. A link that is none
# of these and carries no filament/purchase words is not a purchase offer.
_SHOP_HOST_PARTS = ("amazon.", "aliexpress.", "ebay.", "banggood.", "temu.",
                    "store.bambulab.", "shop.", "store.")
_SHOP_PATH_WORDS = ("/product", "/dp/", "/item/", "/cart", "/order", "/checkout",
                    "/buy", "filament")
_FILAMENT_WORDS = ("filament", "spool", "pla", "petg", "abs", "asa", "tpu",
                   "order", "receipt", "cart", "purchase", "bought")
_BLOCKED_MARKERS = ("captcha", "are you a robot", "robot check", "unusual traffic",
                    "verify you are human", "enter the characters you see",
                    "sign in to continue", "automated access")

FAILURE_MESSAGES = {
    "blocked": ("That shop page would not let me read it (shops often block automated "
                "reads). Send a screenshot of the product page, cart or order instead, "
                "and I will read that. Nothing was created."),
    "unreadable": ("I could not read any filament purchase from that. Send a clearer "
                   "screenshot or the product link, or add the spools by hand with "
                   "`/spool new`. Nothing was created."),
    "non_json": ("The AI answer for that purchase was not usable, so nothing was read "
                 "from it. You can try again, or add the spools by hand with "
                 "`/spool new`. Nothing was created."),
    "missing_fields": ("That purchase did not show everything a new spool needs "
                       "({missing}). Send a clearer screenshot or link, or add the "
                       "spools by hand with `/spool new`. Nothing was created."),
    "unknown_material": ("The material {material!r} is not a recognized filament material, "
                         "so I cannot create spools for it. Set the material to the family "
                         "it prints as, or record the filament in Spoolman first. "
                         "Nothing was created."),
    "fetch_failed": ("I could not fetch that link, so nothing was read from it. Send a "
                     "screenshot of the page instead, or add the spools by hand with "
                     "`/spool new`. Nothing was created."),
}


class PurchaseError(Exception):
    """A named purchase-intake refusal; ``reason`` is one of FAILURE_MESSAGES' keys."""

    def __init__(self, reason: str, missing: tuple[str, ...] = (), material: str = ""):
        self.reason = reason
        self.missing = missing
        message = FAILURE_MESSAGES[reason]
        if missing:
            message = message.format(missing=", ".join(missing))
        if material:
            message = message.format(material=material)
        super().__init__(message)


# ---------------------------------------------------------------------------
# The gate and recognition (pure; no network, no AI)

def ai_route_configured() -> bool:
    """True when a vision/AI route is configured: PRINT_VISION_URL, or
    services.vision_chat_url. The local failure detector is not a route —
    it cannot read a page or a screenshot — so it never opens this gate.
    No route, no offer, and nothing about the channel changes."""
    if os.environ.get("PRINT_VISION_URL"):
        return True
    try:
        return bool(_config.service_url("vision_chat_url"))
    except Exception:
        return False


def purchase_url(text: Any) -> str | None:
    """The shop/order link in a message, else None.

    A model link is never a purchase link (it keeps its existing routing),
    and neither is a bare link with no shop or filament signal.
    """
    text = str(text or "")
    if link_intake.model_link(text):
        return None
    urls = re.findall(r"https?://[^\s<>\)\]]+", text)
    if len(urls) != 1:
        return None
    url = urls[0].rstrip(".,;!?")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    low = (host + parts.path).casefold()
    words = text.casefold()
    if any(part in host for part in _SHOP_HOST_PARTS) \
            or any(word in low for word in _SHOP_PATH_WORDS) \
            or any(word in words for word in _FILAMENT_WORDS):
        return url
    return None


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_PURCHASE_WORDS = _FILAMENT_WORDS + ("screenshot", "invoice", "amazon", "aliexpress")


def looks_like_purchase(text: Any, *, has_image: bool) -> bool:
    """Whether a channel message earns the purchase offer at all.

    A shop/order link, or an image whose caption or (for a bare image) the
    message itself carries filament/purchase words. Recognition alone never
    creates anything and never calls the AI: the offer is a question.
    """
    if purchase_url(text) is not None:
        return True
    if not has_image:
        return False
    low = str(text or "").casefold()
    return any(word in low for word in _PURCHASE_WORDS)


def is_image_name(name: Any) -> bool:
    return str(name or "").lower().endswith(_IMAGE_SUFFIXES)


# ---------------------------------------------------------------------------
# Line-item validation (shared by the structured, AI-text and image routes)

def _price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        match = re.search(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))
        price = float(match.group(0)) if match else None
    return round(price, 2) if price is not None and price >= 0 else None


def _quantity(value: Any) -> int:
    try:
        qty = int(float(value))
    except (TypeError, ValueError):
        qty = 1
    return min(max(qty, 1), MAX_QUANTITY)


def _check_material(material: str) -> None:
    """The existing material validation: a label must resolve to a family
    with a temperature profile (printer_state), so PLA+ passes as PLA with
    its variant adjustment and unknown names are refused, never guessed."""
    try:
        printer_state.resolve_material_temperatures(material, {})
    except Exception:
        raise PurchaseError("unknown_material", material=material) from None


def _split_pack(line: dict[str, Any]) -> list[dict[str, Any]]:
    """Split a multi-color pack line into one line per color.

    A split needs explicit pack evidence: a ``colors`` list enumerating the
    pack's contents, or a stated quantity that covers every color in a
    "/" or "," separated color string ("4 x 1 kg, Black/White/Gray/Red",
    quantity 4). Slash-separated colors alone are NOT a pack: one roll of
    dual-color filament ("Red/Black", quantity 1) is a single multicolor
    roll — the existing spec_color path records its blend — so it stays one
    line with its color and quantity untouched, as does any line the AI
    flags ``multicolor``. The quantity divides across a pack's colors (the
    first colors take the remainder), and a pack-total price divides per
    roll, whether or not the pack splits by color.
    """
    price_is_total = bool(line.pop("price_is_total", False))
    multicolor = bool(line.pop("multicolor", False))
    listed = line.pop("colors", None)
    qty = _quantity(line.get("quantity"))
    if price_is_total and qty > 1 and line.get("price") is not None:
        line = {**line, "price": round(float(line["price"]) / qty, 2)}
    if multicolor:
        return [line]
    if isinstance(listed, list) and len(listed) >= 2:
        colors = [str(color).strip() for color in listed if str(color).strip()]
    elif isinstance(listed, list) and listed:
        line["color"] = str(listed[0]).strip()
        return [line]
    else:
        raw = str(line.get("color") or "")
        parts = [part.strip() for part in re.split(r"[/,]", raw) if part.strip()] \
            if ("/" in raw or "," in raw) else []
        # A color string naming several colors is a pack only when the
        # quantity covers them; below that it is one multicolor roll.
        colors = parts if len(parts) >= 2 and qty >= len(parts) else []
    if len(colors) < 2:
        return [line]
    if qty == 1:
        qty = len(colors)  # one pack of one roll per color ("4 x 1 kg, Black/White/Gray/Red")
    per, extra = divmod(qty, len(colors))
    out = []
    for index, color in enumerate(colors):
        share = per + (1 if index < extra else 0)
        if share <= 0:
            continue
        out.append({**line, "color": color, "quantity": share})
    return out


def validate_purchase_answer(answer: Any, *, purchase_url: str | None = None) -> list[dict[str, Any]]:
    """Validate one AI answer (parsed object or JSON string) into line items.

    Every line needs a recognized material, a color and a net weight; a
    line that lacks one refuses the whole purchase naming what is missing,
    exactly as the #514 label validation refuses rather than guessing.
    """
    if isinstance(answer, str):
        try:
            answer = json.loads(spool_label._strip_fence(answer))
        except ValueError:
            raise PurchaseError("non_json") from None
    if isinstance(answer, dict):
        items = answer.get("items", answer.get("lines"))
        if items is None and answer.get("material"):
            items = [answer]
    elif isinstance(answer, list):
        items = answer
    else:
        items = None
    if not isinstance(items, list):
        raise PurchaseError("non_json")
    if not items:
        raise PurchaseError("unreadable")
    lines: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise PurchaseError("non_json")
        line = {
            "brand": spool_label._clean_text(raw.get("brand")) or None,
            "material": spool_label._clean_text(raw.get("material")),
            "color": spool_label._clean_text(raw.get("color")),
            "hex": spool_sync.screen_hex(raw.get("color_hex") or raw.get("hex")),
            "grams": spool_label._grams(raw.get("grams") if raw.get("grams") is not None
                                        else raw.get("weight")),
            "quantity": _quantity(raw.get("quantity", raw.get("qty", 1))),
            "price": _price(raw.get("price")),
            "nozzle_temp": spool_label._nozzle_temp(raw.get("nozzle_temp")),
            "bed_temp": spool_label._bed_temp(raw.get("bed_temp")),
            "purchase_url": purchase_url,
        }
        if isinstance(raw.get("colors"), list):
            line["colors"] = raw["colors"]
        if raw.get("multicolor"):
            line["multicolor"] = True
        missing = tuple(name for name, value in
                        (("material", line["material"]), ("color", line["color"]),
                         ("weight", line["grams"])) if not value)
        if missing:
            raise PurchaseError("missing_fields", missing)
        _check_material(line["material"])
        lines.extend(_split_pack(line))
    return lines


# ---------------------------------------------------------------------------
# Structured page data (JSON-LD Product / Open Graph): no AI call

class _MetaParser:
    """The page's structured facts: JSON-LD blocks, Open Graph tags, title."""

    def __init__(self, page: str):
        self.json_ld: list[Any] = []
        for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                                page, re.S | re.I):
            try:
                self.json_ld.append(json.loads(_html.unescape(block.strip())))
            except ValueError:
                continue
        self.og: dict[str, str] = {}
        for tag in re.findall(r"<meta[^>]+>", page, re.I):
            prop = re.search(r'(?:property|name)=["\']([^"\']+)["\']', tag, re.I)
            content = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
            if prop and content:
                self.og[prop.group(1).lower()] = _html.unescape(content.group(1)).strip()
        title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        self.title = _html.unescape(title.group(1)).strip() if title else ""


def _products(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        kind = node.get("@type")
        if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
            found.append(node)
        for value in node.values():
            found.extend(_products(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_products(value))
    return found


def _offer_price(product: dict[str, Any]) -> float | None:
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict):
        return _price(offers.get("price"))
    return None


def _pack_count(name: str) -> int | None:
    """The roll count a product name states for a pack, else None.

    Structured product data rarely carries an explicit quantity, but pack
    names state it: "4 x 1 kg", "Pack of 4", "2-Pack", "4 rolls". The count
    matters before the data can count as complete — without it a 4-roll
    pack priced as a whole would resolve as one roll at the pack price.
    """
    text = str(name or "")
    match = re.search(r"(\d+)\s*[x×]\s*[\d.]+\s*(?:kg|g)\b", text, re.I) \
        or re.search(r"(?:pack|bundle|set)\s*(?:of\s*)?(\d+)", text, re.I) \
        or re.search(r"(\d+)\s*-?\s*(?:pack|rolls?|spools?)\b", text, re.I)
    if not match:
        return None
    count = int(match.group(1))
    return count if count > 1 else None


def _structured_lines(page: str, url: str) -> list[dict[str, Any]] | None:
    """Complete line items from the page's own structured data, else None.

    Complete means every product resolves — through the same label-text
    vocabulary matching #514's OCR route uses — to a recognized material,
    a color and a weight. Anything less returns None and the caller falls
    back to the AI route; partial structured data is never merged.
    """
    meta = _MetaParser(page)
    products = [p for block in meta.json_ld for p in _products(block)]
    lines: list[dict[str, Any]] = []
    for product in products:
        name = str(product.get("name") or "").strip()
        if not name:
            continue
        brand = product.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        try:
            spec = spool_label.spec_from_label_text(name)
        except spool_label.SpoolLabelError:
            return None
        qty = _quantity(product.get("quantity", 1))
        if qty == 1:
            pack = _pack_count(name)
            if pack:
                qty = _quantity(pack)
        line = {"brand": spool_label._clean_text(brand) or spec["brand"],
                "material": spec["material"], "color": spec["color"], "hex": None,
                "grams": spec["grams"], "quantity": qty,
                "price": _offer_price(product), "price_is_total": True,
                "nozzle_temp": spec["nozzle_temp"], "bed_temp": spec["bed_temp"],
                "purchase_url": url}
        try:
            _check_material(line["material"])
        except PurchaseError:
            return None
        lines.extend(_split_pack(line))
    if lines:
        return lines
    # Open Graph alone: a complete product title resolves the same way.
    title = meta.og.get("og:title") or meta.title
    if title:
        try:
            spec = spool_label.spec_from_label_text(title)
        except spool_label.SpoolLabelError:
            return None
        og_qty = _quantity(_pack_count(title) or 1)
        line = {"brand": spec["brand"], "material": spec["material"], "color": spec["color"],
                "hex": None, "grams": spec["grams"], "quantity": og_qty,
                "price": _price(meta.og.get("product:price:amount") or meta.og.get("og:price:amount")), "price_is_total": True,
                "nozzle_temp": spec["nozzle_temp"], "bed_temp": spec["bed_temp"],
                "purchase_url": url}
        try:
            _check_material(line["material"])
        except PurchaseError:
            return None
        return _split_pack(line)
    return None


def _page_text(page: str) -> str:
    """The page's readable text for the AI fallback: tags, scripts and
    styles stripped, whitespace collapsed, capped for the prompt."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()[:MAX_AI_TEXT_CHARS]


def _is_blocked(status: int, page: str, final_url: str) -> bool:
    if status in (401, 403, 429):
        return True
    low = page.casefold()
    if any(marker in low for marker in _BLOCKED_MARKERS):
        return True
    path = final_url.split("?")[0].rstrip("/").casefold()
    return "/ap/signin" in path or ("/login" in path and "product" not in low)


# A fetcher returns (status, final_url, body_text); injected in tests so no
# test touches the network.
Fetcher = Callable[[str], tuple[int, str, str]]
AiCall = Callable[..., Any]


def fetch_page(url: str) -> tuple[int, str, str]:
    """Fetch one page once, server side: short timeout, size cap."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PrintHandy/2)"})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as response:
            body = response.read(MAX_PAGE_BYTES + 1)
            return response.status, response.geturl(), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # a wall answers with an error status + body
        try:
            body = exc.read(MAX_PAGE_BYTES + 1)
        except Exception:
            body = b""
        return exc.code, exc.geturl() or url, body.decode("utf-8", "replace")


def propose_from_link(url: str, *, fetcher: Fetcher | None = None,
                      ai_call: AiCall | None = None) -> list[dict[str, Any]]:
    """Propose line items from a shop/order link (the #789 service shape).

    Structured data first (no AI call when complete); the trimmed page
    text goes to the AI route only when structured data is incomplete.
    A blocked page refuses asking for a screenshot.
    """
    fetcher = fetcher or fetch_page
    try:
        status, final_url, page = fetcher(url)
    except PurchaseError:
        raise
    except Exception as exc:
        logger.warning("purchase link fetch failed for %s: %s: %s", url, type(exc).__name__, exc)
        raise PurchaseError("fetch_failed") from None
    if _is_blocked(status, page, final_url):
        raise PurchaseError("blocked")
    if status >= 400:
        raise PurchaseError("fetch_failed")
    lines = _structured_lines(page, url)
    if lines is not None:
        return lines
    if ai_call is None:
        raise PurchaseError("unreadable")
    answer = ai_call(prompt=PURCHASE_PROMPT, text=_page_text(page))
    if answer is None:
        raise PurchaseError("unreadable")
    return validate_purchase_answer(answer, purchase_url=url)


def propose_from_image(image: bytes, *, ai_call: AiCall) -> list[dict[str, Any]]:
    """Propose line items from a purchase screenshot (the #789 service shape).

    The vision endpoint reads the screenshot with the purchase prompt —
    not the #514 label prompt — and its answer goes through the same
    validation as every route.
    """
    if not image:
        raise PurchaseError("unreadable")
    answer = ai_call(prompt=PURCHASE_PROMPT, image=image)
    if answer is None:
        raise PurchaseError("unreadable")
    return validate_purchase_answer(answer)


# ---------------------------------------------------------------------------
# Confirm text and creation

def line_text(line: dict[str, Any]) -> str:
    """One proposed roll as the confirm message shows it: color · material · brand · weight x qty."""
    head = " · ".join(str(part) for part in
                      (line.get("color"), line.get("material"), line.get("brand")) if part)
    return f"{head} · {line['grams']:g} g × {line['quantity']}"


def confirm_text(lines: list[dict[str, Any]]) -> str:
    total = sum(line["quantity"] for line in lines)
    body = "\n".join(f"- {line_text(line)}" for line in lines)
    return (f"Here is what I read from that purchase — {total} spool(s) on the shelf:\n{body}\n"
            "Create them all, edit them by hand instead, or cancel? Nothing is created yet.")


def created_text(created: list[dict[str, Any]]) -> str:
    ids = ", ".join(f"#{entry['spool_id']}" for entry in created)
    return f"Created spool(s) {ids} on the shelf."


def edit_handoff_text(lines: list[dict[str, Any]]) -> str:
    """The Edit route: nothing is created here; each line names the exact
    existing one-shot form (#512) the operator can adjust and run instead."""
    commands = "\n".join(
        f"`/spool new {line.get('brand') or ''}, {line['material']}, {line['color']}, {line['grams']:g}`"
        .replace(", ,", ",") for line in lines)
    return ("Nothing was created. Add or adjust each roll with the normal new-spool form:\n"
            f"{commands}")


class PartialCreationError(RuntimeError):
    """Creation stopped partway: some spools exist and must be reported as such.

    Claiming "nothing was created" after a partial failure sends the
    operator to add the spools by hand and duplicate the ones that did
    land, so the created ids travel with the failure.
    """

    def __init__(self, created: list[dict[str, Any]], cause: Exception):
        self.created = created
        self.cause = cause
        ids = ", ".join(f"#{entry['spool_id']}" for entry in created)
        super().__init__(
            f"Partly created: spool(s) {ids} are on the shelf, but creating the rest "
            f"failed ({type(cause).__name__}), so the remaining rolls were not created. "
            "Check the shelf before adding anything by hand, so nothing is duplicated.")


def create_purchase(lines: list[dict[str, Any]], *,
                    create_spool: Callable[[dict[str, Any]], dict[str, Any]] | None = None
                    ) -> list[dict[str, Any]]:
    """Create every proposed roll on the Shelf through spool_sync.create_spool.

    One create per roll, so quantity is exact and each spool records its
    own per-roll price and the purchase URL. Filament matching/creation is
    create_spool's own rule (brand + material + color; created only when
    new), so a second roll of a line reuses the filament the first made.
    A failure after some rolls landed raises PartialCreationError naming
    them; a failure before the first roll re-raises as-is, so callers can
    tell "some were created" from "none were".
    """
    create = create_spool or spool_sync.create_spool
    created = []
    for line in lines:
        spec = {"brand": line.get("brand"), "material": line["material"],
                "color": line["color"], "grams": line["grams"], "hex": line.get("hex"),
                "nozzle_temp": line.get("nozzle_temp"), "bed_temp": line.get("bed_temp"),
                "price": line.get("price"), "purchase_url": line.get("purchase_url")}
        spec = {key: value for key, value in spec.items() if value is not None}
        for _ in range(_quantity(line.get("quantity"))):
            try:
                made = create(spec)
            except Exception as exc:
                if created:
                    raise PartialCreationError(created, exc) from exc
                raise
            created.append({**made, "line": line})
    return created
