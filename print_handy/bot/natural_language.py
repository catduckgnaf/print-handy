"""The deterministic natural-language capability layer (#1524).

Plain language goes phrase -> typed intent -> validated slots -> shared
service -> factual answer, with no model call. The capability registry
in this module is the one registry three consumers share: chat intents,
the typed read-only tool set for the mentioned conversational lane, and
generated help. Sentence templates live as data in ``nl_templates.json``
and are compiled once; slot vocabulary (colors, brands, materials,
printer names) is built from live Spoolman and roster data, with a
hex-based color family only as an explicit second tier.

Safety shape:

* Mood before intent: a mutating intent is allowed only from an
  un-negated imperative. Questions, past-tense statements and negations
  route to the read-only or how-to sibling, or are blocked outright.
* Read-only by construction: read-only handlers receive a
  :class:`ReadOnlyFacade` with no write methods.
* Bounded follow-up context: per user/channel/thread, expiring,
  scope-isolated, and never a source of mutation authority.
* Shadow mode: while a capability is in shadow, the layer only logs
  disagreements locally and the old behavior still answers.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

READ_ONLY = "read-only"
MUTATING = "mutating"
EXTERNAL = "external"

PLATFORMS = ("discord", "telegram", "matrix", "webui")


@dataclass(frozen=True)
class Capability:
    """One user-facing capability: the registry row every consumer reads."""

    id: str
    description: str
    effect: str
    families: tuple = ()
    platforms: tuple = PLATFORMS
    aliases: tuple = ()
    examples: tuple = ()
    slots: tuple = ()
    confirmation: str = "none"  # none | existing | never-from-model
    handler: str = ""           # answer function name in this module, or "" (registry-only)


CAPABILITIES = (
    Capability("inventory_total", "Total remaining filament by color, material or brand",
               READ_ONLY, examples=("How much purple PLA do I have?", "How much filament do I have?"),
               slots=("color", "material", "brand", "loaded_only", "exclude_color"),
               handler="answer_inventory_total"),
    Capability("inventory_count", "Count matching spools", READ_ONLY,
               examples=("How many rolls of black PETG are left?",),
               slots=("color", "material", "brand"), handler="answer_inventory_total"),
    Capability("inventory_list", "List matching spools or owned colors", READ_ONLY,
               examples=("Show all Polymaker spools", "Which colors do I own?"),
               slots=("color", "material", "brand"), handler="answer_inventory_total"),
    Capability("spool_detail", "One spool's real recorded fields", READ_ONLY,
               examples=("Tell me about spool #12", "Where is spool 12?"),
               slots=("spool_id",), handler="answer_spool_detail"),
    Capability("loaded_spools", "What is loaded, per printer and slot", READ_ONLY,
               examples=("What's loaded?", "What's in slot 2?", "How much white PLA is loaded?"),
               slots=("slot", "color", "material", "printer"), handler="answer_loaded"),
    Capability("inventory_shelf", "Spools on the shelf", READ_ONLY,
               examples=("Which spools are on the shelf?",), handler="answer_inventory_total"),
    Capability("inventory_nearly_empty", "Spools nearly empty", READ_ONLY,
               examples=("Which spools are nearly empty?", "Which color am I running out of?"),
               handler="answer_inventory_total"),
    Capability("inventory_unknown_weights", "Spools whose remaining weight is unknown", READ_ONLY,
               examples=("Which spools have missing weights?",), handler="answer_inventory_total"),
    Capability("inventory_enough", "Enough filament for a model, only with a verified sliced requirement",
               READ_ONLY, examples=("Do I have enough for this model?",),
               slots=("color", "material", "required_grams"), handler="answer_enough"),
    Capability("purchase_guidance", "Where and how to buy filament; never creates inventory",
               READ_ONLY, examples=("Where can I buy PLA?", "Buy filament"),
               handler="answer_purchase_guidance"),
    Capability("purchase_record", "A past purchase, explained — recording is the purchase intake's, with its offer",
               READ_ONLY, examples=("I bought purple PLA",), handler="answer_purchase_record"),
    Capability("spool_add", "Add a new spool (guided, with its confirmation)", MUTATING,
               examples=("Add a new spool",), confirmation="existing"),
    Capability("spool_load", "Load an existing spool into a slot (guided, with its confirmation)",
               MUTATING, examples=("Load spool 12 into slot 2",), confirmation="existing"),
    Capability("spool_weight_guidance", "Correcting a spool's recorded weight, guided", READ_ONLY,
               examples=("My spool weight is wrong",), handler="answer_spool_weight_guidance"),
    Capability("firmware_version", "Installed firmware and component versions", READ_ONLY,
               examples=("What firmware am I on?", "What ZMod version is installed?"),
               slots=("component",), handler="answer_firmware"),
    Capability("firmware_updates", "Whether updates are available, in seven honest outcome states",
               READ_ONLY, examples=("Are there ZMod updates?", "Is there an update?"),
               slots=("component",), handler="answer_firmware"),
    Capability("firmware_release_notes", "Published release information, feed-only families included",
               READ_ONLY, examples=("Show the latest Forge-X release",), handler="answer_firmware"),
    Capability("firmware_howto", "How to update, per family", READ_ONLY,
               examples=("How do I update?", "How do I update the stock AD5X?"),
               handler="answer_firmware"),
    Capability("firmware_update_apply", "Apply one firmware update through the existing guarded flow",
               MUTATING, examples=("Update ZMod",), confirmation="existing"),
    Capability("printer_status", "Printer and job state", READ_ONLY,
               examples=("What is the AD5X doing?", "How long left?", "What layer is it on?"),
               handler="answer_status"),
    Capability("print_history", "Past prints and their outcomes", READ_ONLY,
               examples=("What was my last print?", "Did the last print fail?"),
               handler="answer_status"),
    Capability("print_pause", "Pause the current print (runs at once, as #1535 shipped)", MUTATING,
               examples=("Pause my print",), confirmation="none"),
    Capability("print_resume", "Resume a paused print", MUTATING,
               examples=("Resume it",), confirmation="existing"),
    Capability("print_stop", "Stop the current job", MUTATING,
               examples=("Stop this job",), confirmation="existing"),
    Capability("camera_view", "Camera view and Pasta Cam state", READ_ONLY,
               examples=("Show my camera", "Take a picture"), handler="answer_status"),
    Capability("troubleshooting", "Print-defect diagnosis guidance", READ_ONLY,
               examples=("My first layer won't stick", "The extruder clicks"),
               handler="answer_troubleshooting"),
    Capability("maintenance_due", "Maintenance and calibration state", READ_ONLY,
               examples=("What maintenance is due?",), handler="answer_status"),
    Capability("capability_help", "What Print Handy can do, generated from this registry", READ_ONLY,
               examples=("What can you do?",), handler="answer_capability_help"),
    Capability("indx_tool_question", "Tool questions on an INDX printer (#1198): registered here, "
               "answered honestly as not built yet", READ_ONLY,
               examples=("What is on T2?",), slots=("tool",), handler="answer_indx"),
    Capability("model_fit_question", "Fit and capability questions about a model or material", READ_ONLY,
               examples=("Can this fit on my bed?", "Can this printer print TPU?"),
               handler="answer_fit"),
)

REGISTRY = {cap.id: cap for cap in CAPABILITIES}


def capability(capability_id):
    return REGISTRY.get(str(capability_id or ""))


# ---------------------------------------------------------------- mood

_NEGATED = re.compile(r"^(?:please\s+)?(?:don'?t|do\s+not|never|no,?\s+don'?t)\b", re.I)
_NEGATED_VERB = re.compile(r"\b(?:don'?t|do\s+not|never)\s+(?:\w+\s+)?(stop|pause|cancel|start|print|update|load|add)\b", re.I)
_QUESTION_START = re.compile(
    r"^(?:what|which|who|where|when|why|how|is|are|do|does|did|can|could|would|has|have)\b", re.I)
_PAST = re.compile(
    r"\b(?:bought|ordered|purchased|updated|upgraded|loaded|printed|stopped|paused|added|finished|yesterday)\b",
    re.I)
_PAST_TENSE_VERBS = ("bought", "updated", "loaded", "printed", "stopped", "paused", "added")


def classify_mood(text):
    """One of imperative, interrogative, past, negated (#1524 plan review).

    A mutating intent may only fire from an un-negated imperative, so
    this classification runs before intent effects are honored.
    """
    clean = " ".join(str(text or "").split()).strip()
    if _NEGATED.search(clean) or _NEGATED_VERB.search(clean):
        return "negated"
    if clean.endswith("?") or _QUESTION_START.match(clean):
        # "Why did the print stop?" is a question about the past, and its
        # route is diagnostic — interrogative wins over past.
        return "interrogative"
    if _PAST.search(clean):
        return "past"
    return "imperative"


# Mutating intent -> the read-only sibling a non-imperative mood routes
# to, when one exists. None means the intent is simply blocked.
MUTATING_SIBLINGS = {
    "firmware_update_apply": "firmware_updates",
    "print_pause": "printer_status",
    "print_stop": "printer_status",
    "print_resume": "printer_status",
    "spool_add": None,
    "spool_load": None,
}


# ---------------------------------------------------------------- vocabulary

BASE_MATERIALS = ("pla+", "pla plus", "silk pla", "pla", "petg", "abs", "asa", "tpu", "nylon",
                  "pc", "hips", "pp", "pet", "cf", "wood", "marble")
BASE_COLORS = ("purple", "black", "white", "red", "orange", "yellow", "green", "blue", "pink",
               "gray", "grey", "brown", "clear", "transparent", "natural", "gold", "silver",
               "lilac", "violet", "lavender", "magenta", "cyan", "teal", "beige", "ivory",
               "olive", "maroon", "navy", "turquoise", "peach", "mint", "rainbow", "galaxy")

# The explicit second tier: a generic color word may match a commercial
# color name in its family, but the answer must say it is a family match,
# never an asserted exact color.
COLOR_FAMILIES = {
    "purple": ("purple", "violet", "lilac", "lavender", "magenta", "plum"),
    "blue": ("blue", "navy", "cyan", "teal", "turquoise", "azure", "cobalt"),
    "green": ("green", "olive", "mint", "lime", "emerald"),
    "red": ("red", "maroon", "burgundy", "crimson"),
    "gray": ("gray", "grey", "silver", "charcoal", "graphite"),
    "white": ("white", "ivory", "cream", "natural"),
    "black": ("black", "charcoal", "onyx"),
}


def hex_color_family(hex_value):
    """The broad family of a #RRGGBB hex, or None when it has no hue family."""
    text = str(hex_value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 0.08:
        return "white" if mx > 0.85 else "black" if mx < 0.2 else "gray"
    if mx == r:
        hue = (60 * ((g - b) / (mx - mn))) % 360
    elif mx == g:
        hue = 60 * ((b - r) / (mx - mn)) + 120
    else:
        hue = 60 * ((r - g) / (mx - mn)) + 240
    if hue < 15 or hue >= 345:
        return "red"
    if hue < 45:
        return "orange"
    if hue < 70:
        return "yellow"
    if hue < 160:
        return "green"
    if hue < 200:
        return "cyan"
    if hue < 255:
        return "blue"
    return "purple"


def build_vocabulary(spools=(), printer_names=()):
    """Slot vocabulary from the user's own data (#1524 plan review point 5).

    Colors, brands and materials come from the live Spoolman records, so
    a custom color name or a brand like Polymaker matches without a
    hand-maintained synonym list; the base lists only seed an empty
    inventory.
    """
    colors, brands, materials = set(BASE_COLORS), set(), set(BASE_MATERIALS)
    for spool in spools or ():
        if not isinstance(spool, dict):
            continue
        rec = normalize_spool(spool)
        if rec["color"]:
            colors.add(rec["color"].casefold())
        if rec["brand"]:
            brands.add(rec["brand"].casefold())
        if rec["material"]:
            materials.add(rec["material"].casefold())
    printers = {str(name).casefold() for name in printer_names or () if str(name).strip()}
    return {"colors": sorted(colors, key=len, reverse=True),
            "brands": sorted(brands, key=len, reverse=True),
            "materials": sorted(materials, key=len, reverse=True),
            "printers": sorted(printers, key=len, reverse=True)}


# ---------------------------------------------------------------- spool records

def normalize_spool(raw, loaded_ids=()):
    """One Spoolman spool record in the engine's shape.

    Only real fields: a missing remaining weight stays None (unknown),
    never an estimate. ``loaded_ids`` (spool ids in slots) marks loaded
    spools; a record's own ph_slot extra also counts.
    """
    raw = raw or {}
    filament = raw.get("filament") or {}
    vendor = filament.get("vendor") or {}
    brand = vendor.get("name") if isinstance(vendor, dict) else None
    brand = brand or filament.get("brand") or raw.get("brand")
    color = filament.get("color_name") or filament.get("name") or raw.get("color")
    material = filament.get("material") or raw.get("material")
    remaining = raw.get("remaining_weight", raw.get("remaining_grams"))
    initial = raw.get("initial_weight", raw.get("initial_grams"))
    extra = raw.get("extra") or {}
    slot = None
    ph = extra.get("ph_slot") if isinstance(extra, dict) else None
    if isinstance(ph, str) and ":" in ph:
        try:
            slot = int(ph.rsplit(":", 1)[1])
        except ValueError:
            slot = None
    spool_id = raw.get("id", raw.get("spool_id"))
    return {
        "id": int(spool_id) if spool_id is not None else None,
        "brand": str(brand).strip() if brand else None,
        "material": str(material).strip() if material else None,
        "color": str(color).strip() if color else None,
        "color_hex": filament.get("color_hex") or raw.get("color_hex"),
        "remaining": float(remaining) if isinstance(remaining, (int, float)) else None,
        "initial": float(initial) if isinstance(initial, (int, float)) else None,
        "location": raw.get("location"),
        "archived": bool(raw.get("archived")),
        "slot": slot,
        "loaded": (spool_id in set(loaded_ids or ())) or slot is not None,
        "name": raw.get("name") or filament.get("name"),
    }


# ---------------------------------------------------------------- templates

@dataclass(frozen=True)
class _Template:
    intent: str
    regex: re.Pattern


def _load_templates():
    path = Path(__file__).with_name("nl_templates.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("templates") or []
    except Exception:
        logger.exception("could not load nl_templates.json; the NL layer matches nothing")
        rows = []
    compiled = []
    for row in rows:
        try:
            compiled.append(_Template(str(row["intent"]), re.compile(str(row["pattern"]), re.I)))
        except Exception:
            logger.exception("bad NL template row: %r", row)
    return tuple(compiled)


TEMPLATES = _load_templates()


@dataclass
class ParsedIntent:
    capability_id: str
    slots: dict = field(default_factory=dict)
    mood: str = "imperative"
    text: str = ""
    blocked: bool = False        # a mutating intent a non-imperative mood stopped
    routed_to: str = ""          # the read-only sibling a mood routed to, when any

    @property
    def capability(self):
        return capability(self.routed_to or self.capability_id)

    @property
    def effect(self):
        cap = self.capability
        return cap.effect if cap else READ_ONLY

    def allowed_to_mutate(self):
        cap = capability(self.capability_id)
        return bool(cap and cap.effect == MUTATING and not self.blocked and not self.routed_to)


_ACTION_IMPERATIVE = re.compile(r"\b(?:kill|stop|cancel|pause|abort|restart)\s+it\b", re.I)

_COMPONENTS = {"zmod": "ZMod", "klipper": "Klipper", "moonraker": "Moonraker",
               "forge-x": "Forge-X", "ff5m": "Forge-X", "opencreator": "OpenCreator",
               "open creator": "OpenCreator", "helixscreen": "HelixScreen",
               "fluidd": "Fluidd", "mainsail": "Mainsail", "bambu": "Bambu"}


def _scan_vocabulary(clean, vocabulary):
    """Word slots from the vocabulary: color, material, brand, printer.

    Longest names first, word-boundary matched; a color negated with
    "not <color>" lands in exclude_color instead of color.
    """
    slots = {}
    low = " " + clean.casefold() + " "
    excluded = set()
    for match in re.finditer(r"\bnot\s+([a-z]+)\b", low):
        excluded.add(match.group(1))
    for kind, key in (("color", "colors"), ("brand", "brands"),
                      ("material", "materials"), ("printer", "printers")):
        for word in (vocabulary or {}).get(key) or ():
            # Lookarounds, not \b: "pla+" must match before punctuation.
            if re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", low):
                if kind == "color" and word in excluded:
                    slots.setdefault("exclude_color", word)
                    continue
                slots.setdefault(kind, word)
                break
    if "exclude_color" in slots and slots.get("color") == slots["exclude_color"]:
        del slots["color"]
    return slots


def matching_intents(text, vocabulary=None):
    """Every intent whose template matches, in template order.

    The pairwise-ambiguity corpus test runs this over the whole gold
    corpus: a positive utterance must match exactly one intent.
    """
    clean = " ".join(str(text or "").split()).strip().rstrip(".!").strip()
    if not clean or clean.startswith("/"):
        return []
    found = []
    for template in TEMPLATES:
        if template.regex.match(clean) and template.intent not in found:
            found.append(template.intent)
    return found


def parse(text, vocabulary=None):
    """The one ParsedIntent for a message, or None when nothing matches.

    Mood is classified first; a mutating intent matched by a
    non-imperative mood is blocked or routed to its read-only sibling.
    """
    clean = " ".join(str(text or "").split()).strip()
    if not clean or clean.startswith("/"):
        return None
    stripped = clean.rstrip(".!").strip()
    mood = classify_mood(clean)
    for template in TEMPLATES:
        match = template.regex.match(stripped)
        if not match:
            continue
        if template.intent == "troubleshooting" and _ACTION_IMPERATIVE.search(stripped):
            # "The print is spaghetti, kill it" is a symptom PLUS an
            # action request: the action owns the message (the lanes and
            # their confirmations), not the diagnosis.
            continue
        slots = {k: v for k, v in match.groupdict().items() if v is not None}
        if template.intent == "firmware_update_apply" and "component" in slots:
            # "Update it" names no component: not an apply command at all.
            if str(slots["component"]).strip().casefold() not in _COMPONENTS:
                continue
        if "tool" in slots:
            slots["tool"] = str(slots["tool"]).casefold()
        for key in ("spool_id", "slot"):
            if key in slots:
                try:
                    slots[key] = int(slots[key])
                except (TypeError, ValueError):
                    slots.pop(key, None)
        slots.update({k: v for k, v in _scan_vocabulary(stripped, vocabulary).items()
                      if k not in slots})
        for component in _COMPONENTS:
            if re.search(r"\b" + re.escape(component) + r"\b", stripped, re.I):
                slots.setdefault("component", _COMPONENTS[component])
                break
        grams = re.search(r"\b(\d+(?:\.\d+)?)\s*g\b", stripped, re.I)
        if grams and template.intent == "inventory_enough":
            slots.setdefault("required_grams", float(grams.group(1)))
        parsed = ParsedIntent(capability_id=template.intent, slots=slots, mood=mood, text=clean)
        cap = capability(template.intent)
        if cap and cap.effect == MUTATING and mood != "imperative":
            parsed.blocked = True
            sibling = MUTATING_SIBLINGS.get(template.intent)
            if sibling:
                parsed.routed_to = sibling
        return parsed
    return None


# ---------------------------------------------------------------- follow-up context

class ContextStore:
    """Bounded, expiring, scope-isolated follow-up context.

    Keys are (user, channel, thread) session keys; an entry holds only
    the last intent's slots. Context can refine a read-only question; it
    never authorizes a mutation, and an entry is invisible to any other
    scope.
    """

    def __init__(self, ttl_seconds=600, max_entries=256, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.clock = clock
        self._entries = {}

    def put(self, key, slots):
        if not key:
            return
        if len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k][1])
            self._entries.pop(oldest, None)
        self._entries[key] = (dict(slots or {}), self.clock() + self.ttl)

    def get(self, key):
        row = self._entries.get(key)
        if not row:
            return None
        slots, expires = row
        if self.clock() > expires:
            self._entries.pop(key, None)
            return None
        return dict(slots)

    def clear(self, key=None):
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)


CONTEXT = ContextStore()

_FOLLOWUP_ONLY = re.compile(r"^only\s+(.+)$", re.I)
_FOLLOWUP_WHAT_ABOUT = re.compile(r"^what about\s+(.+?)\??$", re.I)
_FOLLOWUP_LOADED = re.compile(r"^which (?:of those |ones )?are loaded\??$", re.I)
_FOLLOWUP_UPDATE = re.compile(r"^can i update it\??$", re.I)
_FOLLOWUP_KG = re.compile(r"^how much is that in (?:kg|kilograms)\??$", re.I)
_FOLLOWUP_DETAILS = re.compile(r"^show details\??$", re.I)


def parse_with_context(text, key=None, vocabulary=None):
    """Parse, then fold a short follow-up into the stored context slots.

    "Only Polymaker" narrows the brand; "What about black?" swaps the
    color; "Which are loaded?" keeps the filters and asks for the loaded
    subset. A follow-up with no stored context is just an unmatched
    phrase — it never invents a scope.
    """
    parsed = parse(text, vocabulary)
    stored = CONTEXT.get(key) if key else None
    clean = " ".join(str(text or "").split()).strip()
    if stored:
        only = _FOLLOWUP_ONLY.match(clean)
        about = _FOLLOWUP_WHAT_ABOUT.match(clean)
        if parsed is None and (only or about):
            fragment = (only or about).group(1)
            extra = _scan_vocabulary(fragment, vocabulary)
            merged = dict(stored)
            merged.update(extra)
            parsed = ParsedIntent(capability_id="inventory_total", slots=merged,
                                  mood="interrogative", text=clean)
        elif parsed is None and _FOLLOWUP_LOADED.match(clean):
            merged = dict(stored)
            merged["loaded_only"] = True
            parsed = ParsedIntent(capability_id="inventory_total", slots=merged,
                                  mood="interrogative", text=clean)
        elif parsed is None and _FOLLOWUP_KG.match(clean):
            merged = dict(stored)
            merged["unit"] = "kg"
            parsed = ParsedIntent(capability_id="inventory_total", slots=merged,
                                  mood="interrogative", text=clean)
        elif parsed is None and _FOLLOWUP_UPDATE.match(clean) and stored.get("component"):
            parsed = ParsedIntent(capability_id="firmware_updates",
                                  slots={"component": stored["component"]},
                                  mood="interrogative", text=clean)
        elif parsed is None and _FOLLOWUP_DETAILS.match(clean) and stored.get("spool_id"):
            parsed = ParsedIntent(capability_id="spool_detail",
                                  slots={"spool_id": stored["spool_id"]},
                                  mood="interrogative", text=clean)
        elif parsed is not None and parsed.capability_id == "inventory_total":
            for name in ("color", "material", "brand"):
                if name not in parsed.slots and name in stored:
                    parsed.slots[name] = stored[name]
    if parsed is not None and key:
        CONTEXT.put(key, parsed.slots)
    return parsed


# ---------------------------------------------------------------- read-only facade

_WRITE_WORDS = ("set", "patch", "post", "put", "delete", "create", "update", "write",
                "send", "run", "start", "stop", "cancel", "assign", "archive", "rename")


class ReadOnlyFacade:
    """The services a read-only handler may see — and nothing else.

    Only the allowlisted read callables are reachable; any other
    attribute, write-shaped or not, raises AttributeError, so a
    read-only answer path cannot grow a write by accident.
    """

    READ_METHODS = ("spools", "loaded_ids", "firmware_report", "printer_status", "doc_lookup")

    def __init__(self, services):
        object.__setattr__(self, "_services", dict(services or {}))

    def __getattr__(self, name):
        services = object.__getattribute__(self, "_services")
        if name in self.READ_METHODS and name in services:
            return services[name]
        raise AttributeError(f"read-only facade has no {name!r}")

    def __setattr__(self, name, value):
        raise AttributeError("the read-only facade is immutable")


# ---------------------------------------------------------------- inventory engine

NEARLY_EMPTY_GRAMS = 100.0


def filter_spools(records, slots, *, include_archived=False):
    """Structured-field filtering: exact color identity first.

    Returns (matches, tier) where tier is "exact" or "family": the
    family tier runs only when the exact tier found nothing, and the
    caller must label it as a family match.
    """
    rows = [r for r in records if include_archived or not r.get("archived")]

    def base_match(rec):
        if slots.get("brand") and (rec.get("brand") or "").casefold() != slots["brand"]:
            return False
        if slots.get("material") and slots["material"] not in (rec.get("material") or "").casefold():
            return False
        if slots.get("exclude_color") and (rec.get("color") or "").casefold() == slots["exclude_color"]:
            return False
        if slots.get("loaded_only") and not rec.get("loaded"):
            return False
        return True

    rows = [r for r in rows if base_match(r)]
    color = slots.get("color")
    if not color:
        return rows, "exact"
    exact = [r for r in rows if (r.get("color") or "").casefold() == color]
    if exact:
        return exact, "exact"
    family_words = COLOR_FAMILIES.get(color, (color,))
    family = [r for r in rows
              if (r.get("color") or "").casefold() in family_words
              or hex_color_family(r.get("color_hex")) == color]
    return family, "family"


def aggregate(records):
    """(known_total, known_count, unknown_count) over normalized records.

    An unknown remaining weight is never a zero: it is counted apart so
    the answer can say the total is incomplete.
    """
    known = [r for r in records if r.get("remaining") is not None]
    return (sum(r["remaining"] for r in known), len(known), len(records) - len(known))


def _fmt_grams(grams, unit="g"):
    if unit == "kg":
        return f"{grams / 1000:.2f} kg"
    return f"{grams:.0f} g"


def inventory_answer_text(records, slots, *, capability_id="inventory_total"):
    """The factual inventory answer for already-fetched records."""
    slots = dict(slots or {})
    if capability_id == "inventory_nearly_empty":
        records = [r for r in records
                   if r.get("remaining") is not None and r["remaining"] <= NEARLY_EMPTY_GRAMS]
    if capability_id == "inventory_unknown_weights":
        records = [r for r in records if r.get("remaining") is None]
    if capability_id == "inventory_shelf":
        records = [r for r in records if not r.get("loaded")]
    seen, deduped = set(), []
    for rec in records:
        if rec.get("id") in seen:
            continue
        seen.add(rec.get("id"))
        deduped.append(rec)
    matches, tier = filter_spools(deduped, slots)
    describe = " ".join(x for x in (slots.get("brand"), slots.get("color"),
                                    slots.get("material")) if x) or "filament"
    if slots.get("loaded_only"):
        describe += " (loaded)"
    unit = slots.get("unit", "g")
    if not matches:
        return f"You have no {describe} spools recorded in Spoolman."
    total, known, unknown = aggregate(matches)
    lines = []
    if tier == "family":
        lines.append(f"No spool is recorded with the exact color {slots.get('color')!r}; "
                     "counting the broader color family instead:")
    if capability_id == "inventory_count":
        lines.append(f"{len(matches)} spool(s) of {describe}.")
    elif capability_id in ("inventory_list", "inventory_shelf", "inventory_nearly_empty",
                           "inventory_unknown_weights"):
        for rec in sorted(matches, key=lambda r: r.get("id") or 0):
            grams = (_fmt_grams(rec["remaining"], unit) if rec["remaining"] is not None
                     else "weight unknown")
            where = f"slot {rec['slot']}" if rec.get("loaded") and rec.get("slot") else \
                "loaded" if rec.get("loaded") else "shelf"
            label = " ".join(x for x in (rec.get("brand"), rec.get("color"),
                                         rec.get("material")) if x) or "spool"
            lines.append(f"#{rec.get('id')} {label} — {grams} ({where})")
    else:
        lines.append(f"You have {_fmt_grams(total, unit)} of {describe} across "
                     f"{len(matches)} spool(s).")
    if capability_id not in ("inventory_count",):
        if unknown and known:
            lines.append(f"That total is incomplete: {unknown} matching spool(s) have no "
                         "recorded remaining weight.")
        elif unknown and not known:
            lines.append("None of the matching spools has a recorded remaining weight, "
                         "so no total can be given.")
    return "\n".join(lines)


def spool_detail_text(record):
    """One spool, only the fields Spoolman actually recorded."""
    if record is None:
        return "That spool is not in Spoolman."
    parts = [f"Spool #{record.get('id')}"]
    label = " ".join(x for x in (record.get("brand"), record.get("color"),
                                 record.get("material")) if x)
    if label:
        parts.append(label)
    if record.get("remaining") is not None:
        parts.append(f"{record['remaining']:.0f} g remaining")
    else:
        parts.append("remaining weight not recorded")
    if record.get("initial") is not None:
        parts.append(f"started at {record['initial']:.0f} g")
    if record.get("loaded"):
        parts.append(f"loaded in slot {record['slot']}" if record.get("slot") else "loaded")
    elif record.get("location"):
        parts.append(f"location: {record['location']}")
    else:
        parts.append("on the shelf")
    if record.get("archived"):
        parts.append("archived")
    return " — ".join(parts) + "."


# ---------------------------------------------------------------- firmware outcome states

FIRMWARE_STATES = ("update_available", "current", "release_unverified", "unknown",
                   "unsupported", "unavailable", "blocked")

# Family matrix from the issue: how each family's update state is known
# and whether Print Handy may ever apply an update for it.
FIRMWARE_FAMILIES = {
    "zmod": {"source": "Moonraker Update Manager", "applies": True},
    "klipper": {"source": "Moonraker Update Manager", "applies": True},
    "moonraker": {"source": "Moonraker Update Manager", "applies": True},
    "stock_flashforge": {"source": "manual (vendor download)", "applies": False},
    "opencreator": {"source": "upstream release feed only; installed comparison is not verified",
                    "applies": False},
    "forge-x": {"source": "upstream release feed only; installed comparison is not verified",
                "applies": False},
    "bambu": {"source": "installed version only", "applies": False},
    "prusalink": {"source": "reported printer facts only", "applies": False},
    "octoprint": {"source": "reported printer facts only", "applies": False},
    "elegoo": {"source": "reported printer facts only", "applies": False},
}


def classify_firmware_state(family, *, installed=None, available=None, feed_latest=None,
                            reachable=True, apply_blocked_reason=""):
    """One of the seven outcome states — never a false "up to date".

    ``installed``/``available`` are the Update Manager's own comparison;
    ``feed_latest`` is only a published release, which by itself never
    proves the printer is behind.
    """
    family_key = str(family or "").casefold()
    if family_key not in FIRMWARE_FAMILIES:
        return "unsupported"
    if not reachable:
        return "unavailable"
    if apply_blocked_reason:
        return "blocked"
    if installed is not None and available is not None:
        return "update_available" if available != installed else "current"
    if feed_latest is not None:
        return "release_unverified"
    return "unknown"


_STATE_TEXT = {
    "update_available": "an update is confirmed available",
    "current": "verified current — the update manager reports no newer version",
    "release_unverified": "a new release is published, but the installed version could not "
                          "be compared, so it is not known whether this printer needs it",
    "unknown": "the update state is unknown — the version state could not be read",
    "unsupported": "update checks are not supported for this firmware family",
    "unavailable": "the printer is temporarily unavailable, so the update state could not be read",
    "blocked": "an update exists but applying it is blocked right now",
}


def firmware_state_text(component, family, state, *, installed=None, available=None,
                        blocked_reason=""):
    name = component or family or "firmware"
    line = f"{name}: {_STATE_TEXT.get(state, _STATE_TEXT['unknown'])}."
    if installed:
        line += f" Installed: {installed}."
    if available and state == "update_available":
        line += f" Available: {available}."
    if state == "blocked" and blocked_reason:
        line += f" ({blocked_reason})"
    return line


# ---------------------------------------------------------------- route counters, shadow log, unmatched log

_route_counters = {"deterministic": 0, "clarification": 0, "suggestion": 0,
                   "unmatched": 0, "ai_lane": 0, "shadow": 0}
_shadow_disagreements = []
_unmatched_log = []


def record_route(kind):
    if kind in _route_counters:
        _route_counters[kind] += 1


def route_counters():
    """Local-only route counters for diagnostics. Nothing is transmitted."""
    return dict(_route_counters)


_SENSITIVE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://\S+|\b\d{6,}\b")


def sanitize_unmatched(text):
    """Strip addresses, links and long numbers before a phrase is logged."""
    return _SENSITIVE.sub("[redacted]", " ".join(str(text or "").split()))[:200]


def log_unmatched(text, *, enabled=False):
    if not enabled:
        return
    _unmatched_log.append({"phrase": sanitize_unmatched(text), "at": time.time()})
    del _unmatched_log[:-500]


def export_unmatched():
    return [dict(row) for row in _unmatched_log]


def shadow_disagreements():
    return [dict(row) for row in _shadow_disagreements]


# Capabilities whose deterministic answers are live. Everything else the
# parser recognizes stays in shadow until its own tranche flips it.
LIVE_CAPABILITIES = frozenset({
    "inventory_total", "inventory_count", "inventory_list", "spool_detail",
    "loaded_spools", "inventory_shelf", "inventory_nearly_empty",
    "inventory_unknown_weights", "inventory_enough", "purchase_guidance",
    "purchase_record", "spool_weight_guidance", "firmware_version",
    "firmware_updates", "firmware_release_notes", "firmware_howto",
    "troubleshooting", "capability_help", "indx_tool_question", "model_fit_question",
    "printer_status",
})


def _nl_cfg(core):
    try:
        return dict((core.cfg or {}).get("natural_language") or {})
    except Exception:
        return {}


def shadow_mode_for(core, capability_id):
    cfg = _nl_cfg(core)
    if cfg.get("shadow_mode"):
        return True
    return capability_id not in LIVE_CAPABILITIES


# ---------------------------------------------------------------- answer functions

def _fetch_spool_records(core):
    """(records, loaded_ids) from Spoolman via the one client, or raise."""
    from ..runtime import spool_sync
    rows = spool_sync.spoolman("GET", "/api/v1/spool?allow_archived=false", None) or []
    try:
        state = spool_sync.load_state()
        loaded_ids = spool_sync._loaded_spool_ids(state)
    except Exception:
        loaded_ids = set()
    records = [normalize_spool(row, loaded_ids) for row in rows if isinstance(row, dict)]
    return records, loaded_ids


def answer_inventory_total(core, parsed, facade):
    try:
        records, _loaded = _fetch_spool_records(core)
    except Exception:
        return ("Spoolman is unavailable right now, so I can't give you a verified "
                "inventory number.")
    return inventory_answer_text(records, parsed.slots, capability_id=parsed.capability_id)


def answer_spool_detail(core, parsed, facade):
    try:
        records, _loaded = _fetch_spool_records(core)
    except Exception:
        return "Spoolman is unavailable right now, so I can't look that spool up."
    wanted = parsed.slots.get("spool_id")
    record = next((r for r in records if r.get("id") == wanted), None)
    return spool_detail_text(record)


def answer_loaded(core, parsed, facade):
    try:
        records, _loaded = _fetch_spool_records(core)
    except Exception:
        return "Spoolman is unavailable right now, so I can't tell you what's loaded."
    slot = parsed.slots.get("slot")
    loaded = [r for r in records if r.get("loaded")]
    if slot is not None:
        record = next((r for r in loaded if r.get("slot") == slot), None)
        if record is None:
            return f"Slot {slot} has no spool recorded as loaded."
        return spool_detail_text(record)
    if parsed.slots.get("color") or parsed.slots.get("material"):
        merged = dict(parsed.slots)
        merged["loaded_only"] = True
        return inventory_answer_text(records, merged)
    if not loaded:
        return "No spools are recorded as loaded right now."
    lines = ["Loaded now:"]
    for record in sorted(loaded, key=lambda r: (r.get("slot") is None, r.get("slot") or 0)):
        lines.append("- " + spool_detail_text(record))
    return "\n".join(lines)


def answer_enough(core, parsed, facade):
    required = parsed.slots.get("required_grams")
    if not required:
        return ("I can only answer that from a verified sliced requirement. Send the model "
                "or a sliced file and I'll compare its sliced usage against your spools — "
                "I won't guess from the model's bounding box.")
    try:
        records, _loaded = _fetch_spool_records(core)
    except Exception:
        return "Spoolman is unavailable right now, so I can't check your stock."
    matches, _tier = filter_spools(records, parsed.slots)
    total, _known, unknown = aggregate(matches)
    if total >= float(required) and not unknown:
        return f"Yes — you have {_fmt_grams(total)} matching, and the slice needs {required} g."
    if total >= float(required):
        return (f"The known total is {_fmt_grams(total)} against a {required} g slice, but some "
                "matching spools have unknown weights, so the total is incomplete.")
    return f"No — the known matching total is {_fmt_grams(total)} and the slice needs {required} g."


def answer_purchase_guidance(core, parsed, facade):
    return ("Buying guidance: `/print-lookup buying filament` searches for filament. Nothing "
            "is added to your inventory by asking — a purchase is recorded only from a "
            "product link or photo, after the purchase question is answered.")


def answer_purchase_record(core, parsed, facade):
    return ("Noted as a statement, not recorded: I never create inventory from a sentence "
            "like that. Send the product link or a photo of the order and I'll offer to "
            "record the purchase.")


def answer_spool_weight_guidance(core, parsed, facade):
    return ("To correct a recorded weight, weigh the spool and use `/spool weight` with the "
            "spool number and the scale weight — the guided flow confirms before it saves.")


def answer_firmware(core, parsed, facade):
    return firmware_answer(core, parsed)


def firmware_answer(core, parsed):
    """Best-effort firmware answer; unreadable state stays unknown, never current."""
    component = parsed.slots.get("component") or "ZMod"
    family = "zmod"
    try:
        from ..runtime import spool_sync
        from .. import firmware_update
        state = spool_sync.load_state()
        conn = spool_sync.moonraker_for(state=state)
        if conn is None:
            raise LookupError("no Moonraker connection")
        report = firmware_update.firmware_report(conn[0], conn[1], conn[2])
        entry = next((c for c in report.get("components") or []
                      if component.casefold() in str(c.get("name", "")).casefold()), None)
        if entry is None:
            entry = (report.get("components") or [{}])[0]
        installed = entry.get("installed_version") or entry.get("version")
        available = entry.get("available_version") or entry.get("remote_version")
        status = str(entry.get("status") or "")
        fw_state = classify_firmware_state(
            family, installed=installed, available=available,
            apply_blocked_reason="the printer is busy"
            if status in ("printing", "paused") and parsed.capability_id == "firmware_update_apply"
            else "")
        return firmware_state_text(entry.get("name") or component, family, fw_state,
                                   installed=installed, available=available)
    except Exception:
        return firmware_state_text(component, family, "unknown")


def status_answer_text(st):
    """A plain-language status answer from the family's normalized status.

    Only fields the read actually returned are named; a state that
    could not be read says so instead of inventing an idle printer.
    """
    if not st:
        return "I couldn't read the printer's status just now."
    state = str(st.get("state") or "unknown")
    parts = [f"The printer is {state}."]
    if st.get("filename"):
        parts.append(f"Printing {st['filename']}.")
    progress = st.get("progress")
    if isinstance(progress, (int, float)) and state in ("printing", "paused"):
        parts.append(f"{progress * 100:.0f}% complete.")
    if st.get("layer") and st.get("total_layer"):
        parts.append(f"Layer {st['layer']} of {st['total_layer']}.")
    remaining = st.get("remaining_min")
    if isinstance(remaining, (int, float)) and state == "printing":
        parts.append(f"About {remaining:.0f} minutes left.")
    temps = []
    if isinstance(st.get("hotend_c"), (int, float)):
        temps.append(f"nozzle {st['hotend_c']:.0f}°C")
    if isinstance(st.get("bed_c"), (int, float)):
        temps.append(f"bed {st['bed_c']:.0f}°C")
    if temps:
        parts.append("Temperatures: " + ", ".join(temps) + ".")
    return " ".join(parts)


def answer_status(core, parsed, facade):
    if parsed.capability_id != "printer_status":
        # History, camera and maintenance keep their existing handlers;
        # the corpus pins their classification here.
        return None
    try:
        from ..runtime import print_stop
        return status_answer_text(print_stop.status())
    except Exception:
        return "I couldn't read the printer's status just now."


def answer_troubleshooting(core, parsed, facade):
    text = parsed.text.casefold()
    if "first layer" in text or "won't stick" in text or "not sticking" in text:
        return ("First-layer adhesion checklist: clean the bed, check the first-layer height "
                "(z-offset), and slow the first layer down. `/print-troubleshoot` walks "
                "through it with your printer's specifics.")
    if "click" in text:
        return ("Extruder clicking usually means the filament path is resisting: check for a "
                "partial clog, a too-low nozzle temperature, or a kinked path. "
                "`/print-troubleshoot` has the guided checks.")
    return ("`/print-troubleshoot` has guided checks for that symptom — it asks about your "
            "printer and the print before suggesting a fix.")


def answer_capability_help(core, parsed, facade):
    return help_text()


def answer_indx(core, parsed, facade):
    return ("INDX tool questions are registered in the capability list, but INDX printer "
            "support itself is not built yet (#1198), so I can't answer that honestly yet.")


def answer_fit(core, parsed, facade):
    return ("Fit is checked from the model's real geometry when you send the file — drop the "
            "model here and I'll measure it against your printer's bed instead of guessing.")


def help_text():
    """'What can you do', generated from the registry's example phrases."""
    lines = ["Here's what you can ask me, in plain words:"]
    for cap in CAPABILITIES:
        if cap.examples:
            lines.append(f"- {cap.description}: \"{cap.examples[0]}\"")
    return "\n".join(lines)


# ---------------------------------------------------------------- the fallback hook

def answer(core, event, metadata):
    """BotCore's deterministic NL hook: a reply string, or None.

    Runs before the conversational lane and only ever answers read-only
    capabilities (or, in shadow, logs a disagreement and answers
    nothing). Mutating intents are never executed here — the existing
    deterministic handlers and their confirmations own them.
    """
    cfg = _nl_cfg(core)
    key = None
    try:
        from .runtime import session_key
        key = session_key(event.source)
    except Exception:
        key = None
    vocabulary = None
    try:
        records, _loaded = _fetch_spool_records(core)
        vocabulary = build_vocabulary(records)
    except Exception:
        vocabulary = build_vocabulary()
    parsed = parse_with_context(event.text, key, vocabulary)
    if parsed is None:
        record_route("unmatched")
        log_unmatched(event.text, enabled=bool(cfg.get("unmatched_log_enabled")))
        return None
    effective = parsed.routed_to or parsed.capability_id
    if parsed.blocked:
        record_route("clarification")
    if shadow_mode_for(core, effective):
        _shadow_disagreements.append({
            "text": sanitize_unmatched(event.text), "intent": effective,
            "slots": dict(parsed.slots), "at": time.time()})
        del _shadow_disagreements[:-200]
        record_route("shadow")
        return None
    cap = capability(effective)
    if cap is None or cap.effect != READ_ONLY or not cap.handler:
        return None
    handler = globals().get(cap.handler)
    if handler is None:
        return None
    facade = ReadOnlyFacade({})
    result = handler(core, parsed, facade)
    if result:
        record_route("deterministic")
    return result
