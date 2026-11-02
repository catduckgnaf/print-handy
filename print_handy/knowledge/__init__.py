"""Lookup and guided troubleshooting over the root-level registries (issue #169).

    from print_handy import knowledge
    knowledge.lookup_text("zmod ifs load")
    session = knowledge.start_flow("clog")

Read-only and advisory by construction: this package loads TOML data files,
searches them and walks flow graphs. It has no printer client, and by default
it sends nothing. The one exception is opt-in: with PRINT_FUZZY_URL set, a
lookup that misses locally sends the query text and the registries' own entry
names to the operator's semantic endpoint (#468 stage 4, core.fuzzy_semantic)
— never per-install data — and any failure falls back to the local answer.
"""
from __future__ import annotations

from functools import lru_cache

from .flows import Flow, FlowError, FlowSession, Node, Step
from .registry import (ROOT, FirmwareEntry, Match, PrinterEntry, Registry,
                       RegistryError, Symptom, ThermalCapabilities, _norm, load_registry)
from .thermal import (thermal_assumptions_lines, thermal_entry_for_model,
                      thermal_lines_for_model, thermal_setup_questions,
                      unknown_thermal_fields)

__all__ = [
    "ROOT", "Flow", "FlowError", "FlowSession", "Node", "Step", "FirmwareEntry",
    "Match", "PrinterEntry", "Registry", "RegistryError", "Symptom", "ThermalCapabilities",
    "default_registry", "load_registry", "lookup_text", "flows_text", "start_flow",
    "thermal_assumptions_lines", "thermal_entry_for_model", "thermal_lines_for_model",
    "thermal_setup_questions", "unknown_thermal_fields",
]

# Shown with every lookup result and at the end of every guided flow, so the
# boundary is in the message and not just in the docs.
ADVISORY_NOTE = ("Advisory only: nothing was sent to the printer. Acting on this is a person at the "
                 "printer, or one of the guarded commands that asks first.")


@lru_cache(maxsize=1)
def default_registry() -> Registry:
    return load_registry()


def start_flow(flow_id, registry=None) -> FlowSession:
    registry = registry or default_registry()
    flow = registry.flow(flow_id)
    if flow is None:
        known = ", ".join(f.id for f in registry.flows)
        raise FlowError(f"unknown troubleshooting flow {flow_id!r}; try one of: {known}")
    return FlowSession(flow)


def _format_match(match: Match) -> str:
    item = match.item
    if match.kind == "firmware":
        lines = [f"**{item.name}** ({item.firmware}, {item.kind})"]
        if item.command:
            lines.append(f"`{item.command}`")
        lines.append(item.description)
        lines.append(f"Safety: {item.safety}")
        if item.notes:
            lines.append(item.notes)
        return "\n".join(lines)
    if match.kind == "symptom":
        lines = [f"**Symptom: {item.title}**", item.summary]
        lines += [f"- First check: {check}" for check in item.first_checks]
        if item.flow:
            lines.append(f"Guided flow: `/print-troubleshoot {item.flow}`")
        return "\n".join(lines)
    if match.kind == "flow":
        return (f"**Guided flow: {item.title}**\n"
                f"Step-by-step, with safety warnings first: `/print-troubleshoot {item.id}`")
    positions = f"{item.tools} independent toolhead(s)" if item.tools else f"{item.filament_slots} filament slot(s)"
    return (f"**Printer: {item.label}** (`{item.key}`)\n"
            f"{item.firmware} over {item.connection}; bed {item.bed_mm[0]:g} x {item.bed_mm[1]:g} mm, "
            f"height {item.max_height_mm:g} mm, {positions}. {item.notes}")


def lookup_text(query, *, firmware=None, printer=None, pack=None, registry=None) -> str:
    """The Discord-ready answer for one lookup. Always ends with the advisory note."""
    query = str(query or "").strip()
    if not query:
        return ("What should I look up? Try a command (`/print-lookup M600`), a firmware topic "
                "(`/print-lookup bed mesh klipper`), a symptom (`/print-lookup first layer not sticking`) "
                "or a printer (`/print-lookup ad5x`).\n" + ADVISORY_NOTE)
    registry = registry or default_registry()
    results = registry.lookup(query, firmware=firmware, printer=printer, pack=pack)
    if not results:
        semantic = _semantic_lookup(query, registry, firmware=firmware, printer=printer, pack=pack)
        if semantic:
            body = "\n\n".join(_format_match(match) for match in semantic)
            return (f"No local match for {query!r}. Closest by meaning, from the semantic endpoint "
                    f"this install configured (PRINT_FUZZY_URL):\n\n{body}\n\n" + ADVISORY_NOTE)
        return (f"Nothing in the registries matches {query!r}. Try the command or macro name, a symptom "
                f"in plain words, or one of the guided flows:\n{flows_text(registry)}\n" + ADVISORY_NOTE)
    body = "\n\n".join(_format_match(match) for match in results)
    return body + "\n\n" + ADVISORY_NOTE


def _semantic_lookup(query, registry, *, firmware=None, printer=None, pack=None):
    """#468 stage 4: meaning-level matches from the operator's PRINT_FUZZY_URL endpoint.

    Consulted only when the local lookup found nothing — local results are
    never merged with or re-ranked by this layer. Candidates are the
    registries' own public entries (id + the display text a person would
    type), filtered exactly as ``Registry.lookup`` filters them; per-install
    data such as print-history titles is never a candidate. Returns Match
    objects in the client's enforced score order (best first), scores
    rescaled to the local 0–100 display scale. Any failure or an unset
    URL returns [].
    """
    from ..core import fuzzy_semantic

    if not fuzzy_semantic.configured():
        return []
    candidates, by_id = [], {}

    def add(kind, item, ident, text):
        cid = f"{kind}:{ident}"
        candidates.append({"id": cid, "text": text})
        by_id[cid] = (kind, item)

    firmware_norm = _norm(firmware).replace(" ", "") or None
    pack_norm = _norm(pack).replace(" ", "-") if pack is not None else None
    for entry in registry.firmware_entries:
        if firmware_norm and entry.firmware != firmware_norm:
            continue
        if printer and "*" not in entry.printers and printer not in entry.printers:
            continue
        if pack_norm is not None and entry.pack != pack_norm:
            continue
        add("firmware", entry, entry.id, entry.name)
    for symptom in registry.symptoms:
        add("symptom", symptom, symptom.id, symptom.title)
    for flow in registry.flows:
        add("flow", flow, flow.id, flow.title)
    for entry in registry.printers:
        add("printer", entry, entry.key, entry.label)
    matches = []
    for hit in fuzzy_semantic.semantic_matches(query, candidates):
        found = by_id.get(hit["id"])
        if found is None:
            continue  # an id the endpoint invented or mangled: not a registry entry, not shown
        kind, item = found
        matches.append(Match(kind, round(hit["score"] * 100), item))
    return matches


def flows_text(registry=None) -> str:
    registry = registry or default_registry()
    lines = ["Guided physical troubleshooting (step-by-step, read-only advice):"]
    lines += [f"- `/print-troubleshoot {flow.id}`: {flow.title}" for flow in registry.flows]
    return "\n".join(lines)
