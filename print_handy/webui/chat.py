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


def render_chat_page(service, *, form_token, notice=None, error=None,
                     theme=shell.DEFAULT_THEME, nav=None):
    """The GET /chat page for a WebChatService."""
    # The refresh exists so asynchronously arriving replies show up,
    # but a timed reload also wipes a half-typed message. It is emitted
    # only while a reply is actually pending — the bot is mid-turn and
    # no question is waiting on the operator (an open question is
    # already rendered; the page can sit still for the answer).
    prompt = service.open_prompt()
    parts = []
    if service.busy() and not prompt:
        parts.append(f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">')
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    body = []
    for entry in service.entries():
        kind = entry.get("kind")
        if kind == "operator":
            body.append(f'<p class="chat-op"><strong>You:</strong> '
                        f'{_esc(entry.get("content") or "")}</p>')
        elif kind == "message":
            body.append(f'<p class="chat-bot">{_esc(entry.get("content") or "")}</p>')
        elif kind == "prompt":
            body.append(f'<p class="chat-bot"><em>'
                        f'{_esc(entry.get("question") or "")}</em></p>')
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


__all__ = ["REFRESH_SECONDS", "render_chat_page"]
