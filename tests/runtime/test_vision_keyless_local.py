"""A local OpenAI-compatible vision server needs no key; a hosted one still does.

Ollama, LM Studio or llama.cpp on this machine or the LAN usually take no
API key, so the one AI endpoint (``llm.chat_url``, #1731) pointed there
must work without its key. A hosted endpoint without its key would fail
every check and post a false alert each time, so the watch stays off
there as before.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from print_handy.runtime import visual_watch as vw


# These tests exercise the vision endpoint routes, which run only with AI
# camera checks on (vision.ai_enabled, off by default since #1376).
pytestmark = pytest.mark.usefixtures("vision_ai_on")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/v1/chat/completions",
    "http://localhost:1234/v1/chat/completions",
    "http://192.168.1.20:11434/v1/chat/completions",
    "http://10.0.0.5/v1/chat/completions",
    "http://ollama:11434/v1/chat/completions",
    "http://tower.local:8080/v1/chat/completions",
    "http://[::1]:11434/v1/chat/completions",
])
def test_local_and_lan_endpoints_need_no_key(url):
    assert vw.keyless_ok(url)


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1/chat/completions",
    "https://api.cloudflare.com/client/v4/accounts/x/ai/v1/chat/completions",
    "https://8.8.8.8/v1/chat/completions",
    "",
    None,
])
def test_hosted_or_missing_endpoints_still_need_a_key(url):
    assert not vw.keyless_ok(url)


def _point_endpoint(monkeypatch, url, model="m"):
    cfg = vw._config.cfg()
    monkeypatch.setitem(cfg, "llm", {"chat_url": url, "model": model,
                                     "api_key_env": "LLM_API_KEY", "timeout_s": 20})


def test_vision_ready_follows_the_endpoint(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(vw.local_vision, "detector_ready", lambda: False)
    _point_endpoint(monkeypatch, "http://192.168.1.20:11434/v1/chat/completions")
    assert vw.vision_ready()
    _point_endpoint(monkeypatch, "https://api.openai.com/v1/chat/completions")
    assert not vw.vision_ready()
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    assert vw.vision_ready()


def test_keyless_local_request_sends_no_authorization_header(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append(self.headers.get("Authorization"))
            body = json.dumps({"choices": [{"message": {"content":
                                                        '{"verdict": "ok", "reason": "fine"}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/v1/chat/completions"
        _point_endpoint(monkeypatch, url, model="llava")
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        answer = vw._vision_answer(b"\xff\xd8\xff\xd9", "prompt", context="")
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert answer == {"verdict": "ok", "reason": "fine"}
    assert seen == [None]


def test_hosted_endpoint_without_a_key_still_raises(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    _point_endpoint(monkeypatch, "https://api.openai.com/v1/chat/completions")
    with pytest.raises(vw._VisionKeyMissing):
        vw._vision_answer(b"x", "p", context="")
