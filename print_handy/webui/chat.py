"""The Web UI chat page: the bot, in the browser.

Renders the web chat service's transcript (operator messages, bot
replies, button questions) and the two forms that feed it: a message
form (``POST /chat``) and, while a question is open, one button per
choice (``POST /chat/choice``). While a bot reply is pending the page
refreshes itself every few seconds, because replies arrive
asynchronously — a sliced print's confirmation question appears here
the same way it would in Discord — but an idle page never reloads, so
a half-typed message survives.

Everything the operator says goes to :class:`print_handy.bot.web.
WebChatService`, which runs it through ``BotCore.process``: this page
never slices, prints or approves anything itself. A choice button here
is the prompt's own button, resolved through the adapter with the
prompt's own approval rules.
"""
from __future__ import annotations

from . import shell

_esc = shell._esc
REFRESH_SECONDS = 3
WORKING_TEXT = "Working on it…"
POLL_FAST_MS = 600     # poll interval right after a message is sent
POLL_SLOW_MS = 3000    # the interval it backs off to while the turn keeps running
# Reload when the transcript grew or the turn ended. Only numbers and a
# boolean come back from /chat/state; nothing from it is put on the page.
POLL_SCRIPT = (
    "<script>(function(){var seq=__SEQ__,wait=" + str(POLL_FAST_MS) + ";"
    "function tick(){fetch('/chat/state',{credentials:'same-origin',cache:'no-store'})"
    ".then(function(r){return r.ok?r.json():null;}).then(function(s){"
    "if(s&&(s.seq!==seq||!s.pending)){location.reload();return;}"
    "wait=Math.min(wait*1.5," + str(POLL_SLOW_MS) + ");setTimeout(tick,wait);})"
    ".catch(function(){setTimeout(tick," + str(POLL_SLOW_MS) + ");});}"
    "setTimeout(tick,wait);})();</script>")


def chat_state(service):
    """GET /chat/state: the newest transcript entry and whether a reply is pending.

    The page's poll compares these with what it rendered and reloads on a
    change. No message text is returned. ``service`` None (chat off) reads
    as nothing pending.
    """
    if service is None:
        return {"seq": 0, "pending": False}
    entries = service.entries()
    return {"seq": entries[-1]["seq"] if entries else 0,
            "pending": bool(service.busy() and not service.open_prompt())}


def render_chat_disabled_page(*, theme=shell.DEFAULT_THEME, nav=None):
    """The GET /chat page while [webui] chat_enabled is false.

    No service exists behind this page — the server never builds one
    while the switch is off — so there is no transcript and no form,
    just the state and the way back on.
    """
    body = ('<p>Web chat is turned off. The chat page is disabled by the '
            '<code>webui.chat_enabled</code> setting, so the bot is not '
            'running here and no message can be sent.</p>'
            '<p>To turn it on, set <strong>Web chat</strong> on in Settings '
            '(or <code>webui.chat_enabled</code> / '
            '<code>PRINT_HANDY__WEBUI__CHAT_ENABLED</code> to '
            '<code>true</code>) and restart Print Handy.</p>')
    return shell.page("chat", body, path="/chat", theme=theme,
                      heading="Chat", nav=nav)


def render_chat_page(service, *, form_token, notice=None, error=None,
                     theme=shell.DEFAULT_THEME, nav=None):
    """The GET /chat page for a WebChatService."""
    # The refresh exists so asynchronously arriving replies show up,
    # but a timed reload also wipes a half-typed message. It is emitted
    # only while a reply is actually pending — the bot is mid-turn and
    # no question is waiting on the operator (an open question is
    # already rendered; the page can sit still for the answer).
    prompt = service.open_prompt()
    pending = service.busy() and not prompt
    entries = service.entries()
    parts = []
    if pending:
        # With scripts on, a small poll of /chat/state reloads the page as
        # soon as a reply lands (the page itself is still rendered here,
        # escaped, never built in the browser). Without scripts the timed
        # refresh in <noscript> does the same, a little later.
        seq = entries[-1]["seq"] if entries else 0
        parts.append(f'<noscript><meta http-equiv="refresh" content="{REFRESH_SECONDS}"></noscript>')
        parts.append(POLL_SCRIPT.replace("__SEQ__", str(int(seq))))
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    body = []
    for entry in entries:
        kind = entry.get("kind")
        if kind == "operator":
            body.append(f'<p class="chat-op"><strong>You:</strong> '
                        f'{_esc(entry.get("content") or "")}</p>')
        elif kind == "message":
            body.append(f'<p class="chat-bot">{_esc(entry.get("content") or "")}</p>')
        elif kind == "prompt":
            body.append(f'<p class="chat-bot"><em>'
                        f'{_esc(entry.get("question") or "")}</em></p>')
    if pending:
        # The browser's typing indicator: the turn is running. Shown from
        # the page render right after Send, so the operator sees the
        # message was taken before the first reply arrives.
        body.append(f'<p class="chat-bot chat-working"><em>{WORKING_TEXT}</em></p>')
    if body:
        parts.append('<div class="chat-log">' + "".join(body) + "</div>")
    else:
        parts.append("<p>Nothing here yet. Say hello, drop a model link, "
                     "or ask for a design — the chat does everything the "
                     "Discord bot does, including printing, through the "
                     "same confirmation questions.</p>")
    if prompt:
        buttons = []
        for index, choice in enumerate(prompt["choices"]):
            buttons.append(
                '<form method="post" action="/chat/choice" class="inline">'
                f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
                f'<input type="hidden" name="clarify_id" '
                f'value="{_esc(prompt["clarify_id"])}">'
                f'<input type="hidden" name="index" value="{index}">'
                f'<button type="submit">{_esc(choice)}</button></form>')
        parts.append('<div class="chat-prompt"><p>'
                     f'{_esc(prompt["question"])}</p>' + "".join(buttons) + "</div>")
    parts.append(
        '<form method="post" action="/chat">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<div class="field"><label for="c-text">Message</label>'
        '<input id="c-text" name="text" autocomplete="off" '
        'placeholder="A model link, /print-help, or describe a design"></div>'
        '<button type="submit" class="accent">Send</button></form>')
    return shell.page("chat", "".join(parts), path="/chat", theme=theme,
                      heading="Chat", nav=nav)


__all__ = ["REFRESH_SECONDS", "chat_state", "render_chat_disabled_page", "render_chat_page"]
