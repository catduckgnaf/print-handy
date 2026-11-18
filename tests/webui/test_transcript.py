"""The one conversation renderer behind Chat, Designs and Workflow."""
from print_handy.webui import transcript as t


def test_replies_keep_their_line_breaks_bold_code_and_lists():
    html = t.render_text("**Print Handy** ready.\nUse `/print-help`:\n- one\n- two\n\n1. first\n2) second")
    assert html == ("<p><strong>Print Handy</strong> ready.<br>Use <code>/print-help</code>:</p>"
                    "<ul><li>one</li><li>two</li></ul><ol><li>first</li><li>second</li></ol>")


def test_reply_text_is_escaped_before_any_formatting():
    html = t.render_text('<script>alert(1)</script> **<img src=x onerror=y>** `<b>`')
    assert "<script>" not in html and "<img" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "<strong>&lt;img src=x onerror=y&gt;</strong>" in html
    assert "<code>&lt;b&gt;</code>" in html


def test_links_are_clickable_and_cannot_break_out_of_the_attribute():
    html = t.render_text('see https://example.com/a?b=1&c=2. now')
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html  # the full stop is not part of it
    assert 'rel="noopener noreferrer"' in html
    hostile = t.render_text('https://e.example/"onmouseover="alert(1) and **https://e.example/x**')
    assert 'href="https://e.example/"' in hostile and "onmouseover=" not in hostile.split("</a>")[0]
    assert "&quot;onmouseover=&quot;alert(1)" in hostile


def test_the_log_marks_who_said_what_in_order():
    html = t.render_log([{"kind": "operator", "content": "hello"},
                         {"kind": "message", "content": "hi"}])
    assert html.index("chat-op") < html.index("chat-bot")
    assert ">You<" in html and ">Print Handy<" in html


def test_an_open_question_is_shown_once_with_its_buttons_not_twice():
    entries = [{"kind": "prompt", "clarify_id": "q1", "question": "Slice it?", "choices": ["Yes", "No"]}]
    prompt = {"clarify_id": "q1", "question": "Slice it?", "choices": ["Yes", "No"]}
    log = t.render_log(entries, open_prompt=prompt)
    box = t.render_prompt(prompt, action="/chat/choice", form_token="tok")
    assert "Slice it?" not in log
    assert box.count("Slice it?") == 1
    assert box.count('action="/chat/choice"') == 2 and 'name="index" value="1"' in box
    assert 'name="_csrf" value="tok"' in box


def test_an_answered_question_stays_in_the_thread_with_its_note():
    html = t.render_log([{"kind": "prompt", "clarify_id": "q0", "question": "Which color?"},
                         {"kind": "prompt_closed", "clarify_id": "q0", "note": "You chose **Red**"}],
                        open_prompt=None)
    assert "Which color?" in html and "<strong>Red</strong>" in html


def test_working_line_and_empty_state():
    assert "chat-working" in t.render_log([], working=True)
    assert t.render_log([], empty="<p>Nothing yet</p>") == "<p>Nothing yet</p>"
    assert t.render_prompt(None, action="/x", form_token="t") == ""


def test_an_image_entry_shows_the_picture_in_the_thread():
    html = t.render_log([{"kind": "image", "url": "/designs/abc/preview?turn=2", "caption": "Turn 2"}])
    assert '<img src="/designs/abc/preview?turn=2" alt="Turn 2"' in html
    assert "<figcaption>Turn 2</figcaption>" in html
