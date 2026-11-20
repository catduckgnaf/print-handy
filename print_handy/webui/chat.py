"""The Web UI chat page: the bot, in the browser.

Renders the web chat service's transcript (operator messages, bot
replies, button questions) and the forms that feed it: a message
form (``POST /chat``), an upload-a-part-to-edit form
(``POST /chat/upload``, #1721), and, while a question is open, one
button per choice (``POST /chat/choice``). While a bot reply is pending the page
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

from . import transcript
from . import shell

_esc = shell._esc
REFRESH_SECONDS = 3
WORKING_TEXT = "Working on it…"
POLL_FAST_MS = 600     # poll interval right after a message is sent
POLL_SLOW_MS = 3000    # the interval it backs off to while the turn keeps running
# Reload when the transcript grew or the turn ended. Only numbers and a
# boolean come back from the state route; nothing from it is put on the page.
POLL_SCRIPT = (
    "<script>(function(){var seq=__SEQ__,wait=" + str(POLL_FAST_MS) + ";"
    "function tick(){fetch('__STATE__',{credentials:'same-origin',cache:'no-store'})"
    ".then(function(r){return r.ok?r.json():null;}).then(function(s){"
    "if(s&&(s.seq!==seq||!s.pending)){location.reload();return;}"
    "wait=Math.min(wait*1.5," + str(POLL_SLOW_MS) + ");setTimeout(tick,wait);})"
    ".catch(function(){setTimeout(tick," + str(POLL_SLOW_MS) + ");});}"
    "setTimeout(tick,wait);})();</script>")


def poll_script(seq, state_path="/chat/state"):
    """The reload-on-reply poll for a conversation whose state is served at
    ``state_path`` (one of our fixed routes, never request text), from the
    transcript position ``seq`` the page was rendered at. Chat, Workflow
    and Designs all poll this way."""
    return POLL_SCRIPT.replace("__STATE__", state_path).replace("__SEQ__", str(int(seq)))


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


# Slash commands another page may hand the chat as a starting message
# (``/chat?text=...``). The text is only typed into the box, never sent:
# the operator still presses Send, so a link cannot start a flow.
PREFILL_COMMANDS = ("/nozzle change",)


def prefill_text(raw):
    """The allowed prefill for a ``text`` query value, or ""."""
    text = " ".join(str(raw or "").split())
    return text if text in PREFILL_COMMANDS else ""


def render_chat_page(service, *, form_token, notice=None, error=None,
                     theme=shell.DEFAULT_THEME, nav=None, prefill="",
                     ask_ai_default=False, active_design=None):
    """The GET /chat page for a WebChatService.

    The AI lane is selected by server configuration, not a per-message
    checkbox. Deterministic commands and print confirmations continue
    through the normal bot pipeline."""
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
        parts.append(poll_script(seq))
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    if active_design is not None:
        from . import designs as designs_module
        return designs_module.render_designs_page(
            designs_module.catalog_rows(active_design["data"]),
            theme=theme, nav=nav, workshop=active_design["workshop"],
            form_token=form_token, notice=notice, error=error, route="/chat")
    # The working line is the browser's typing indicator: the turn is
    # running, so the operator sees the message was taken before the first
    # reply arrives.
    parts.append(transcript.render_log(
        entries, open_prompt=prompt, working=pending,
        empty="<p>Nothing here yet. Paste a model link, or describe something "
              "to design. Chat does everything the Discord bot "
              "does, including printing, through the same confirmation questions.</p>"))
    parts.append(transcript.render_prompt(prompt, action="/chat/choice", form_token=form_token))
    parts.append(
        '<form method="post" action="/chat">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<div class="field"><label for="c-text">Message</label>'
        '<input id="c-text" name="text" autocomplete="off" '
        + (f'value="{_esc(prefill_text(prefill))}" autofocus ' if prefill_text(prefill) else "")
        + 'placeholder="A model link, or describe a design"></div>'
        '<button type="submit" class="accent">Send</button></form>')
    # Upload a part to edit it (#1721): the file rides a /print-design
    # message into this chat, the same edit a dropped file starts on
    # Discord. The change is optional — with none, the part is shown as
    # it is and the next message here makes the change. (Uploading a
    # file to print it, without editing, stays on the Upload and
    # Workflow pages.)
    parts.append(
        '<form method="post" action="/chat/upload" '
        'enctype="multipart/form-data" class="chat-upload">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<div class="field"><label for="c-file">Upload a part to edit '
        '(.stl, .3mf or .scad)</label>'
        '<input id="c-file" type="file" name="file" '
        'accept=".stl,.3mf,.scad"></div>'
        '<div class="field"><label for="c-change">Change to make '
        '(optional)</label>'
        '<input id="c-change" name="change" autocomplete="off" '
        'placeholder="make it 5 mm taller"></div>'
        '<button type="submit" class="accent">Upload and edit</button></form>')
    return shell.page("chat", "".join(parts), path="/chat", theme=theme,
                      heading="Chat", nav=nav)


__all__ = ["REFRESH_SECONDS", "chat_state", "poll_script", "render_chat_disabled_page", "render_chat_page"]
