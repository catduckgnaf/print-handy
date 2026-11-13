"""The spool chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, re, time

from .common import (
    _ask_which_printer, _confirm, _control_gate, _control_preamble, _picker_label, _pkw,
    _printer_label, _prompt_brand, _routed_form, _routed_printer_id, _seen, _spool_timeout,
    _undecided_printer_ids,
)

# The logger name stays the one the code had before the move, so log
# routing and filters see the same records.
logger = logging.getLogger("print_handy.bot.handlers")


_SPOOL_CANCELLED = 'Cancelled. Nothing changed.'
_SPOOL_UNCLEAR = 'That answer was not clear. Nothing changed.'
_SPOOL_MENU_NEW = 'Add a new spool'
_SPOOL_MENU_CHANGE = 'Change a spool'
_SPOOL_MENU_LIST = 'List spools'
_SPOOL_DISPOSITIONS = {'Back on the shelf': 'shelf', 'Archive it': 'archive',
                       'Mark it as depleted': 'depleted', 'Archive as depleted': 'depleted',
                       'Delete it': 'delete'}

_SPOOL_NL_NEW_RES = (
    re.compile(r"(?:please\s+)?add(?:ing)?\s+(?:a\s+|an\s+|some\s+)?(?:new\s+)?(?:filament|spool|roll)\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+to\s+add\s+(?:a\s+|an\s+|some\s+)?(?:new\s+)?(?:filament|spool|roll)\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+(?:a\s+)?new\s+(?:filament|spool|roll)\s*[.!]?", re.I),
    re.compile(r"new\s+(?:filament|spool|roll)\s*[.!]?", re.I),
)
_SPOOL_NL_CHANGE_RES = (
    re.compile(r"(?:please\s+)?(?:change|swap|replace)\s+(?:the\s+|a\s+|an\s+)?(?:filament|spool|roll)(?:\s+(?:roll|spool))?(?:\s+in\s+slot\s+[1-4])?\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+to\s+(?:change|swap|replace)\s+(?:the\s+|a\s+|an\s+)?(?:filament|spool|roll)(?:\s+(?:roll|spool))?\s*[.!]?", re.I),
    re.compile(r"(?:please\s+)?(?:change|swap|replace)\s+(?:the\s+|a\s+|an\s+)?filament\s+roll\s*[.!]?", re.I),
    re.compile(r"(?:we|i)\s+(?:need|want)\s+to\s+change\s+a\s+filament\s+roll\s*[.!]?", re.I),
)


def _spool_nl_intent(text):
    """Deterministic plain-text spool requests (#475): 'new' or 'change', else None.

    Anchored full-message patterns in the style of 'print 57' (#436), not
    loose substring matching: 'add new filament', 'we need to add new
    filament', 'new spool', 'swap the filament'. Discussion ('the filament
    is tangled', 'add new filament to my shopping list', questions about
    filament) never matches, and this runs with no LLM configured.
    """
    text = ' '.join(str(text or '').split()).strip()
    if not text or text.startswith('/'):
        return None
    if any(pattern.fullmatch(text) for pattern in _SPOOL_NL_NEW_RES):
        return 'new'
    if any(pattern.fullmatch(text) for pattern in _SPOOL_NL_CHANGE_RES):
        return 'change'
    return None


async def _spool_ask(adapter, source, metadata, gateway, question, choices):
    """One guided /spool question through the shared button prompt (_confirm)."""
    return await _confirm(adapter, source, metadata, 'spool', question, list(choices),
                          _spool_timeout(), live_key=gateway._session_key_for_source(source))


def _spool_is_cancel(answer):
    if answer is None or not isinstance(answer, str):
        return True
    return answer.strip().casefold() in ('cancel', '__cancelled__')


def _spool_type_label(filament_type):
    """A filament type in the picker shape (#792 part 2): 'color · material · brand'.

    A type is not a spool, so it has no spool number or grams to show;
    color and material lead, in the same order and separator the spool
    pickers use, with the brand last.
    """
    parts = [str(filament_type.get('color') or '').strip(),
             str(filament_type.get('material') or '').strip(),
             str(filament_type.get('brand') or '').strip()]
    return ' · '.join(x for x in parts if x)


def _spool_spec_text(spec):
    head = ' '.join(x for x in (spec.get('brand'), spec.get('material')) if x)
    return f"{head}, {spec['color']}" if head else str(spec['color'])


async def _spool_slot_ask(adapter, source, metadata, gateway, spool_sync, state, lead):
    """Ask which slot, showing what each slot holds. (slot, None) or (None, final message)."""
    slots = (state or {}).get('filament_slots') or {}
    lines = [lead]
    for slot in spool_sync.SLOTS:
        rec = slots.get(str(slot)) or {}
        sid = rec.get('spoolman_id')
        if not sid:
            lines.append(f"Slot {slot}: empty")
        elif rec.get('color') and rec.get('material'):
            # A fully recorded slot names its spool in the one picker shape.
            from print_handy.runtime.discord_print_summary import spool_picker_label
            lines.append(f"Slot {slot}: {spool_picker_label(rec['color'], rec['material'], sid, rec.get('remaining_grams'))}")
        else:
            lines.append(f"Slot {slot}: #{sid} {rec.get('name') or ''}".rstrip())
    answer = await _spool_ask(adapter, source, metadata, gateway, '\n'.join(lines),
                              [f'Slot {slot}' for slot in spool_sync.SLOTS])
    if _spool_is_cancel(answer):
        return None, _SPOOL_CANCELLED
    match = re.fullmatch(r'(?:slot\s*)?([1-4])', str(answer).strip().casefold())
    if not match:
        return None, _SPOOL_UNCLEAR
    return int(match.group(1)), None


async def _spool_disposition_ask(adapter, source, metadata, gateway, slot, old_id, old_remaining=None):
    """The old spool's fate, as buttons. (disposition, None) or (None, final message).

    Delete is offered alongside shelf/archive/depleted (#512 §3); when the
    old spool has nothing left, Archive as depleted leads and says why.
    """
    empty = isinstance(old_remaining, (int, float)) and old_remaining <= 0
    if empty:
        choices = ['Archive as depleted', 'Back on the shelf', 'Archive it', 'Delete it', 'Cancel']
        question = (f"Slot {slot} still holds spool #{old_id} (0 g left). What happens to spool #{old_id}? "
                    f"Archive as depleted is the recommended choice for an empty spool.")
    else:
        choices = ['Back on the shelf', 'Archive it', 'Mark it as depleted', 'Delete it', 'Cancel']
        question = f"Slot {slot} still holds spool #{old_id}. What happens to spool #{old_id}?"
    answer = await _spool_ask(adapter, source, metadata, gateway, question, choices)
    if _spool_is_cancel(answer):
        return None, _SPOOL_CANCELLED
    disposition = _SPOOL_DISPOSITIONS.get(str(answer).strip())
    return (disposition, None) if disposition else (None, _SPOOL_UNCLEAR)


async def _spool_new_type_details(adapter, source, metadata, gateway, lead=''):
    """Brand / material / colour for a filament type the database does not know.

    Returns a spec dict, or a final message string when the flow ends.
    Hex is never asked and never required (#475). Brand is asked only when
    spool.prompt_brand is on (#512): off by default, the flow goes straight
    to material and the spool is created with no brand. ``lead`` prefixes the
    first question (the early spool-number preview, #512 §2).
    """
    brand = None
    if _prompt_brand():
        answer = await _spool_ask(adapter, source, metadata, gateway,
                                  (lead + '\n' if lead else '')
                                  + 'What brand is the new filament? Type the brand name, or choose Skip brand.',
                                  ['Skip brand', 'Cancel'])
        if _spool_is_cancel(answer):
            return _SPOOL_CANCELLED
        brand = None if str(answer).strip().casefold() in ('skip brand', 'skip') else str(answer).strip()
        if brand is not None and not brand:
            return _SPOOL_UNCLEAR
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              (lead + '\n' if lead and not _prompt_brand() else '')
                              + 'What material is it? Pick one, or type another material.',
                              ['PLA', 'Silk PLA', 'PLA+', 'PETG', 'TPU', 'ABS', 'Cancel'])
    if _spool_is_cancel(answer):
        return _SPOOL_CANCELLED
    material = str(answer).strip()
    if not material:
        return _SPOOL_UNCLEAR
    while True:
        answer = await _spool_ask(adapter, source, metadata, gateway,
                                  'What is the color called? Type the color name — no color code is needed.',
                                  ['Cancel'])
        if _spool_is_cancel(answer):
            return _SPOOL_CANCELLED
        color = str(answer).strip()
        if not color:
            return _SPOOL_UNCLEAR
        # Parsed-color sanity (#792 part 2): a color that parses to
        # multiple colors or carries unrecognized fragment words gets a
        # confirm-or-correct step naming what was understood, instead of
        # minting a spool record with a possible garble baked in.
        from print_handy.core import filament_color
        sanity_text = filament_color.color_sanity_text(color, material)
        if sanity_text is None:
            break
        answer = await _spool_ask(adapter, source, metadata, gateway,
                                  sanity_text + ' Is that exactly as labeled, or do you want to type it again?',
                                  ['That is exactly as labeled', 'Type it again', 'Cancel'])
        if _spool_is_cancel(answer):
            return _SPOOL_CANCELLED
        if str(answer).strip() == 'That is exactly as labeled':
            break
        if str(answer).strip() != 'Type it again':
            return _SPOOL_UNCLEAR
    return {'brand': brand, 'material': material, 'color': color, 'hex': None}


async def _spool_grams_ask(adapter, source, metadata, gateway):
    """Full-spool grams: a preset button or a typed positive number. A float, or a final message."""
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              'How many grams is a full spool? Pick one, or type the weight in grams.',
                              ['1000', '500', '250', 'Cancel'])
    if _spool_is_cancel(answer):
        return _SPOOL_CANCELLED
    try:
        grams = float(re.sub(r'[^0-9.]', '', str(answer)))
    except ValueError:
        return _SPOOL_UNCLEAR
    if grams <= 0:
        return _SPOOL_UNCLEAR
    return round(grams, 1)


async def _spool_guided_new(adapter, source, metadata, gateway, spool_sync, pid, mr_kw, *, offer_load=True):
    """Walk through adding a spool (#475). Returns (created spool id or None, final message).

    Database first: a known filament type supplies brand/material/colour/hex,
    so no colour or hex question follows. Nothing is created before the
    confirmation button, and no spool number is named before creation
    (#1063: a predicted id can be wrong by creation time — the actual
    number is announced in the result message the moment it exists).
    """
    types = await asyncio.to_thread(spool_sync.filament_types)
    preview = ("Preparing a new spool — nothing is created until you confirm, and I will "
               "announce its actual number the moment it is created.")
    spec = None
    if types:
        listed = types[:8]
        lines = [preview,
                 'Which filament is the new spool? Pick a type already in the database, '
                 'or choose New filament type.']
        lines += [f"{i}. {_spool_type_label(t)}" for i, t in enumerate(listed, 1)]
        lines.append(f"{len(listed) + 1}. New filament type")
        # The registered choices must be the numbered list itself, in order
        # (P2-182): buttonless adapters translate a typed number through
        # this list, so registering only the first three types made the
        # displayed fourth type open new-type entry instead.
        choices = [_spool_type_label(t) for t in listed] + ['New filament type']
        answer = await _spool_ask(adapter, source, metadata, gateway, '\n'.join(lines), choices)
        if _spool_is_cancel(answer):
            return None, _SPOOL_CANCELLED
        text = str(answer).strip()
        pick = None
        if text.casefold() in ('new', 'new filament type'):
            pick = 'new'
        elif text.isdigit() and 1 <= int(text) <= len(listed):
            pick = listed[int(text) - 1]
        else:
            pick = next((t for t in listed if text.casefold() == _spool_type_label(t).casefold()), None)
        if pick is None:
            return None, _SPOOL_UNCLEAR
        if pick != 'new':
            spec = {'brand': pick.get('brand'), 'material': pick['material'],
                    'color': pick['color'], 'hex': pick.get('hex')}
    if spec is None:
        details = await _spool_new_type_details(adapter, source, metadata, gateway,
                                                lead='' if types else preview)
        if isinstance(details, str):
            return None, details
        spec = details
    grams = await _spool_grams_ask(adapter, source, metadata, gateway)
    if isinstance(grams, str):
        return None, grams
    spec = {**spec, 'grams': grams}
    create_label = 'Create spool'
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"This creates a new spool — {_spool_spec_text(spec)}, "
                              f"{grams:g} g, on the shelf. Create it?",
                              [create_label, 'Cancel'])
    if answer != create_label:
        return None, _SPOOL_CANCELLED if _spool_is_cancel(answer) else _SPOOL_UNCLEAR
    made = await asyncio.to_thread(spool_sync.create_spool, spec)
    made_id = int(made['spool_id'])
    message = (f"Created spool #{made_id} on the shelf"
               f"{' (existing filament type reused)' if made.get('reused_filament') else ''}.")
    if not offer_load:
        return made_id, message
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"Load spool #{made_id} into a slot now, or leave it on the shelf?",
                              ['Load it into a slot', 'Leave it on the shelf'])
    if answer != 'Load it into a slot':
        return made_id, message
    state = await asyncio.to_thread(spool_sync.load_state, **_pkw(pid))
    slot, problem = await _spool_slot_ask(adapter, source, metadata, gateway, spool_sync, state,
                                          f"Which slot should spool #{made_id} go into?")
    if slot is None:
        return made_id, f"{message} Nothing was loaded; no slot changed."
    old_id = ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('spoolman_id')
    disposition = 'shelf'
    if old_id and int(old_id) != made_id:
        disposition, problem = await _spool_disposition_ask(
            adapter, source, metadata, gateway, slot, old_id,
            ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('remaining_grams'))
        if disposition is None:
            return made_id, f"{message} Nothing was loaded; slot {slot} is unchanged."
    # One confirmation naming the exact spool before the bind (#792 part 2):
    # the load used to happen on the slot answer alone.
    fate = {'shelf': f" Spool #{old_id} goes back on the shelf.",
            'archive': f" Spool #{old_id} will be archived.",
            'depleted': f" Spool #{old_id} will be marked as depleted (0 g left).",
            'delete': f" Spool #{old_id} will be DELETED from Spoolman."}.get(disposition, '') if old_id else ''
    from print_handy.runtime.discord_print_summary import spool_picker_label
    label = spool_picker_label(spec.get('color'), spec.get('material'), made_id, spec.get('grams'))
    load_label = f'Load spool #{made_id}'
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"Load spool {label} into slot {slot}?{fate}", [load_label, 'Cancel'])
    if answer != load_label:
        return made_id, (f"{message} Nothing was loaded; slot {slot} is unchanged."
                         if _spool_is_cancel(answer) else f"{message} {_SPOOL_UNCLEAR}")
    result = await asyncio.to_thread(spool_sync.bind_slot, slot, made_id,
                                     prior_disposition=disposition,
                                     expected_prior_spool_id=int(old_id) if old_id else None,
                                     **mr_kw)
    return made_id, f"{message} {spool_sync.bind_text(result)}"


async def _spool_guided_change(adapter, source, metadata, gateway, spool_sync, pid, mr_kw):
    """Walk through changing a slot's spool (#475): slot -> replacement ->
    the old spool's fate -> one confirmation -> bind_slot."""
    state = await asyncio.to_thread(spool_sync.load_state, **_pkw(pid))
    slot, problem = await _spool_slot_ask(adapter, source, metadata, gateway, spool_sync, state,
                                          'Which slot are you changing?')
    if slot is None:
        return problem
    old_id = ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('spoolman_id')
    shelf = await asyncio.to_thread(spool_sync.shelf_spools, **_pkw(pid))
    listed = shelf[:12]
    lines = [f"Which spool goes into slot {slot}? Pick one from the shelf, or choose Add a new spool."]
    if listed:
        for i, entry in enumerate(listed, 1):
            lines.append(f"{i}. {_picker_label(entry)}")
    else:
        lines.append('(nothing on the shelf)')
    if listed:
        lines.append(f"{len(listed) + 1}. Add a new spool")
    # Register every listed spool with its FULL label (P2-182, P2-183):
    # adapters truncate only the displayed button text and resolve a click
    # back to the registered value, so a [:80] truncation here — or offering
    # only the first three — made clicks and typed numbers land on the
    # wrong entry or on no entry at all. The label is the one picker shape
    # (#792 part 2): color · material · #id · grams left (or grams unknown).
    choices = [_picker_label(entry) for entry in listed] + ['Add a new spool']
    if not listed:
        choices = ['Add a new spool', 'Cancel']
    answer = await _spool_ask(adapter, source, metadata, gateway, '\n'.join(lines), choices)
    if _spool_is_cancel(answer):
        return _SPOOL_CANCELLED
    text = str(answer).strip()
    created_note = ''
    new_id = None
    if text.casefold() == 'add a new spool':
        made_id, made_message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                        spool_sync, pid, mr_kw, offer_load=False)
        if made_id is None:
            return made_message
        new_id = made_id
        created_note = made_message + ' '
    else:
        if text.startswith('#') and text[1:].isdigit():
            wanted = int(text[1:])
            new_id = wanted if any(entry['spool_id'] == wanted for entry in shelf) else None
        elif text.isdigit():
            number = int(text)
            if 1 <= number <= len(listed):
                new_id = listed[number - 1]['spool_id']
            elif any(entry['spool_id'] == number for entry in shelf):
                new_id = number
        else:
            # The registered picker label, or the pre-#792 '#id name' form
            # a typed answer may still use.
            pick = next((entry for entry in listed
                         if text.casefold() in (_picker_label(entry).casefold(),
                                                f"#{entry['spool_id']} {entry['name']}".casefold())), None)
            new_id = pick['spool_id'] if pick else None
        if new_id is None:
            return _SPOOL_UNCLEAR
    if old_id and int(old_id) == int(new_id):
        return f"{created_note}Slot {slot} already holds spool #{new_id}. Nothing changed."
    disposition = 'shelf'
    if old_id:
        disposition, problem = await _spool_disposition_ask(
            adapter, source, metadata, gateway, slot, old_id,
            ((state.get('filament_slots') or {}).get(str(slot)) or {}).get('remaining_grams'))
        if disposition is None:
            return (f"{created_note}The change was cancelled; slot {slot} is unchanged."
                    if created_note else problem)
    fate = {'shelf': f" Spool #{old_id} goes back on the shelf.",
            'archive': f" Spool #{old_id} will be archived.",
            'depleted': f" Spool #{old_id} will be marked as depleted (0 g left).",
            'delete': f" Spool #{old_id} will be DELETED from Spoolman."}.get(disposition, '') if old_id else ''
    # The confirmation names the exact spool (#792 part 2): number, color,
    # material and remaining — not just the number the picker carried.
    picked = next((entry for entry in listed if entry['spool_id'] == int(new_id)), None)
    if picked is not None:
        exact = _picker_label(picked)
    elif created_note:
        # A spool created inside this flow is on the shelf now: name it
        # exactly too, when the shelf can be re-read.
        try:
            fresh = await asyncio.to_thread(spool_sync.shelf_spools, **_pkw(pid))
        except Exception:
            fresh = []
        made = next((entry for entry in fresh if entry['spool_id'] == int(new_id)), None)
        exact = _picker_label(made) if made is not None else f"#{new_id}"
    else:
        exact = f"#{new_id}"
    confirm_label = f'Change slot {slot}'
    answer = await _spool_ask(adapter, source, metadata, gateway,
                              f"Change slot {slot} to spool {exact}?{fate}", [confirm_label, 'Cancel'])
    if answer != confirm_label:
        return (f"{created_note}The change was cancelled; slot {slot} is unchanged."
                if created_note else
                _SPOOL_CANCELLED if _spool_is_cancel(answer) else _SPOOL_UNCLEAR)
    result = await asyncio.to_thread(spool_sync.bind_slot, slot, int(new_id),
                                     prior_disposition=disposition,
                                     expected_prior_spool_id=int(old_id) if old_id else None,
                                     **mr_kw)
    return created_note + spool_sync.bind_text(result)


async def spool_dispatch(event, gateway, **kwargs):
    """/spool: bare, incomplete or natural-language forms open the guided flow (#475);
    the complete typed forms (list | change <slot> <spool#> | new brand, material,
    color, grams[, hex] | sync) keep their one-shot behaviour. Never moves the printer."""
    text = str(getattr(event, 'text', '')).strip()
    nl_intent = None if text.startswith('/spool') else _spool_nl_intent(text)
    if nl_intent is not None:
        # The plain-text twin of /spool (#475, in the style of 'print 57'):
        # same gate, same guided flow — never the LLM and never the help dump.
        source, adapter = _control_gate(event, gateway)
    else:
        source, adapter = _control_preamble(event, gateway, '/spool', with_args=True)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'spool UI delivery unavailable'}
    from print_handy.runtime import spool_sync
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    pid = _routed_printer_id(source, gateway)
    # A routed channel's spool state and screen pushes are that printer's:
    # its hardware record and its own Moonraker (#335). Spoolman itself is shared.
    mr_kw = {'mr': spool_sync.moonraker_for(pid), 'printer_id': pid} if pid else {}
    args = text[len('/spool'):].strip() if nl_intent is None else ''
    verb, _, rest = args.partition(' ')
    verb = verb.casefold()
    # Decide up front whether this entry needs the guided flow, so the
    # already-open guard covers exactly the button conversations.
    guided = nl_intent is not None or verb == ''
    change_parts = rest.replace('#', ' ').split() if verb in ('change', 'set', 'load') else []
    change_complete = len(change_parts) == 2 and all(p.isdigit() for p in change_parts)
    new_spec = None
    if verb in ('new', 'add'):
        try:
            new_spec = spool_sync.parse_new(rest)
        except Exception:
            new_spec = None
        guided = new_spec is None
    if verb in ('change', 'set', 'load'):
        guided = not change_complete
    # Shared channel (#536): slot binds and screen syncs are printer-bound
    # and ask first; making a shelf spool and listing inventory are global.
    candidates = _undecided_printer_ids(source, gateway) if not pid else None
    decided = {'done': False}

    async def _spool_printer():
        """Decide the printer for a printer-bound spool form, once."""
        nonlocal pid, mr_kw
        if decided['done']:
            return True
        decided['done'] = True
        if not candidates:
            return True
        chosen = await _ask_which_printer(source, gateway, adapter, candidates,
                                          question='Which printer are the spools for?',
                                          metadata=metadata)
        if chosen is None:
            return False
        pid = _routed_form(chosen)
        mr_kw = {'mr': spool_sync.moonraker_for(pid), 'printer_id': pid} if pid else {}
        return True

    async def _spool_list_message():
        if candidates:
            # No silent default: a shared channel's list names each
            # printer's loaded slots separately.
            sections = []
            for candidate in candidates:
                body = await asyncio.to_thread(spool_sync.list_text, printer_id=candidate)
                sections.append(f'**{_printer_label(candidate)}**\n{body}')
            return '\n\n'.join(sections)
        return await asyncio.to_thread(spool_sync.list_text, **_pkw(pid))

    key = ('spool', source.chat_id, source.thread_id)
    if guided:
        if key in _seen:
            await adapter.send(chat_id=source.chat_id, metadata=metadata,
                               content='A spool question is already open here. Nothing changed.')
            return {'action': 'skip', 'reason': 'spool flow already active'}
        _seen[key] = time.monotonic()
    try:
        if nl_intent == 'new':
            if await _spool_printer():
                _made, message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                         spool_sync, pid, mr_kw)
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif nl_intent == 'change':
            if await _spool_printer():
                message = await _spool_guided_change(adapter, source, metadata, gateway,
                                                     spool_sync, pid, mr_kw)
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif verb in ('list', 'ls'):
            message = await _spool_list_message()
        elif verb in ('change', 'set', 'load'):
            if not await _spool_printer():
                message = 'No printer chosen, so nothing was changed.'
            elif change_complete:
                result = await asyncio.to_thread(spool_sync.bind_slot, int(change_parts[0]),
                                                 int(change_parts[1]), **mr_kw)
                message = spool_sync.bind_text(result)
            else:
                message = await _spool_guided_change(adapter, source, metadata, gateway,
                                                     spool_sync, pid, mr_kw)
        elif verb in ('new', 'add'):
            if new_spec is not None:
                # Parsed-color sanity (#792 part 2): the one-shot form has
                # no confirmation step, so a color that parses to multiple
                # colors or a fragment is not minted from it — the guided
                # form asks the confirm-or-correct step instead.
                from print_handy.core import filament_color
                sanity_text = filament_color.color_sanity_text(new_spec.get('color'),
                                                               new_spec.get('material'))
                if sanity_text is not None:
                    message = (f"{sanity_text} Nothing was created. Run `/spool new` and confirm "
                               f"the color there, or retype the color exactly as labeled.")
                    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
                    return {'action': 'skip', 'reason': 'spool new color needs confirmation'}
                made = await asyncio.to_thread(spool_sync.create_spool, new_spec)
                message = (f"Created spool #{made['spool_id']} on the shelf"
                           f"{' (existing filament type reused)' if made['reused_filament'] else ''}. "
                           f"Load it with `/spool change <slot> {made['spool_id']}`.")
            elif await _spool_printer():
                _made, message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                         spool_sync, pid, mr_kw)
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif verb in ('weight', 'grams'):
            # #1374: correct a spool's grams left in one line. Global, like
            # /spool new: a Spoolman record, not a printer-bound slot change.
            parts = rest.replace('#', ' ').replace('g', ' ').split()
            try:
                spool_id, grams = int(parts[0]), float(parts[1])
            except (IndexError, ValueError):
                message = ('`/spool weight <spool#> <grams left>`, for example `/spool weight 19 930`. '
                           'Nothing changed.')
            else:
                message = await asyncio.to_thread(spool_sync.set_remaining_text, spool_id, grams)
        elif verb == 'sync':
            if await _spool_printer():
                # Reconcile first (#798): Spoolman's ph_slot assignments rebuild
                # the local cache and converge the screens, then the existing
                # screen sync runs as before. The message shows both outcomes.
                try:
                    reconciled = await asyncio.to_thread(spool_sync.reconcile, **mr_kw)
                    reconcile_line = spool_sync.reconcile_text(reconciled)
                except Exception:
                    logger.exception('/spool sync reconcile failed')
                    reconcile_line = ('Spool reconcile: could not run — the local slot record '
                                      'was left unchanged')
                message = (reconcile_line + '\nPrinter sync from Spoolman:\n'
                           + '\n'.join(await asyncio.to_thread(spool_sync.sync_all_screens, **mr_kw)))
            else:
                message = 'No printer chosen, so nothing was changed.'
        elif verb == '':
            answer = await _spool_ask(adapter, source, metadata, gateway,
                                      'What would you like to do with the spools?',
                                      [_SPOOL_MENU_NEW, _SPOOL_MENU_CHANGE, _SPOOL_MENU_LIST, 'Cancel'])
            picked = str(answer).strip().casefold() if isinstance(answer, str) else ''
            if answer == _SPOOL_MENU_NEW or picked in ('new', 'add', 'add a new spool'):
                if await _spool_printer():
                    _made, message = await _spool_guided_new(adapter, source, metadata, gateway,
                                                             spool_sync, pid, mr_kw)
                else:
                    message = 'No printer chosen, so nothing was changed.'
            elif answer == _SPOOL_MENU_CHANGE or picked in ('change', 'change a spool', 'swap', 'replace'):
                if await _spool_printer():
                    message = await _spool_guided_change(adapter, source, metadata, gateway,
                                                         spool_sync, pid, mr_kw)
                else:
                    message = 'No printer chosen, so nothing was changed.'
            elif answer == _SPOOL_MENU_LIST or picked == 'list':
                message = await _spool_list_message()
            elif _spool_is_cancel(answer):
                message = _SPOOL_CANCELLED
            else:
                message = _SPOOL_UNCLEAR
        else:
            message = ('`/spool list` · `/spool change <slot> <spool#>` · '
                       '`/spool new brand, material, color, grams[, hex]` · '
                       '`/spool weight <spool#> <grams left>` · `/spool sync`')
    except Exception as exc:
        logger.exception('/spool failed')
        message = 'That spool change did not work, so nothing was changed. Check the slot and spool number and try again.'
    finally:
        if guided:
            _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'spool NL intent handled' if nl_intent else 'spool command handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = spool_dispatch
