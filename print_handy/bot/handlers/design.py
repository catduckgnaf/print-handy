"""The design chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import asyncio, re
from pathlib import Path

from .common import (
    _DESIGN_BUSY, _adapter_for, _design_busy_reply, _design_claim, _design_conversation,
    _design_engine, _design_failure_text, _design_owner, _design_root, _design_slice,
    _design_transport, _is_print_discord_event, _plain_print_number, _recent_model_link,
    _run_design_turn,
)
from ...runtime import _config


def _fetch_design_link(url, target_dir):
    """Seam over design.fetch_link_source so dispatch tests can fake the network."""
    from print_handy.core import design
    return design.fetch_link_source(url, target_dir)


def _design_file(event):
    """The single dropped .scad in this message (a design source), else None."""
    hits = [Path(p) for p in (getattr(event, 'media_urls', None) or [])
            if Path(str(p)).suffix.lower() == '.scad']
    return hits[0] if len(hits) == 1 and hits[0].is_file() else None


_DESIGN_CONTEXT_PHRASES = ("design this", "design it", "redesign this",
                           "edit this", "modify this", "change this")
_DESIGN_ASK_PHRASES = ("edit this", "modify this", "change this")


def _design_parse(text):
    """Classify a plain message as a design request: (kind, rest) or None.

    Kinds: 'create' (rest is the description), 'context' (a design/edit ask
    that needs a model — attached, linked or recent; rest is the instruction,
    possibly ''), 'ask' (a bare 'design me'/'help me design' with neither a
    description nor an obvious model). Word-boundaried, case-insensitive;
    commands and ordinary chat return None.
    """
    low = text.casefold()

    def rest_of(match):
        return re.sub(r"\s+", " ", text[:match.start()] + " " +
                      text[match.end():]).strip(" \t—–-:,.").strip()

    for phrase in ("help me design", "design me"):
        match = re.search(rf"\b{re.escape(phrase)}\b", low)
        if match:
            rest = rest_of(match)
            return ("create", rest) if rest else ("ask", "")
    for phrase in _DESIGN_CONTEXT_PHRASES:
        match = re.search(rf"\b{re.escape(phrase)}\b", low)
        if match:
            return ("context", rest_of(match))
    return None


def _recent_design_source(source):
    """(path, link) for the channel's recent model, else (None, None).

    The recent link wins (the same machinery "print it" uses); otherwise the
    newest archived job by record mtime whose source file is still on disk —
    a design-context recency hint, deliberately not "the last print" (#979).
    """
    url = _recent_model_link(source)
    if url:
        return None, url
    try:
        from print_handy.core.prepared_intake import newest_archived_job
        job = newest_archived_job()
    except Exception:
        job = None
    if job:
        path = Path(job.get('downloaded_file') or '')
        if path.is_file() and path.suffix.lower() in ('.stl', '.3mf', '.scad'):
            return path, None
    return None, None


_DESIGN_COMMANDS = ('/print-design', '/design')  # canonical + short alias (#388 close-out)


def _design_command_args(text):
    """The argument text when `text` is a design command, else None."""
    for command in _DESIGN_COMMANDS:
        if text == command:
            return ''
        if text.startswith(command + ' '):
            return text[len(command):].strip()
    return None


async def _no_design_model(messages):
    """The engine's model seam when no endpoint is configured. Only the
    no-model paths (undo, a plain parameter edit) run then, so reaching
    this is a change that needs the model (#1206)."""
    from print_handy.core import design
    raise design.DesignError('that change needs the design model, and no design endpoint '
                             'is configured (see docs/DESIGN.md).')


def _brings_a_model(event, text):
    """True when a message carries a model for the print intake and no design phrase.

    The same detection the intake hook uses: a model link, dropped
    .stl/.3mf files or a .zip of them, or a G-code file. A design phrase
    beside it ("redesign this <link>") is a design request instead.
    """
    if _design_command_args(text) is not None or _design_parse(text) is not None:
        return False
    from print_handy.core import prepared_intake, yolo
    from print_handy.core.link_intake import model_link
    return bool(model_link(text) or prepared_intake.model_files(event)
                or prepared_intake.archive_files(event) or yolo.gcode_file(event))


_PRINT_VERB_RE = re.compile(r"\b(?:print|reprint|start|send|slice)\b", re.I)


def _starts_a_print(text):
    """True for a plain print request that is not a design instruction.

    A reprint ("print 57", "print the last one again") or a print phrase
    naming a configured printer ("print this on the Garage AD5X"). Plain
    "print it" stays with the design: while a design is running it may
    mean the design itself.
    """
    if not text or _design_parse(text) is not None:
        return False
    from print_handy.core import prepared_intake
    if _plain_print_number(text) is not None or prepared_intake.reprint_request(text):
        return True
    if not _PRINT_VERB_RE.search(text):
        return False
    try:
        from print_handy import settings
        roster = settings.printers(_config.cfg())
    except Exception:
        return False
    low = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", low)
               for entry in roster for name in _printer_names(entry))


def _printer_names(entry):
    """What a message may call a roster printer: its name, its id, and the
    distinctive words of its name ("Garage AD5X" -> "garage", "ad5x")."""
    full = str(entry.get('name') or '').casefold().strip()
    names = {full, str(entry.get('id') or '').casefold().strip()}
    names.update(word for word in re.findall(r"\w+", full)
                 if len(word) >= 4 or (len(word) >= 2 and any(c.isdigit() for c in word)))
    return {name for name in names if len(name) >= 2}


async def design_dispatch(event, gateway, **kwargs):
    """Serialize design turns per owner around _design_dispatch_turns (P2-151).

    A turn awaits the model (and then its Slice/Change/Cancel prompt) with
    the session's files as shared state. A second owner message arriving
    in that window used to load the same session and run a concurrent
    turn: both turns wrote the same SCAD/STL/preview files, and the first
    turn's still-open approval buttons could hand off the second turn's
    geometry. Hold a per-owner claim for the whole dispatch and refuse a
    concurrent one with a busy note; other commands and unrelated chat
    still fall through untouched.

    The claim covers design instructions only. A message that brings a
    model (a pasted link, a dropped file) always goes to the intake, and
    while a turn runs a print request ("print 57", "print this on the
    Garage AD5X") goes to the print handlers: the owner can start another
    print while the design works. A second design by the same owner in the
    same channel or thread is still refused; a design in another thread is
    another owner and runs alongside.

    While the design's own question is open (phase "prompt"), a plain
    message is the answer to it — a typed choice, a Matrix number, or a
    change typed instead of pressing Change it — so it falls through to the
    bot core, which routes typed answers to the open prompt. Refusing it
    here used to swallow every typed answer with the busy note.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    owner = _design_owner(source)
    if owner in _DESIGN_BUSY:
        text = str(getattr(event, 'text', '') or '').strip()
        if _design_command_args(text) is None and text.startswith('/'):
            return None  # another command: not design's to refuse
        phase = _DESIGN_BUSY.get(owner)
        if _brings_a_model(event, text) or (phase == 'turn' and _starts_a_print(text)):
            # A new model (a pasted link, a dropped file) or a print
            # request is not a design instruction: the intake and print
            # handlers take it while the design keeps running. A link
            # typed at the open design question is a new intake too,
            # never a revision of the design on screen.
            return None
        if phase == 'prompt' and _design_command_args(text) is None:
            # The answer to the open design question. Answer it here, the
            # way the bot core's typed-answer route would: returning None
            # let a later hook (edit_dispatch) claim "make it 5 mm taller"
            # or "undo" first and apply it to the channel's job (#1206).
            adapter = _adapter_for(gateway, source)
            answer_typed = getattr(adapter, 'answer_typed', None)
            if text and answer_typed is not None and await answer_typed(
                    gateway._session_key_for_source(source), text,
                    user_id=getattr(source, 'user_id', None)):
                return {'action': 'skip', 'reason': 'typed answer to the design question'}
            return None
        from print_handy.core import design as _design
        if _design_command_args(text) is None and not _design.find_session_for_owner(
                _design_root(_config.cfg()), owner):
            return None  # no live design session: not a design message
        return await _design_busy_reply(_adapter_for(gateway, source), source)
    with _design_claim(owner):
        return await _design_dispatch_turns(event, gateway, **kwargs)


async def _design_dispatch_turns(event, gateway, **kwargs):
    """/print-design (alias /design) and natural design requests (#388).

    Create loop (Stage 1): /print-design <what to make>, Slice it / Change
    it / Cancel, Slice it handing the STL to prepared intake. Stage 2 adds
    edit sessions — /print-design (or a design phrase) with a dropped
    .stl/.3mf/.scad or a model link in the same message, the same phrases
    against the channel's recent model, and create phrases ("design me …",
    "help me design …") — seeded through the engine's edit path. Routing is
    deterministic and narrow: no design phrase means this handler returns
    None and plain intake/chat behave exactly as before; inside a session
    awaiting a revision, the owner's next plain message is the revision
    before any phrase detection runs.

    Close-out rulings: the handler serves every bot platform — Discord,
    Telegram and Matrix — through the platform-neutral adapter/prompt path
    (Matrix answers the Slice it / Change it / Cancel choices as a numbered
    list, via the shared prompt machinery). Design is its own configured
    lane: on Telegram/Matrix it runs only through an explicitly configured
    [design] endpoint, and their pure-bot chat posture is untouched —
    ordinary chat there still has no [llm] fallback (BotCore keys that off
    the adapter's uses_llm, which design never changes). The enable switch
    is tri-state (design.effective_enabled): unset is auto-on when an
    endpoint resolves, explicit false is off in all cases.
    """
    if not _is_print_discord_event(event):
        return None
    source = event.source
    if not getattr(source, 'user_id', None) or not gateway._is_user_authorized_for_source(source):
        return None
    text = str(getattr(event, 'text', '') or '').strip()
    command_args = _design_command_args(text)
    is_command = command_args is not None
    from print_handy.core import design
    from print_handy.core.link_intake import model_link
    from print_handy.core.prepared_intake import model_file
    cfg = _config.cfg()
    root = _design_root(cfg)
    owner = _design_owner(source)
    session = design.find_session_for_owner(root, owner)

    # Resolve what this message asks for before any state changes.
    intent = None  # (kind, instruction, file_path, link_url)
    if is_command:
        intent = ("command", command_args, None, None)
    elif text.startswith('/'):
        return None
    elif (session and session.get('status') == 'awaiting_revision'
          and not _brings_a_model(event, text)):
        # A pasted link or a dropped model is a new print, not the change
        # the design waits for: with no design phrase it is left to the
        # intake hook, and the design keeps waiting.
        intent = ("revision", text, None, None)
    else:
        parsed = _design_parse(text)
        if parsed is None:
            return None
        kind, rest = parsed
        attached = model_file(event) or _design_file(event)
        linked = model_link(text)
        if attached or linked:
            intent = ("edit", rest, attached, linked)
        elif kind == "create":
            intent = ("create", rest, None, None)
        elif kind == "ask":
            recent_path, recent_link = _recent_design_source(source)
            if recent_path or recent_link:
                intent = ("edit", "", recent_path, recent_link)
            else:
                intent = ("ask", "", None, None)
        else:  # 'context': only the change-this family stays silent without a model
            recent_path, recent_link = _recent_design_source(source)
            if recent_path or recent_link:
                intent = ("edit", rest, recent_path, recent_link)
            elif any(re.search(rf"\b{re.escape(p)}\b", text.casefold())
                     for p in _DESIGN_ASK_PHRASES):
                return None  # "change this" with no model anywhere is not ours
            else:
                intent = ("ask", "", None, None)
    # Natural-language intents get the same answers as the command when
    # design cannot run (#388 criterion 7, catduck's ruling): the enabled
    # and transport checks below reply "not set up" / "not configured" and
    # consume the message. Messages that matched no trigger returned None
    # during intent resolution above, so non-design chat is untouched
    # whatever the design state.
    adapter = _adapter_for(gateway, source)
    if adapter is None:
        return {'action': 'skip', 'reason': 'design UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None

    async def say(content):
        await adapter.send(chat_id=source.chat_id, metadata=metadata, content=content)

    complete, _kind = _design_transport(cfg)
    engine = _design_engine(cfg, complete or _no_design_model)
    # A button word, an undo or a plain parameter edit of the waiting
    # design needs no model, so a missing endpoint does not refuse it (#1206).
    model_free = (complete is None and intent[0] == "revision" and session is not None
                  and (design.typed_choice(text) is not None
                       or not engine.will_call_model(session, text)))
    design_raw = (cfg.get('design') or {}).get('enabled')
    if design_raw is False:
        await say('Design is not set up: design.enabled is false — set design.enabled = true '
                  '(PRINT_HANDY__DESIGN__ENABLED=true) or remove it to turn design on when a '
                  'model is configured — see docs/DESIGN.md. Nothing was designed.')
        return {'action': 'skip', 'reason': 'design not enabled'}
    if complete is None and not model_free:
        if design_raw is True:
            await say('Design is enabled but no design model is configured: set '
                      '[design] chat_url/model (or the [llm] values they inherit). '
                      'Nothing was designed.')
            return {'action': 'skip', 'reason': 'design model not configured'}
        # Auto posture with nothing configured anywhere: the same "not set
        # up" answer the command has always given (#422).
        await say('Design is not set up: configure a design model — set [design] chat_url/model '
                  '(or the [llm] values they inherit), or PRINT_HANDY__DESIGN__ENABLED=true with '
                  'a design endpoint — see docs/DESIGN.md. Nothing was designed.')
        return {'action': 'skip', 'reason': 'design not enabled'}
    note = ''
    try:
        kind, instruction, file_path, link_url = intent
        if kind == "revision":
            # After Change it, a typed button word is that button, not a
            # change to send to the model (#1206): "never mind" cancels and
            # "slice it" hands the current version to intake.
            chosen = design.typed_choice(text)
            current_stl = (root / session['id'] / session['stl']) if session.get('stl') else None
            if chosen == design.CANCEL_LABEL:
                engine.cancel(session)
                await say('Design cancelled. The files stay in the session folder for a while; '
                          'nothing was sliced or started.')
                return {'action': 'skip', 'reason': 'design cancelled'}
            if chosen == design.SLICE_LABEL and (
                    design.unprintable(session.get('findings'))
                    or (current_stl is not None and current_stl.is_file())):
                return await _design_slice(event, gateway, adapter, engine, session, current_stl)
            if chosen == design.CHANGE_LABEL:
                await say('Say the change in your next message — e.g. "make it 5 mm taller".')
                return {'action': 'skip', 'reason': 'design awaiting revision'}
            result = await _run_design_turn(engine, session, text, adapter, source, metadata)
        elif kind == "ask":
            await say('Drop the file you want to work on, paste its model link, or tell me '
                      'what to make — e.g. "design me a 40 mm bracket with two holes". '
                      'Nothing was designed.')
            return {'action': 'skip', 'reason': 'design needs a model or a description'}
        else:
            if kind == "command":
                attached = model_file(event) or _design_file(event)
                linked = model_link(instruction)
                if attached or linked:
                    kind, file_path, link_url = "edit", attached, linked
                elif not instruction:
                    if session and session.get('status') == 'awaiting_revision':
                        await say('A design is waiting for your call: reply with the change you want, '
                                  'or /print-design <a new part> to start over.')
                    else:
                        await say('Usage: /print-design <what to make> — e.g. '
                                  '/print-design a 40x20x3 plate with two 4 mm holes 30 mm apart; '
                                  'or drop a model file / paste a link with the change you want.')
                    return {'action': 'skip', 'reason': 'design usage shown'}
                else:
                    kind = "create"
            if kind == "create":
                if session:
                    engine.cancel(session)  # a fresh design supersedes the old session
                session = engine.start_session(owner, source.chat_id, instruction)
                result = await _run_design_turn(engine, session, instruction, adapter, source,
                                              metadata)
            else:  # edit
                if link_url:
                    instruction = instruction.replace(link_url, '').strip(" \t—–-:,.")
                if file_path and link_url:
                    # An attached model wins over a link in the same message
                    # (P2-156): the operator supplied the exact source to
                    # edit, while fetching the link would silently edit the
                    # downloaded version instead (e.g. an edited .scad
                    # attached alongside its original model link).
                    link_url = None
                if session:
                    engine.cancel(session)  # a fresh design supersedes the old session
                description = instruction or f"Edit {(file_path.name if file_path else 'the linked model')}"
                if link_url:
                    session = await asyncio.to_thread(
                        engine.start_edit_session, owner, source.chat_id, description,
                        None, link_url=link_url,
                        fetcher=lambda url, folder: _fetch_design_link(url, folder))
                else:
                    session = await asyncio.to_thread(
                        engine.start_edit_session, owner, source.chat_id, description,
                        file_path)
                note = design.source_note(session)
                if instruction:
                    result = await _run_design_turn(engine, session, instruction, adapter, source,
                                              metadata)
                else:
                    # No change asked for: show the dropped model exactly as
                    # it is, with no model call in between (#841 gap 4).
                    result = await asyncio.to_thread(engine.baseline_turn, session)
    except design.DesignQuestion as asked:
        # The one clarifying question a design may ask, only when the answer
        # changes the part and cannot be assumed. The session waits; the
        # owner's next plain message is the answer and the design runs then.
        await say(f'{asked.question}\n(Answer in your next message — or say "you pick" and '
                  'I will choose a sensible size and tell you what I assumed.)')
        return {'action': 'skip', 'reason': 'design asked a question'}
    except Exception as exc:
        await say(_design_failure_text(exc))
        return {'action': 'skip', 'reason': 'design turn failed'}
    return await _design_conversation(event, gateway, adapter, engine, result, note=note)


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = design_dispatch
