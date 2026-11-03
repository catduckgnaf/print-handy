"""Fresh Discord URL intake. Local labels only; never contact a printer or source site."""
import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from .. import settings

logger = logging.getLogger(__name__)


def model_link(text):
    # A question beside a link is still fresh intake, not historical recovery.
    text = str(text or '').strip()
    if re.match(r'^(?:/)?(?:retry|resume)\b', text, re.I):
        return None
    urls = re.findall(r'https?://[^\s<>\)\]]+', text)
    if len(urls) != 1:
        return None
    text = urls[0].rstrip('.,;!?')
    host = (urlsplit(text).hostname or '').lower()
    path = urlsplit(text).path
    if host in {'makerworld.com', 'www.makerworld.com'} and path.startswith('/en/models/'):
        return text
    if host in {'printables.com', 'www.printables.com'} and re.match(r'/(?:[a-z]{2}/)?model/', path):
        return text
    if host in {'thingiverse.com', 'www.thingiverse.com'} and re.match(r'/thing:\d+', path):
        return text
    if host in {'myminifactory.com', 'www.myminifactory.com'} and re.match(r'/object/(?:[^/]*-)?\d+/?$', path):
        return text
    if host in {'cults3d.com', 'www.cults3d.com'} and re.match(r'/(?:[a-z]{2}/)?3d-model/(?:[^/]+/)?[^/]+/?$', path):
        return text
    if host in {'crealitycloud.com', 'www.crealitycloud.com'} and re.match(r'/(?:[a-z]{2}(?:-[a-z]{2})?/)?model-detail/[^/]+/?$', path, re.I):
        return text
    if host in {'thangs.com', 'www.thangs.com'} and (
            re.fullmatch(r'/m/\d+/?', path) or re.search(r'/3d-model/[^/]*-\d+/?$', path)):
        return text
    if host in {'nexprint.com', 'www.nexprint.com'} and re.fullmatch(r'/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/[A-Za-z0-9]+/?', path, re.I):
        return text
    if host in {'makeronline.com', 'www.makeronline.com'} and re.fullmatch(r'/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(?:[^/]+/)?\d+\.html', path, re.I):
        return text
    if _direct_file_link(text, host, path):
        return text
    return None


# Direct file links (#607): a public link straight at a print file, on any
# host. Recognition is by the URL alone — the guarded fetch in model_job
# verifies the destination is public and the bytes are real geometry, so a
# link that merely claims to be a file is refused there, never trusted here.
DIRECT_FILE_EXTS = {'.stl', '.3mf'}


def _direct_file_link(url, host, path):
    if host in {'www.dropbox.com', 'dropbox.com'}:
        # Share links: model_job rewrites these to the download form.
        return path.startswith('/s/') or path.startswith('/scl/')
    if host == 'dl.dropboxusercontent.com':
        return path.startswith('/s/') or Path(path).suffix.lower() in DIRECT_FILE_EXTS
    return Path(path).suffix.lower() in DIRECT_FILE_EXTS


def has_duration_question(text: str) -> bool:
    """Detect whether a message asks about print duration or time."""
    if not text:
        return False
    return bool(re.search(r'\b(how long|duration|how much time|print time|when.*finish|take)\b', str(text), re.IGNORECASE))


# Prompts that accept a typed reply, by who was asked where and for which intake:
# (chat_id, thread_id, user_id, session_key) -> (clarify_id, parser). Newest last.
_typed_prompts = {}

# Every prompt ask() opens, parser or not, under the same key shape ->
# clarify_id. Newest last. typed_reply's cross-printer ambiguity check
# reads this alongside _typed_prompts: a button-only question (a colour
# pick) for another printer is still an open question a typed reply could
# be answering, so it must count when deciding a reply is ambiguous (#536).
_open_prompts = {}


def _scope(source):
    return (str(getattr(source, 'chat_id', '')), str(getattr(source, 'thread_id', '') or ''),
            str(getattr(source, 'user_id', '') or ''))


def _round_trips(parser, value):
    """True when str(value) re-parses to value with the prompt's own parser."""
    try:
        return parser(str(value)) == value
    except Exception:
        return False


def typed_reply(source, text):
    """Route typed text to the sender's newest open typed prompt in this chat. Returns None when they
    have none open (another operator's prompt never takes it), '' when the reply resolved it, or a
    corrective message (the prompt stays open)."""
    from .prompts import gateway as _prompts
    cm = _prompts()
    scope = _scope(source)
    keys = [key for key in _typed_prompts if key[:3] == scope]
    if not scope[2] or not keys:
        return None
    # Two intakes of one operator decided for DIFFERENT printers in a
    # shared channel (#536) register under different printer-scoped keys:
    # a typed reply cannot tell them apart, so it resolves neither rather
    # than feeding one printer's prompt the other's answer. Same-printer
    # and unscoped prompts keep the newest-wins routing below. The scan
    # covers every open prompt in this scope, not only the parser ones in
    # _typed_prompts: an open button-only question for another printer
    # (tracked in _open_prompts) makes the reply just as ambiguous.
    from .prompts import PRINTER_KEY_SEP
    open_ids = [_typed_prompts[key][0] for key in keys]
    open_ids += [cid for key, cid in list(_open_prompts.items()) if key[:3] == scope]
    printers = set()
    for cid in open_ids:
        try:
            registered = str(cm.session_key(cid) or '')
        except Exception:
            registered = ''
        if PRINTER_KEY_SEP in registered:
            printers.add(registered.rsplit(PRINTER_KEY_SEP, 1)[-1])
    if len(printers) > 1:
        return None
    cid, parser = _typed_prompts[keys[-1]]
    is_open = getattr(cm, 'is_open', None)
    if is_open is not None and not is_open(cid):
        # The prompt already closed — a button answered it, it expired, it
        # was cancelled — but this entry outlives it until the intake's own
        # cleanup runs. Never run the parser for a closed prompt (P2-253):
        # a connect prompt's parser writes the pasted credential to config,
        # so a token arriving after Cancel or Save would still be saved,
        # and the message would then fall through to ordinary chat handling.
        # (A gateway build without is_open keeps the resolve-time check
        # below instead of breaking every typed reply.)
        return None
    global _resolved_prompt_id
    if str(text or '').strip().casefold() in CANCEL_WORDS:
        if cm.resolve_gateway_clarify(cid, 'cancel'):
            _resolved_prompt_id = cid
            return ''
        return None
    global _secret_taken
    _secret_taken = False
    _resolved_prompt_id = None
    try:
        value = parser(text)
    except ValueError as exc:
        _secret_taken = False
        return str(exc)
    # The gateway answer is a string ask() re-parses with this same parser, so
    # it must round-trip — str(value) does not always (a file selection parses
    # to a list of indices, whose str form "[1, 2]" the parser rejects, so the
    # question reopened forever). When str(value) does not re-parse to value,
    # resolve with the operator's own text, which parses to value by
    # construction. A reply that carried a secret is the exception: the secret
    # itself never travels through the prompt registry, so that path keeps
    # str(value) — the parser's public result (P2-252).
    wire = str(value)
    if not _secret_taken and not _round_trips(parser, value):
        wire = str(text)
    # A prompt a button already answered is closed: let the text fall through instead of swallowing it.
    resolved = cm.resolve_gateway_clarify(cid, wire)
    if resolved:
        _resolved_prompt_id = cid
        return ''
    if _secret_taken:
        # The parser saved a secret but the prompt closed underneath it (a
        # button won the race mid-parse): the reply is still consumed and
        # the dispatcher still deletes the message — a saved credential
        # must never fall through to ordinary chat handling (P2-253).
        return ''
    return None


# A parser that took a secret (a pasted login token) calls note_secret_reply()
# while typed_reply runs; the dispatcher then asks take_secret_reply() whether
# the operator's message should be deleted. Reset at the start of every
# typed_reply, so a flag never outlives the reply that set it.
_secret_taken = False


def note_secret_reply():
    """Called by a typed-reply parser that just accepted a secret from the message."""
    global _secret_taken
    _secret_taken = True


def take_secret_reply():
    """True once after a typed_reply that accepted a secret: delete that message."""
    global _secret_taken
    taken, _secret_taken = _secret_taken, False
    return taken


# The prompt a typed_reply just resolved (P2-209): the registry resolution
# alone leaves the prompt's rendered message standing, so the dispatcher
# takes the id here — once, right after the resolving reply — and retires
# the message through the clarify UI's close_prompt, the same end state a
# button click or an own-registry typed answer produces.
_resolved_prompt_id = None


def take_resolved_prompt_id():
    """The clarify id the last typed_reply resolved, once; None otherwise."""
    global _resolved_prompt_id
    taken, _resolved_prompt_id = _resolved_prompt_id, None
    return taken


CANCEL_WORDS = {'cancel', 'stop', 'nevermind', 'never mind', 'abort'}


def _typed_choice(text, choices):
    """Map clear plain-language intent to a visible button, or None to keep asking."""
    value = str(text or '').strip().casefold()
    if not value:
        return None
    if value in CANCEL_WORDS:
        terms = ('cancel', "don't print", 'stop')
    elif re.fullmatch(r'\d+', value):
        index = int(value) - 1
        return index if 0 <= index < len(choices) else None
    elif 'split' in value:
        terms = ('split',)
    elif value in {'100%', '100', 'full size', 'full'} or 'full size' in value:
        terms = ('100%', 'full size')
    elif 'preview' in value:
        terms = ('preview',)
    elif any(word in value for word in ('shrink', 'smaller', 'small')):
        terms = ('shrink', 'smaller', '%')
    elif value in {'yes', 'start', 'print', 'go'}:
        terms = ('start', 'slice and print', 'yes')
    else:
        return None
    for index, choice in enumerate(choices):
        if any(term in str(choice).casefold() for term in terms):
            return index
    return None


class PromptCancelled(RuntimeError):
    """The operator cancelled a prompt. Nothing after this point should run."""


class PromptExpired(RuntimeError):
    """A button question reached its timeout with no choice and no decision saved."""


CANCEL_LABEL = "Cancel"


async def ask(adapter, source, session_key, question, choices, timeout=None, allow_other=False, parser=None,
              live_key=None, approval=False):
    """Ask a button question. With ``parser``, a typed reply in the same chat is also accepted;
    ``live_key`` registers under the live chat session so a busy gateway routes the text here
    instead of queueing it as a new agent turn. ``approval`` marks a print
    approval (P1-93): the prompt is registered wherever the gateway keeps it,
    but the mark always lands in this package's own registry, whose
    ``is_approval`` the standalone adapters consult before resolving — so
    only the approval user can answer it when one is configured."""
    from . import prompts as _prompt_marks
    from .prompts import gateway as _prompts
    cm = _prompts()
    registration_key = live_key or session_key
    # Every question offers Cancel. Callers that already end on a cancel choice keep theirs.
    cancel_added = not choices or not any(term in str(choices[-1]).casefold()
                                          for term in ("cancel", "don't print", "do not print"))
    shown = list(choices) + ([CANCEL_LABEL] if cancel_added else [])
    typed_key = _scope(source) + (str(session_key),)
    cid = None
    try:
        for attempt in range(2):
            # Register BEFORE sending: a fast click can resolve the prompt inside send_clarify,
            # and re-registering afterwards would discard that answer.
            cid = uuid.uuid4().hex
            cm.register(cid, registration_key, question, shown)
            if approval:
                _prompt_marks.mark_approval(cid)
            _open_prompts.pop(typed_key, None)  # re-added last, like _typed_prompts below
            _open_prompts[typed_key] = cid
            if parser is not None:
                _typed_prompts.pop(typed_key, None)  # re-added last: the newest open question takes typed text
                _typed_prompts[typed_key] = (cid, parser)
            result = await adapter.send_clarify(chat_id=str(source.chat_id), question=question,
                choices=shown, clarify_id=cid, session_key=session_key,
                metadata={'thread_id': source.thread_id} if source.thread_id else None)
            if not result.success:
                raise RuntimeError('color prompt delivery failed')
            answer = await asyncio.to_thread(cm.wait_for_response, cid, timeout)
            if cancel_added and answer == CANCEL_LABEL:
                raise PromptCancelled('cancelled')
            if answer in choices:
                return choices.index(answer)
            if isinstance(answer, str) and parser is not None:
                try:
                    parsed = parser(answer)
                    if parsed is not None:
                        return parsed
                except ValueError:
                    # The live gateway can replay the original request text into an
                    # active prompt. Ignore non-answers and keep the prompt available.
                    pass
            if answer is None:
                raise PromptExpired('this question expired before a button was pressed; no decision saved')
            if answer == cm.CANCELLED:
                break
            if isinstance(answer, str):
                picked = _typed_choice(answer, choices)
                if picked is not None:
                    return picked
                if parser is not None:
                    try:
                        return parser(answer)
                    except ValueError:
                        # A live session may replay the original request text (for example,
                        # a URL plus a duration question) into the first typed prompt. Keep
                        # the prompt open instead of treating that context as a bad reply.
                        pass
            if attempt == 0:
                question += '\nPick a button or type split / 100% / shrink / cancel.'
        raise RuntimeError('clarification requires a valid button choice; no decision saved')
    finally:
        if _open_prompts.get(typed_key) == cid:
            _open_prompts.pop(typed_key, None)
        if parser is not None and _typed_prompts.get(typed_key, (None,))[0] == cid:
            _typed_prompts.pop(typed_key, None)
        if approval and cid is not None:
            _prompt_marks.forget_approval(cid)
        cm.resolve_gateway_clarify(cid, cm.CANCELLED)
        cm.wait_for_response(cid, 0.001)


async def collect_link(event, adapter, state, root, timeout=300):
    from .model_job import resolve_filament_selection, build_slot_labels, build_tool_labels, configured_slots, position_limit
    from .. import printers
    source = event.source
    url = model_link(event.text)
    message_id = getattr(event, 'message_id', None)
    if not url or not message_id:
        raise ValueError('plain model link requires a stable Discord message ID')
    from .model_job import intake_id_from_message
    intake_id = intake_id_from_message(source, message_id)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (intake_id + '.json')
    receipt = {'intake_id': intake_id, 'url': url, 'status': 'AWAITING_COLOR',
               'sliced': False, 'uploaded': False, 'started': False}
    # Cross-process reservation; completed/failed events are not automatically replayed.
    try:
        with path.open('x') as f:
            json.dump(receipt, f)
    except FileExistsError:
        return {'action': 'skip', 'reason': 'duplicate model-link event'}
    try:
        multi_tool = printers.current().multi_tool
        selections = [resolve_filament_selection(state, dict(label, slot=int(slot)))
                      for slot, label in sorted(configured_slots(state).items())]
        # A multi_tool printer's positions are toolheads, never slots (#346).
        labels = build_tool_labels(selections) if multi_tool else build_slot_labels(selections)
        if not labels or len(labels) > position_limit() or len(set(labels)) != len(labels):
            raise ValueError('requires one to four distinct configured toolheads' if multi_tool
                             else 'requires one to four distinct configured slots')
        session_key = 'model-link:' + intake_id
        mode = await ask(adapter, source, session_key,
            'New model request: print in one color, or choose colors per part after inspecting the source?',
            ['Print in one color', 'Choose colors per part'], timeout)
        receipt['mode'] = 'single_color' if mode == 0 else 'per_part'
        if mode == 0:
            slot = await ask(adapter, source, session_key,
                'Choose the color for this NEW request, even if this model was printed before. '
                f"These are configured {'toolhead' if multi_tool else 'slot'} labels.",
                labels, timeout)
            receipt['filament_selection'] = selections[slot]
        receipt['status'] = 'COLOR_CONFIRMED' if mode == 0 else 'PART_COLORS_REQUIRED'
        path.write_text(json.dumps(receipt, indent=2) + '\n')
        packet = json.dumps(receipt, sort_keys=True)
        return {'action': 'rewrite', 'text': (
            f'{url}\n\nCURRENT MODEL INTAKE: {packet}\n'
            'This is a NEW request, not a retry, even for the same URL. History is reference only. '
            'Retrieve fresh source inventory with python3 -m print_handy.core.model_job fetch and --intake-id ' + intake_id + '. '
            'Do not reuse any historical JOB.json, variant, color decisions, artifact or approval. '
            'Use only this receipt for the initial single-color selection; per-part mode still requires '
            'fresh named-part choices after source inspection. Ask fresh variant/completeness and slicing/bed-clear gates. '
            'No slicing, upload or launch is authorized by this URL or these color choices. '
            'A historical sensor refusal is not current telemetry and does not block intake.')}
    except Exception as exc:
        receipt['status'] = 'FAILED'
        path.write_text(json.dumps(receipt, indent=2) + '\n')
        logger.exception('new model color selection failed')
        from . import failure_report
        await adapter.send(chat_id=source.chat_id,
            content=failure_report.post(
                exc, 'color plan', subsystem='intake',
                headline='Choosing the colors for that model stopped before a choice was saved.',
                not_happened='No slicing, upload or print started.',
                transient='Send the link or file again to pick the colors again.'),
            metadata={'thread_id': source.thread_id} if source.thread_id else None)
        return {'action': 'skip', 'reason': 'new model color confirmation failed'}
