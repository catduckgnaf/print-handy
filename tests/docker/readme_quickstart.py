#!/usr/bin/env python3
"""The README's Docker quickstart, run as written, to a `/print-help` reply. Hosted CI only.

    python3 tests/docker/readme_quickstart.py LOCAL_IMAGE

A newcomer reads README.md "Get started" and nothing else: runs the copy-paste block, edits `.env`
as the bullets under it say, runs the start block, then sends `/print-help`. This script does the
same in a fresh temporary directory, so the README cannot drift away from a working first run:

1. The bash blocks of the "Get started" section are taken from README.md itself (everything before
   its first subsection). The last one must be the start block (`docker compose up -d`).
2. One substitution, and only one: the download base
   `https://raw.githubusercontent.com/catduckgnaf/print-handy/<tag>/` becomes `file://<checkout>/`,
   so the block fetches this checkout's `docker-compose.yml` and `.env.example` instead of the last
   release's. Every other character of every block runs verbatim.
3. The `.env` edits the README lists: `PRINT_HANDY_IMAGE` is the locally built image (CI has no
   published digest to pin), `PRINT_HANDY__SERVICES__MOONRAKER_URL` is a stand-in Moonraker
   (`fake_moonraker.py`, an idle printer that never accepts a print) on this host, and
   `DISCORD_BOT_TOKEN` stays empty: there is no Discord here.
4. `/print-help` goes through the Web UI's web chat, which is the full bot (BotCore) on the
   `webui` platform, so the reply is the same command list Discord would get.

Nothing is pushed or published, and no secret is read.
"""
import http.client
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
RAW_BASE = re.compile(r"https://raw\.githubusercontent\.com/catduckgnaf/print-handy/[^/\s]+/")
# The opening line of print_handy.bot.commands.help_text(); tests/test_readme_quickstart.py pins it.
HELP_MARKERS = ("prints from this channel.", "Nothing starts without a button press.")
# The .env keys the README's bullets tell a newcomer to fill in.
ENV_KEYS = ("PRINT_HANDY_IMAGE", "DISCORD_BOT_TOKEN", "PRINT_HANDY__SERVICES__MOONRAKER_URL")
WEBUI_PORT = 8088
FAKE_PRINTER_PORT = 7125


class QuickstartError(AssertionError):
    pass


def get_started_section(text):
    """The README's "Get started" section up to its first subsection or the next section."""
    match = re.search(r"^## Get started[ \t]*\n(.*?)(?=^#{2,3} |\Z)", text, re.S | re.M)
    if not match:
        raise QuickstartError('README.md has no "## Get started" section')
    return match.group(1)


def quickstart_blocks(text):
    """(setup_script, start_script, section) from README text, checked for the expected shape."""
    section = get_started_section(text)
    blocks = re.findall(r"^```bash[ \t]*\n(.*?)^```[ \t]*$", section, re.S | re.M)
    if len(blocks) < 2:
        raise QuickstartError(f'"Get started" has {len(blocks)} bash block(s); expected a setup '
                              "block and a start block")
    start = blocks[-1]
    if not re.search(r"^docker compose up -d\b", start, re.M):
        raise QuickstartError('the last "Get started" bash block does not run `docker compose up -d`')
    for needle in ("/print-help", str(WEBUI_PORT), ".env") + ENV_KEYS:
        if needle not in section:
            raise QuickstartError(f'"Get started" no longer mentions {needle!r}')
    return "".join(blocks[:-1]), start, section


def localize_downloads(script, checkout):
    """The one substitution: release download URLs point at this checkout. Returns (script, count)."""
    base = "file://" + Path(checkout).resolve().as_posix().rstrip("/") + "/"
    return RAW_BASE.subn(lambda _match: base, script)


def set_env_value(text, key, value):
    """Set KEY=value the way a newcomer would: on the live KEY= line, else the commented one.

    A commented example can sit above the live line (`# KEY=example` then `KEY=`). Editing the
    comment would leave the later, empty assignment in force, so the live line wins.
    """
    name = re.escape(key)
    for pattern in (re.compile(rf"^{name}=.*$", re.M), re.compile(rf"^#[ \t]*{name}=.*$", re.M)):
        if pattern.search(text):
            return pattern.sub(lambda _match: f"{key}={value}", text, count=1)
    raise QuickstartError(f".env has no {key} line for the README's edit step")


def _run(args, **kwargs):
    print("+", " ".join(args) if isinstance(args, list) else args, flush=True)
    return subprocess.run(args, check=True, **kwargs)


def _host_address():
    """This host's first address, as a newcomer would type their printer's LAN address."""
    out = subprocess.run(["hostname", "-I"], check=True, capture_output=True, text=True).stdout
    for address in out.split():
        if "." in address and not address.startswith("127."):
            return address
    raise QuickstartError("no IPv4 address on this host for the stand-in printer")


def _compose(qdir, env, *args, **kwargs):
    return subprocess.run(["docker", "compose", *args], cwd=qdir, env=env, **kwargs)


def _wait_healthy(qdir, env, timeout=420):
    deadline = time.monotonic() + timeout
    status = "unknown"
    while time.monotonic() < deadline:
        cid = _compose(qdir, env, "ps", "-q", "combined", capture_output=True, text=True).stdout.strip()
        if cid:
            state = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", cid],
                capture_output=True, text=True).stdout.split()
            status = " ".join(state)
            if state[:1] == ["exited"]:
                raise QuickstartError(f"combined container exited before it was healthy ({status})")
            if state[1:2] == ["healthy"]:
                print(f"combined container: {status}", flush=True)
                return
        time.sleep(5)
    raise QuickstartError(f"combined container not healthy after {timeout}s ({status})")


def _request(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", WEBUI_PORT, timeout=60)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.getheader("Location") or "", response.read().decode("utf-8", "replace")
    finally:
        conn.close()


def _wait_webui(timeout=120):
    """Wait until the Web UI answers HTTP on its port.

    Polls /healthz and takes any answer, 503 included: that route is the chat
    bot's heartbeat verdict, and with no platform token in CI the bot never
    starts, so it stays 503 while the Web UI and its in-process chat work. Not
    / either: the first browser navigation to / is the one-time redirect to the
    walkthrough (walkthrough.offer_once), which ask_for_help checks, so the
    wait must not spend it.
    """
    deadline = time.monotonic() + timeout
    last = "no connection"
    while time.monotonic() < deadline:
        try:
            status, _, _ = _request("GET", "/healthz")
            return status
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    raise QuickstartError(f"Web UI on port {WEBUI_PORT} did not answer HTTP within {timeout}s ({last})")


def ask_for_help(timeout=180):
    """Send /print-help through the Web UI chat; return the page once the help reply is on it."""
    html_headers = {"Accept": "text/html"}
    _wait_webui()
    status, location, _ = _request("GET", "/", headers=html_headers)
    # The README: "Open http://<this-machine>:8088 for the Web UI and its first-run walkthrough",
    # with no password until one is set.
    if status not in (302, 303) or not location.endswith("/walkthrough"):
        raise QuickstartError(f"GET / on first boot gave {status} {location!r}, not the walkthrough")
    status, _, page = _request("GET", "/chat", headers=html_headers)
    if status != 200:
        raise QuickstartError(f"GET /chat gave {status}; the Web UI should be open on first boot")
    token = re.search(r'name="_csrf" value="([^"]+)"', page)
    if not token:
        raise QuickstartError("the chat page has no message form")
    body = urllib.parse.urlencode({"_csrf": token.group(1), "text": "/print-help"})
    status, location, _ = _request("POST", "/chat", body=body, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    if status not in (302, 303) or not location.endswith("/chat"):
        raise QuickstartError(f"POST /chat gave {status} {location!r}, not the redirect back to /chat")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, page = _request("GET", "/chat", headers=html_headers)
        if status == 200 and all(marker in page for marker in HELP_MARKERS):
            return page
        time.sleep(3)
    raise QuickstartError(f"no /print-help reply on the chat page within {timeout}s")


def main(argv):
    if len(argv) != 2:
        print(__doc__.splitlines()[2].strip(), file=sys.stderr)
        return 2
    image = argv[1]
    setup, start, _ = quickstart_blocks(README.read_text(encoding="utf-8"))
    setup, replaced = localize_downloads(setup, REPO)
    if replaced < 1:
        raise QuickstartError("the setup block downloads nothing from the release; update this check")
    if "raw.githubusercontent.com" in setup + start:
        raise QuickstartError("a download URL the substitution did not cover is left in the blocks")
    print(f"README setup block ({replaced} download URL(s) pointed at this checkout):\n{setup}"
          f"README start block:\n{start}", flush=True)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PRINT_HANDY", "DISCORD_", "COMPOSE_"))}
    work = Path(tempfile.mkdtemp(prefix="ph-readme-quickstart-"))
    printer = f"ph-readme-printer-{os.getpid()}"
    qdir = None
    ok = False
    try:
        _run(["docker", "run", "-d", "--rm", "--name", printer, "-p", f"{FAKE_PRINTER_PORT}:{FAKE_PRINTER_PORT}",
              "-v", f"{REPO / 'tests' / 'docker'}:/fixture:ro", "--entrypoint", "python3", image,
              "/fixture/fake_moonraker.py", str(FAKE_PRINTER_PORT)], env=env)
        marker = work / ".quickstart-dir"
        _run(["bash", "-euo", "pipefail", "-c", setup + f'pwd > "{marker}"\n'], cwd=work, env=env)
        qdir = Path(marker.read_text().strip())
        env_file = qdir / ".env"
        text = env_file.read_text()
        for key in ENV_KEYS:
            if not re.search(rf"^#?[ \t]*{key}=", text, re.M):
                raise QuickstartError(f"the downloaded .env has no {key} line to fill in")
        text = set_env_value(text, "PRINT_HANDY_IMAGE", image)
        text = set_env_value(text, "PRINT_HANDY__SERVICES__MOONRAKER_URL",
                             f"http://{_host_address()}:{FAKE_PRINTER_PORT}")
        env_file.write_text(text)
        _run(["bash", "-euo", "pipefail", "-c", start], cwd=qdir, env=env)
        _wait_healthy(qdir, env)
        page = ask_for_help()
        reply = re.search(r'<p class="chat-bot">([^<]*prints from this channel[^<]*)</p>', page)
        print("web chat reply to /print-help:", (reply.group(1) if reply else HELP_MARKERS[0])[:400])
        ok = True
        print("README quickstart to a /print-help reply: PASS")
        return 0
    finally:
        if qdir is not None:
            if not ok:
                _compose(qdir, env, "ps")
                _compose(qdir, env, "logs", "--no-color", "--tail", "300")
            _compose(qdir, env, "down", "--volumes", "--remove-orphans", capture_output=True)
        if not ok:
            subprocess.run(["docker", "logs", "--tail", "50", printer])
        subprocess.run(["docker", "rm", "-f", printer], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            subprocess.run(["sudo", "rm", "-rf", str(work)])


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (QuickstartError, subprocess.CalledProcessError) as exc:
        print(f"README quickstart: FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
