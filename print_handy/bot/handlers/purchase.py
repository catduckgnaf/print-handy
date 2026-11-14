"""The purchase chat handler.

Moved verbatim from ``plugins/print-feedback/__init__.py`` (#1323). The plugin file still
loads the same handlers for one release.
"""
import logging
import asyncio, json, os, time
from pathlib import Path

from .common import _confirm, _control_gate, _seen, _spool_timeout

# Per-module logger (#1404): records name the handler module that
# made them, so [logging] levels rules can target one handler.
logger = logging.getLogger(__name__)


def _purchase_ai_call():
    """The AI call for purchase intake (#801), on the #514 vision route.

    One callable with the purchase_intake shape (prompt, plus an image or
    page text): a screenshot goes through the shared vision extraction
    (visual_watch.visual_extract, the listener's configured values, as
    _spool_label_vision_call does); page text goes to the same chat
    endpoint as a text-only message, or to PRINT_VISION_URL with the text
    inside the prompt when that is the only route.
    """
    import urllib.request
    from print_handy.runtime import moonraker_event_listener as listener
    from print_handy.runtime import visual_watch

    def call(*, prompt, image=None, text=None):
        if image is not None:
            return visual_watch.visual_extract(
                image, prompt, chat_url=listener._vision_chat_url, model=listener._VISION_MODEL,
                api_key=lambda: listener._profile_env_value("CLIPROXY_API_KEY"),
                context="Filament purchase screenshot or order. Reply with JSON only.")
        full = prompt if text is None else f"{prompt}\n\nPage text:\n{text}"
        endpoint = os.environ.get("PRINT_VISION_URL")
        if endpoint:
            import base64 as _b64
            payload = json.dumps({"image": _b64.b64encode(b"").decode(), "prompt": full}).encode()
            req = urllib.request.Request(endpoint, data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read())
        url = listener._vision_chat_url()
        key = listener._profile_env_value("CLIPROXY_API_KEY")
        if not key and not visual_watch.keyless_ok(url):
            return None
        body = {"model": listener._VISION_MODEL, "temperature": 0, "max_tokens": 800,
                "messages": [{"role": "user", "content": full}]}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read())
        content = visual_watch._strip_json_fence(result["choices"][0]["message"]["content"])
        try:
            return json.loads(content)
        except ValueError:
            return content

    return call


def _purchase_event_image_paths(event):
    from print_handy.runtime import purchase_intake
    return [str(p) for p in (getattr(event, 'media_urls', None) or [])
            if purchase_intake.is_image_name(p)]


def _purchase_event_image(event):
    """(has_image, image bytes or None) for a channel message (#801).

    Saved attachments ride the event as media_urls (the model_file shape);
    their bytes are read only after the operator accepts the offer.
    """
    paths = _purchase_event_image_paths(event)
    if not paths:
        return False, None
    for path in paths:
        candidate = Path(path)
        if candidate.is_file():
            try:
                return True, candidate.read_bytes()
            except OSError:
                return True, None
    return True, None


async def purchase_dispatch(event, gateway, **kwargs):
    """/purchase intake (#801): a shop/order link or a purchase screenshot in
    the printer channel earns a confirm-first offer, only when a vision/AI
    route is configured (purchase_intake.ai_route_configured — the #514
    gating values). Never automatic: the offer asks first, the proposed
    spools are shown as one list, and Create all / Edit / Cancel decide.
    Model links keep model_link_dispatch's routing (it runs earlier, and
    recognition here refuses them too). With no AI route, or nothing
    purchase-shaped, this returns None and the message is untouched.
    """
    from print_handy.runtime import purchase_intake
    text = str(getattr(event, 'text', '') or '')
    if not purchase_intake.looks_like_purchase(text, has_image=bool(_purchase_event_image_paths(event))):
        return None
    if not purchase_intake.ai_route_configured():
        return None
    source, adapter = _control_gate(event, gateway)
    if source is None:
        return None
    if adapter is None:
        return {'action': 'skip', 'reason': 'purchase UI delivery unavailable'}
    metadata = {'thread_id': source.thread_id} if source.thread_id else None
    key = ('purchase', source.chat_id, source.thread_id)
    if key in _seen:
        return {'action': 'skip', 'reason': 'purchase flow already active'}
    _seen[key] = time.monotonic()
    live_key = gateway._session_key_for_source(source)
    try:
        answer = await _confirm(adapter, source, metadata, 'purchase',
                                purchase_intake.OFFER_QUESTION,
                                [purchase_intake.OFFER_YES, purchase_intake.OFFER_NO],
                                _spool_timeout(), live_key=live_key)
        if answer != purchase_intake.OFFER_YES:
            message = 'Okay — nothing was added.'
        else:
            url = purchase_intake.purchase_url(text)
            try:
                if url is not None:
                    lines = await asyncio.to_thread(
                        purchase_intake.propose_from_link, url, ai_call=_purchase_ai_call())
                else:
                    has_image, image = _purchase_event_image(event)
                    if not image:
                        raise purchase_intake.PurchaseError('unreadable')
                    lines = await asyncio.to_thread(
                        purchase_intake.propose_from_image, image, ai_call=_purchase_ai_call())
            except purchase_intake.PurchaseError as exc:
                message = str(exc)
            else:
                choice = await _confirm(adapter, source, metadata, 'purchase',
                                        purchase_intake.confirm_text(lines),
                                        list(purchase_intake.CONFIRM_CHOICES),
                                        _spool_timeout(), live_key=live_key)
                if choice == 'Create all':
                    try:
                        created = await asyncio.to_thread(purchase_intake.create_purchase, lines)
                        message = purchase_intake.created_text(created)
                    except purchase_intake.PartialCreationError as exc:
                        # Some spools landed: say which, never "nothing was created".
                        message = str(exc)
                    except Exception:
                        logger.exception('purchase creation failed before any spool was created')
                        message = ('Creating the spools failed before any were created, so nothing '
                                   'was added. Add the spools by hand with `/spool new`.')
                elif choice == 'Edit':
                    message = purchase_intake.edit_handoff_text(lines)
                else:
                    message = 'Cancelled. Nothing was created.'
    except Exception:
        logger.exception('purchase intake failed')
        message = 'That purchase could not be read, so nothing was created. Add the spools by hand with `/spool new`.'
    finally:
        _seen.pop(key, None)
    await adapter.send(chat_id=source.chat_id, metadata=metadata, content=message)
    return {'action': 'skip', 'reason': 'purchase intake handled'}


# --- handler interface (#1323) ---
from ._gates import free_text  # noqa: E402

match = free_text
handle = purchase_dispatch
