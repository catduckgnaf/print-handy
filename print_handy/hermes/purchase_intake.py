"""Purchase intake: propose spools from a shop link or an order screenshot (#801).

The conversational new-spool walkthrough (#512) and the photo label intake
(#514) create one spool at a time. This module reads what the operator
just bought — a product or order link, or a screenshot of a product page,
cart, order confirmation or receipt — into a list of line items the plugin
turns into one confirmation message. It owns, all pure and offline-testable:

* structured page reading (JSON-LD ``Product``, Open Graph, title) that
  resolves a complete product without any AI call;
* the purchase prompt and the validation every AI answer goes through
  (spool_label's helpers, and the same "Material labels" rule: a material
  Print Handy cannot resolve to a family is refused, never guessed);
* pack expansion (one line per color) and the one-line-per-roll wording;
* a bounded page fetch (short timeout, size cap, plain user agent, no
  retries) whose blocked or bot-walled outcome is a named error telling the
  operator to send a screenshot instead.

Nothing here creates a spool, stores the page or the image, or contacts
Spoolman. The plugin asks the operator to confirm the proposed rolls and
then calls spool_sync.create_spool once per roll with ``roll_specs``.
"""
from __future__ import annotations

import contextlib
import json
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable

from . import spool_label
from .spool_label import _bed_temp, _clean_text, _grams, _nozzle_temp, _strip_fence

MAX_QUANTITY = 20  # rolls of one line
MAX_ROLLS = 40  # rolls in one purchase: a typo or a misread total must not create hundreds

# The bounded fetch: one GET, no retries, a short wait, a capped body.
PAGE_TIMEOUT_S = 8
PAGE_MAX_BYTES = 1 << 20
PAGE_MAX_REDIRECTS = 3
PAGE_UA = "PrintHandy/1.0 (+purchase intake; one page read)"
PAGE_TEXT_CHARS = 6000  # what is sent to the AI call for a page that is not structured

FAILURE_MESSAGES = {
    "unreadable": ("I couldn't read that purchase. Send a clearer screenshot that shows the "
                   "filament names and quantities, or add the spools by hand with `/spool new`."),
    "non_json": ("The AI answer for that purchase wasn't usable, so nothing was read from it. "
                 "You can send a screenshot instead, or add the spools by hand with `/spool new`."),
    "no_items": ("I couldn't find any filament I can add in that purchase{detail}. "
                 "You can send a screenshot instead, or add the spools by hand with `/spool new`."),
    "too_many": ("That purchase came out as more than {limit} rolls, which looks like a misread, "
                 "so nothing was proposed. Send a screenshot of just the filament, or add the "
                 "spools by hand with `/spool new`."),
    "blocked": ("That shop wouldn't let Print Handy read the page{detail} (common on Amazon and "
                "other large stores). Send a screenshot of the product page or your order "
                "instead, or add the spools by hand with `/spool new`."),
    "fetch_failed": ("I couldn't open that link{detail}. Send a screenshot of the page or your "
                     "order instead, or add the spools by hand with `/spool new`."),
}


class PurchaseIntakeError(Exception):
    """A named purchase-reading failure; ``reason`` is one of FAILURE_MESSAGES' keys."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(FAILURE_MESSAGES[reason].format(detail=suffix, limit=MAX_ROLLS))


class PageBlockedError(PurchaseIntakeError):
    """The shop refused the fetch or served a bot wall; the operator sends a screenshot."""

    def __init__(self, detail: str = ""):
        super().__init__("blocked", detail)


@dataclass
class PurchaseRead:
    """What one purchase read produced: validated line items plus how they were read."""

    items: list[dict[str, Any]]
    source: str  # "page data", "page text" or "screenshot"
    url: str | None = None
    skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Recognizing the input

FILAMENT_WORDS = re.compile(
    r"\b(?:filament|spools?|pla\+?|petg|abs|asa|tpu|1\.75\s*mm|2\.85\s*mm)\b", re.I)

# Hosts and path shapes that read as a shop or order page. A link that is
# neither must carry filament words in the message to be offered anything.
_SHOP_HOST = re.compile(
    r"(?:^|\.)(?:amazon\.[a-z.]+|amzn\.(?:to|com)|a\.co|aliexpress\.[a-z.]+|ebay\.[a-z.]+|"
    r"walmart\.com|etsy\.com|temu\.com|newegg\.com|microcenter\.com|matterhackers\.com|"
    r"store\.bambulab\.com|us\.store\.bambulab\.com|eu\.store\.bambulab\.com|"
    r"prusa3d\.com|polymaker\.com|sunlu\.com|esun3d\.com|overture3d\.com|elegoo\.com|"
    r"creality\.com|store\.creality\.com|anycubic\.com|eryone3d\.com|3djake\.[a-z]+)$", re.I)
_SHOP_PATH = re.compile(
    r"/(?:dp|gp/product|gp/your-account/order-details|gp/css/order-details|products?|item|"
    r"collections/[^/]+/products|orders?|order-status|shop|store|cart)(?:/|$)", re.I)


def purchase_link(text: Any) -> str | None:
    """The one non-model shop or order link in a message, else None.

    Model links (MakerWorld, Printables, ...) are never purchase links: they
    keep their existing routing through core.link_intake, which is checked
    first. A message with several links is not guessed at.
    """
    from print_handy.core.link_intake import model_link

    text = str(text or "").strip()
    if not text or model_link(text):
        return None
    urls = re.findall(r"https?://[^\s<>\)\]]+", text)
    if len(urls) != 1:
        return None
    url = urls[0].rstrip(".,;!?")
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password:
        return None
    if _SHOP_HOST.search(host) or _SHOP_PATH.search(parts.path):
        return url
    return url if FILAMENT_WORDS.search(text.replace(url, " ") + " " + parts.path) else None


def mentions_filament(text: Any) -> bool:
    """Whether a message's own words talk about filament (for an image posted with it)."""
    return bool(FILAMENT_WORDS.search(str(text or "")))


# ---------------------------------------------------------------------------
# The AI call: prompt, answer validation

PURCHASE_PROMPT = (
    "You are reading a 3D printer filament purchase: a product page, shopping cart, order "
    "confirmation or receipt. Reply with a single JSON object and nothing else: "
    '{"items": [...]}. Each item is one filament product line with these keys: '
    '"brand" (the maker\'s name, or null), '
    '"material" (the material including any sub-type, e.g. "PLA", "Silk PLA", "PLA+", "PETG"), '
    '"color" (the color name exactly as listed), '
    '"hex" (a #RRGGBB code only when the page shows one, else null), '
    '"weight_g" (the net weight of ONE roll in grams as a number), '
    '"quantity" (the number of rolls ordered, 1 when not shown), '
    '"price" (the price of ONE roll as a number without a currency symbol, or null), '
    '"nozzle_temp" and "bed_temp" (recommended temperatures in °C as numbers, or null when not shown). '
    'For a multi-color pack such as "4 x 1 kg, Black/White/Gray/Red" give ONE item with '
    '"colors" listing every color in the pack, "quantity" the number of packs bought and '
    '"price" the price of the whole pack. Include only filament: skip printers, nozzles, '
    "build plates, tools, tax and shipping. Do not guess: use null for anything not shown, and "
    "never invent a weight or a quantity. "
    'If there is no readable filament purchase at all, reply {"unreadable": true}.'
)
_IMAGE_CONTEXT = "The image is a screenshot of the purchase. "


def _count(value: Any) -> int | None:
    numbers = spool_label._numbers(value)
    if not numbers:
        return None
    count = int(round(numbers[0]))
    return count if count >= 1 else None


def _price(value: Any) -> float | None:
    """One roll's price from a number or a string like '$21.99', '1,299.00' or '21,99 €'."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        raw = re.sub(r"[^0-9,.]", "", str(value or ""))
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
            raw = raw.replace(",", "")
        elif re.fullmatch(r"\d+,\d{1,2}", raw):
            raw = raw.replace(",", ".")
        try:
            price = float(raw)
        except ValueError:
            return None
    return round(price, 2) if 0 < price < 10000 else None


def _hex(value: Any) -> str | None:
    from print_handy.core import filament_color
    return filament_color.normalize_hex(value) if value else None


def material_supported(material: str) -> bool:
    """Whether Print Handy can resolve this material to a family it has a profile for.

    The same rule the rest of the stack applies (SETTINGS.md "Material labels"):
    finish words around the polymer resolve, and PLA+ is PLA; speed grades,
    blends and unknown names are refused rather than guessed.
    """
    from print_handy.core import printer_state
    try:
        printer_state.resolve_material_temperatures(material, {})
    except (ValueError, RuntimeError):
        return False
    return True


def _item(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """One validated line item, or (None, why it was skipped)."""
    material = _clean_text(entry.get("material"))
    color = _clean_text(entry.get("color"))
    grams = _grams(entry.get("weight_g") or entry.get("grams"))
    label = " ".join(x for x in (color, material) if x) or "an item"
    missing = [name for name, value in (("material", material), ("color", color), ("weight", grams))
               if not value]
    if missing:
        return None, f"{label}: no {', '.join(missing)} shown"
    if not material_supported(material):
        return None, (f"{label}: {material} is not a material Print Handy can use "
                      "(see SETTINGS.md, Material labels)")
    quantity = _count(entry.get("quantity")) or 1
    return {"brand": _clean_text(entry.get("brand")) or None, "material": material, "color": color,
            "hex": _hex(entry.get("hex")), "grams": grams, "quantity": min(quantity, MAX_QUANTITY),
            "price": _price(entry.get("price")),
            "nozzle_temp": _nozzle_temp(entry.get("nozzle_temp")),
            "bed_temp": _bed_temp(entry.get("bed_temp"))}, None


def _expand_pack(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """A pack variant (``colors``) becomes one entry per color.

    ``quantity`` is the number of packs, so each color is bought that many
    times; ``price`` is the whole pack's, split evenly per roll.
    """
    colors = entry.get("colors")
    if not isinstance(colors, list):
        return [entry]
    names = [_clean_text(c) for c in colors]
    names = [n for n in dict.fromkeys(names) if n]
    if len(names) < 2:
        return [{**entry, "color": names[0] if names else entry.get("color")}]
    pack_price = _price(entry.get("price"))
    each = round(pack_price / len(names), 2) if pack_price else None
    # A pack-wide hex cannot be right for every color.
    return [{**entry, "colors": None, "color": name, "price": each, "hex": None} for name in names]


def validate_items(answer: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate one AI answer into (line items, skipped notes).

    Accepts a parsed object, a bare list, or a JSON string (fences stripped).
    A line missing its material, color or weight, or with a material Print
    Handy refuses, is skipped and named in the notes; the rest are kept, and
    the confirmation message shows the notes, so nothing is dropped
    silently. Raises PurchaseIntakeError when nothing usable is left.
    """
    if isinstance(answer, str):
        try:
            answer = json.loads(_strip_fence(answer))
        except ValueError:
            raise PurchaseIntakeError("non_json") from None
    if isinstance(answer, dict):
        if answer.get("unreadable"):
            raise PurchaseIntakeError("unreadable")
        answer = answer.get("items", answer.get("line_items"))
    if not isinstance(answer, list):
        raise PurchaseIntakeError("non_json")
    items: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in answer:
        if not isinstance(entry, dict):
            continue
        for line in _expand_pack(entry):
            item, why = _item(line)
            if item is None:
                skipped.append(why)
                continue
            twin = next((i for i in items if _same_roll(i, item)), None)
            if twin is not None:  # the same product listed twice is one line
                twin["quantity"] = min(twin["quantity"] + item["quantity"], MAX_QUANTITY)
            else:
                items.append(item)
    if not items:
        detail = "; ".join(skipped[:3])
        raise PurchaseIntakeError("no_items", detail)
    if sum(i["quantity"] for i in items) > MAX_ROLLS:
        raise PurchaseIntakeError("too_many")
    return items, skipped


def _same_roll(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(str(a.get(k) or "").casefold() == str(b.get(k) or "").casefold()
               for k in ("brand", "material", "color")) and a["grams"] == b["grams"]


def _ask(ai_call: Callable[[Any, str], Any] | None, image: bytes | None, prompt: str) -> Any:
    if ai_call is None:
        raise PurchaseIntakeError("unreadable")
    try:
        answer = ai_call(image, prompt)
    except Exception:
        raise PurchaseIntakeError("unreadable") from None
    if answer is None:
        raise PurchaseIntakeError("unreadable")
    return answer


def extract_from_image(image: bytes, ai_call: Callable[[Any, str], Any] | None) -> PurchaseRead:
    """Read a purchase screenshot through the vision call (``ai_call(image, prompt)``)."""
    if not image:
        raise PurchaseIntakeError("unreadable")
    items, skipped = validate_items(_ask(ai_call, image, _IMAGE_CONTEXT + PURCHASE_PROMPT))
    return PurchaseRead(items, "screenshot", None, skipped)


# ---------------------------------------------------------------------------
# Structured page reading

class _PageParser(HTMLParser):
    """Collects what a shop page offers without scripting: title, meta tags,
    JSON-LD blocks and the visible text."""

    _SKIPPED = ("style", "noscript", "svg", "template")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.ld: list[str] = []
        self.text: list[str] = []
        self._skip = 0
        self._in_title = False
        self._ld_open = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").casefold()
            if key and "content" in attrs:
                self.meta.setdefault(key, attrs["content"])
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            if "ld+json" in attrs.get("type", "").casefold():
                self._ld_open = True
                self._buffer = []
            else:
                self._skip += 1
        elif tag in self._SKIPPED:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            if self._ld_open:
                self.ld.append("".join(self._buffer))
                self._ld_open = False
            elif self._skip:
                self._skip -= 1
        elif tag in self._SKIPPED and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._ld_open:
            self._buffer.append(data)
        elif self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(re.sub(r"\s+", " ", data).strip())


def parse_page(html: Any) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:  # malformed markup must degrade to "less data", not raise
        pass
    return parser


def _ld_products(blocks: list[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
        elif isinstance(node, dict):
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if any(str(k).casefold() in ("product", "productgroup") for k in kinds):
                found.append(node)
            for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                if key in node:
                    walk(node[key], depth + 1)

    for raw in blocks:
        try:
            walk(json.loads(raw.strip()))
        except ValueError:
            continue
    return found


# Words that change what a roll is. When the product name carries one that the
# matched material does not, the page is not "complete": the AI path reads it.
_FINISH_WORD = re.compile(
    r"\b(?:silk|matte|marble|glow|wood|sparkle|galaxy|rainbow|carbon|cf|gf|fiber|glass|"
    r"metallic|speed|hs|hf)\b", re.I)
_MULTI_WORD = re.compile(
    r"\b\d+\s*(?:packs?|rolls?|spools?|colou?rs?|pcs|pieces)\b|\b(?:bundle|multi|assorted|set of)\b|"
    r"\bcolou?rs?\s*(?:pack|set|bundle)\b", re.I)


def _name_grams(text: str) -> float | None:
    """The single net weight a product name states, else None (two weights are ambiguous)."""
    values = set()
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(kg|g)\b", text.casefold()):
        value = float(match.group(1)) * (1000 if match.group(2) == "kg" else 1)
        if 50 <= value <= 10000:
            values.add(round(value, 1))
    return values.pop() if len(values) == 1 else None


def _node_grams(node: dict[str, Any], name: str) -> float | None:
    weight = node.get("weight")
    if isinstance(weight, dict):
        unit = str(weight.get("unitCode") or weight.get("unitText") or "").casefold()
        number = spool_label._numbers(weight.get("value"))
        if number and unit in ("kgm", "kg", "kilogram", "kilograms"):
            return _grams(f"{number[0]} kg")
        if number and unit in ("grm", "g", "gram", "grams"):
            return _grams(number[0])
    elif weight:
        return _grams(weight)
    return _name_grams(name)


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or ""
    return _clean_text(value)


def _node_price(node: dict[str, Any]) -> float | None:
    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else [offers]
    prices = set()
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        spec = offer.get("priceSpecification")
        value = offer.get("price", offer.get("lowPrice"))
        if value is None and isinstance(spec, dict):
            value = spec.get("price")
        price = _price(value)
        if price is not None:
            prices.add(price)
    return prices.pop() if len(prices) == 1 else None  # variants at different prices: unknown


def extract_from_page(html: Any, url: str | None = None) -> list[dict[str, Any]] | None:
    """Line items from a page's own structured data, or None when it is not complete.

    Reads a JSON-LD ``Product`` first, then an Open Graph product (``og:type``
    product, with the title as the name). The result is one line (quantity 1)
    only when the page names exactly one product with a material, a single
    color and a single net weight that Print Handy accepts; a product group,
    a multi-color or multi-roll title, a finish word the material did not
    capture ("Matte Black PLA"), or a material it refuses all return None, so
    the AI call reads the page text instead. A complete page costs no AI call.
    """
    parsed = parse_page(html)
    products = _ld_products(parsed.ld)
    if len(products) > 1:
        return None
    node = products[0] if products else None
    meta = parsed.meta
    if node is None:
        if "product" not in meta.get("og:type", "").casefold():
            return None
        node = {"name": meta.get("og:title") or parsed.title,
                "brand": meta.get("product:brand") or meta.get("og:brand"),
                "description": meta.get("og:description"),
                "offers": {"price": meta.get("product:price:amount") or meta.get("og:price:amount")}}
    elif str(node.get("@type")).casefold() == "productgroup" or node.get("hasVariant"):
        return None
    name = _first_text(node.get("name"))
    if not name or _MULTI_WORD.search(name):
        return None
    low = name.casefold()
    material = (_first_text(node.get("material")) if not str(node.get("material") or "").startswith("http")
                else "") or spool_label._match_vocabulary(spool_label.TEXT_MATERIALS, low)
    if not material or not material_supported(material):
        return None
    for word in _FINISH_WORD.findall(name):
        if word.casefold() not in material.casefold():
            return None
    color = _first_text(node.get("color"))
    if color:
        if re.search(r"[/&,]|\band\b|\bmulti|\bassorted", color, re.I):
            return None
    else:
        color = spool_label._match_color(low, name, []) or ""
        # A color vocabulary hit in a title that lists several colors is a guess.
        if not color:
            return None
    grams = _node_grams(node, name)
    if not grams:
        return None
    brand = _first_text(node.get("brand")) or None
    item, _why = _item({"brand": brand, "material": material, "color": color, "weight_g": grams,
                        "quantity": 1, "price": _node_price(node)})
    return [item] if item else None


# ---------------------------------------------------------------------------
# The bounded page fetch

_BLOCK_STATUSES = (401, 403, 405, 406, 429, 451, 503)
_REDIRECTS = (301, 302, 303, 307, 308)
_WALL_MARKERS = (
    "captcha", "robot check", "are you a robot", "are you a human", "verify you are human",
    "unusual traffic", "automated access", "access denied", "request blocked",
    "enable javascript and cookies", "just a moment", "attention required",
)


def looks_blocked(parsed: _PageParser) -> bool:
    """Whether a parsed page is a bot wall rather than a product page.

    Judged on the title and the first visible text only (never scripts, which
    carry reCAPTCHA references on perfectly good pages), and never when the
    page carries its own product data.
    """
    if _ld_products(parsed.ld) or "product" in parsed.meta.get("og:type", "").casefold():
        return False
    head = (parsed.title + " " + " ".join(parsed.text)[:3000]).casefold()
    return any(marker in head for marker in _WALL_MARKERS)


def fetch_page(url: str, *, timeout: float = PAGE_TIMEOUT_S, max_bytes: int = PAGE_MAX_BYTES,
               open_once: Callable[..., Any] | None = None) -> tuple[str, str]:
    """Fetch one page once: (final URL, HTML text).

    A short timeout, a capped body, a plain user agent and no retries. Every
    hop (the link and each of up to three redirects) passes the public-address
    check in core.model_job, so a link cannot reach the local network. A
    refusal or bot wall raises PageBlockedError; anything else that stops the
    read raises PurchaseIntakeError("fetch_failed"). ``open_once`` injects the
    transport in tests.
    """
    if open_once is None:
        from print_handy.core import model_job
        opener, refused = model_job._open_once, model_job.DirectLinkError
    else:
        opener, refused = open_once, ()  # an injected transport has no address check to refuse
    current = str(url)
    for hop in range(PAGE_MAX_REDIRECTS + 1):
        try:
            response = opener(current, timeout=timeout, user_agent=PAGE_UA)
        except urllib.error.HTTPError as exc:
            code = exc.code
            location = exc.headers.get("Location") if exc.headers else None
            with contextlib.suppress(Exception):
                exc.close()
            if code in _REDIRECTS and location and hop < PAGE_MAX_REDIRECTS:
                current = urllib.parse.urljoin(current, location)
                continue
            if code in _BLOCK_STATUSES:
                raise PageBlockedError(f"HTTP {code}") from None
            raise PurchaseIntakeError("fetch_failed", f"the shop answered HTTP {code}") from None
        except refused:
            raise PurchaseIntakeError(
                "fetch_failed", "that address could not be reached or is not a public web page") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise PurchaseIntakeError("fetch_failed", "the shop did not answer in time") from None
        try:
            status = getattr(response, "status", None) or getattr(response, "code", 200)
            kind = str((getattr(response, "headers", None) or {}).get("Content-Type") or "")
            if status in _REDIRECTS:
                location = (getattr(response, "headers", None) or {}).get("Location")
                if location and hop < PAGE_MAX_REDIRECTS:
                    current = urllib.parse.urljoin(current, location)
                    continue
                raise PurchaseIntakeError("fetch_failed", "too many redirects")
            if status in _BLOCK_STATUSES:
                raise PageBlockedError(f"HTTP {status}")
            if kind and not re.search(r"html|xml|text/plain", kind, re.I):
                raise PurchaseIntakeError("fetch_failed", "that link is not a web page")
            body = response.read(max_bytes)
        except (OSError, ValueError):
            raise PurchaseIntakeError("fetch_failed", "the shop did not answer in time") from None
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        charset = re.search(r"charset=([\w-]+)", kind, re.I)
        try:
            return current, body.decode(charset.group(1) if charset else "utf-8", "replace")
        except LookupError:
            return current, body.decode("utf-8", "replace")
    raise PurchaseIntakeError("fetch_failed", "too many redirects")  # pragma: no cover


def page_text(parsed: _PageParser, limit: int = PAGE_TEXT_CHARS) -> str:
    """The trimmed page content sent to the AI call: title, product meta, JSON-LD, visible text."""
    lines = []
    if parsed.title.strip():
        lines.append("Title: " + re.sub(r"\s+", " ", parsed.title).strip())
    for key in ("og:title", "og:description", "og:site_name", "product:brand",
                "product:price:amount", "og:price:amount"):
        if parsed.meta.get(key):
            lines.append(f"{key}: {_clean_text(parsed.meta[key])}")
    for node in _ld_products(parsed.ld)[:3]:
        lines.append("Product data: " + json.dumps(node, ensure_ascii=False)[:2000])
    lines.append(" ".join(parsed.text))
    return "\n".join(line for line in lines if line.strip())[:limit]


def read_link(url: str, ai_call: Callable[[Any, str], Any] | None, *,
              fetcher: Callable[[str], tuple[str, str]] | None = None) -> PurchaseRead:
    """Read a shop link: fetch it once, structured data first, then the AI call on the page text.

    ``fetcher`` injects the page fetch in tests (it returns (final URL, HTML)).
    Only the link is kept for the spool; the page itself never is.
    """
    final, html = (fetcher or fetch_page)(url)
    items = extract_from_page(html, final)
    if items:
        return PurchaseRead(items, "page data", url)
    parsed = parse_page(html)
    if looks_blocked(parsed):
        raise PageBlockedError("the page asked for a robot check")
    text = page_text(parsed)
    if len(text) < 80:
        raise PageBlockedError("the page had no readable content")
    host = urllib.parse.urlsplit(final).hostname or "the shop"
    answer = _ask(ai_call, None, PURCHASE_PROMPT + f"\nThis is the text of a page from {host}:\n" + text)
    found, skipped = validate_items(answer)
    return PurchaseRead(found, "page text", url, skipped)


# ---------------------------------------------------------------------------
# Wording and the specs spool_sync creates

def weight_text(grams: float) -> str:
    return f"{grams / 1000:g} kg" if grams >= 1000 and grams % 100 == 0 else f"{grams:g} g"


def line_text(item: dict[str, Any]) -> str:
    """One proposed line: color · material · brand · weight × quantity."""
    return (f"{item['color']} · {item['material']} · {item.get('brand') or 'no brand'} · "
            f"{weight_text(item['grams'])} × {item['quantity']}")


def roll_count(items: list[dict[str, Any]]) -> int:
    return sum(i["quantity"] for i in items)


def type_count(items: list[dict[str, Any]]) -> int:
    """How many distinct filament types (brand + material + color) the lines are."""
    return len({(str(i.get("brand") or "").casefold(), i["material"].casefold(), i["color"].casefold())
                for i in items})


def proposal_text(read: PurchaseRead) -> str:
    """The one confirmation message body listing every proposed roll."""
    rolls, types = roll_count(read.items), type_count(read.items)
    head = (f"From {read.source}, I read {rolls} spool{'s' if rolls != 1 else ''} "
            f"on {types} filament type{'s' if types != 1 else ''}, all going on the Shelf:")
    lines = [head] + [f"{n}. {line_text(i)}" for n, i in enumerate(read.items, 1)]
    if read.skipped:
        lines.append("Left out: " + "; ".join(read.skipped))
    lines.append("Nothing is created until you press Create all.")
    return "\n".join(lines)


def roll_specs(items: list[dict[str, Any]], url: str | None = None) -> list[dict[str, Any]]:
    """One spool_sync.create_spool spec per roll: a quantity-2 line is two specs.

    ``price`` and ``purchase_url`` ride the spec; create_spool records them
    on the Spoolman spool (its price field and its comment).
    """
    specs = []
    for item in items:
        spec = {"brand": item.get("brand"), "material": item["material"], "color": item["color"],
                "grams": item["grams"], "hex": item.get("hex"), "price": item.get("price"),
                "nozzle_temp": item.get("nozzle_temp"), "bed_temp": item.get("bed_temp")}
        if url:
            spec["purchase_url"] = url[:300]
        specs.extend(dict(spec) for _ in range(item["quantity"]))
    return specs
