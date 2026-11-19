"""One conversation transcript for every Web UI chat surface.

Chat, Designs and Workflow all show a web chat conversation: the
operator's messages, the bot's replies, an open question with its
buttons, and a "working" line while a turn runs. They used to carry three
copies of that loop, each pouring a reply's text into one paragraph, so
line breaks collapsed and the bot's chat formatting (bold, code, lists)
showed as raw asterisks and backticks. This module is the one renderer:

- ``render_log(entries, ...)``: the thread, oldest first, the operator's
  messages and the bot's replies visibly distinct.
- ``render_prompt(prompt, ...)``: the open question with its buttons.
- ``render_text(text)``: a reply's text with the small Markdown subset the
  chat replies use, escaped first so nothing in a reply is ever markup.

Everything is server-rendered and escaped; the browser builds nothing.
"""
from __future__ import annotations

import html
import re

WORKING_TEXT = "Working on it…"

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"(https?://[^\s<>\"']*[^\s<>\"'.,;:!?)\]])")
_BULLET = re.compile(r"^\s*[-*•]\s+")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+")


def _esc(value):
    return html.escape(str(value), quote=True)


def _inline(line: str) -> str:
    """One line: `code`, bare links and **bold**, every piece escaped.

    Code spans and links are found in the raw text, before escaping, so a
    link's end is never confused with an escaped entity, and each piece of
    text is escaped on its own before it is wrapped in markup.
    """
    out = []
    for part in re.split(r"(`[^`\n]+`)", str(line)):
        code = _CODE.fullmatch(part)
        if code:
            out.append(f"<code>{_esc(code.group(1))}</code>")
            continue
        for piece in _LINK.split(part):
            if piece and _LINK.fullmatch(piece):
                out.append(f'<a href="{_esc(piece)}" rel="noopener noreferrer">{_esc(piece)}</a>')
            else:
                out.append(_BOLD.sub(r"<strong>\1</strong>", _esc(piece)))
    return "".join(out)


def render_text(text) -> str:
    """A reply as HTML blocks: paragraphs, line breaks and simple lists."""
    blocks, para, items, ordered = [], [], [], False

    def flush_para():
        if para:
            blocks.append("<p>" + "<br>".join(_inline(line) for line in para) + "</p>")
            para.clear()

    def flush_list():
        if items:
            tag = "ol" if ordered else "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items)
                          + f"</{tag}>")
            items.clear()

    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        bullet, numbered = _BULLET.match(line), _NUMBERED.match(line)
        if bullet or numbered:
            flush_para()
            if items and ordered != bool(numbered):
                flush_list()
            ordered = bool(numbered)
            items.append(line[(bullet or numbered).end():])
        elif not line.strip():
            flush_para()
            flush_list()
        else:
            flush_list()
            para.append(line)
    flush_para()
    flush_list()
    return "".join(blocks)


def _bubble(who: str, body: str, label: str) -> str:
    return (f'<li class="chat-msg chat-{who}"><span class="chat-who">{label}</span>'
            f'<div class="chat-body">{body}</div></li>')


def render_log(entries, *, open_prompt=None, working=False, empty="") -> str:
    """The thread for ``entries`` (oldest first), or ``empty`` when there is none.

    A question that is still open is left out of the log: it is shown once,
    with its buttons, by ``render_prompt``. A question already answered stays
    in the log as the bot's message, followed by the note it was closed with.
    """
    open_id = str((open_prompt or {}).get("clarify_id") or "")
    rows = []
    for entry in entries or ():
        kind = entry.get("kind")
        if kind == "operator":
            rows.append(_bubble("op", render_text(entry.get("content")), "You"))
        elif kind == "message":
            rows.append(_bubble("bot", render_text(entry.get("content")), "Print Handy"))
        elif kind == "prompt":
            if open_id and str(entry.get("clarify_id") or "") == open_id:
                continue
            rows.append(_bubble("bot", render_text(entry.get("question")), "Print Handy"))
        elif kind == "prompt_closed" and entry.get("note"):
            rows.append(f'<li class="chat-note">{_inline(entry.get("note"))}</li>')
        elif kind == "image" and entry.get("url"):
            caption = entry.get("caption") or ""
            rows.append(_bubble("bot", f'<figure><img src="{_esc(entry["url"])}" '
                                       f'alt="{_esc(caption or "Preview")}" loading="lazy">'
                                       + (f"<figcaption>{_inline(caption)}</figcaption>"
                                          if caption else "") + "</figure>", "Print Handy"))
    if working:
        rows.append(f'<li class="chat-msg chat-bot chat-working" aria-live="polite">'
                    f'<span class="chat-who">Print Handy</span>'
                    f'<div class="chat-body"><p>{WORKING_TEXT}</p></div></li>')
    if not rows:
        return empty
    return '<ol class="chat-log">' + "".join(rows) + "</ol>"


def render_prompt(prompt, *, action: str, form_token: str) -> str:
    """The open question and its choice buttons, each answered through ``action``."""
    if not prompt:
        return ""
    buttons = []
    for index, choice in enumerate(prompt.get("choices") or ()):
        style = ' class="secondary"' if index else ""
        buttons.append(
            f'<form method="post" action="{_esc(action)}" class="inline">'
            f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
            f'<input type="hidden" name="clarify_id" value="{_esc(prompt["clarify_id"])}">'
            f'<input type="hidden" name="index" value="{index}">'
            f'<button type="submit"{style}>'
            f'{_esc(choice)}</button></form>')
    return ('<div class="chat-prompt" role="group" aria-label="Question">'
            f'<div class="chat-body">{render_text(prompt.get("question"))}</div>'
            f'<div class="chat-choices">{"".join(buttons)}</div></div>')


__all__ = ["WORKING_TEXT", "render_log", "render_prompt", "render_text"]
