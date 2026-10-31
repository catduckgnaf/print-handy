#!/usr/bin/env python3
"""Container readiness across the bot, the printer listener and local Spoolman."""
import os
import sys
import urllib.error
import urllib.request

# Installed as /usr/local/bin/print-handy-healthcheck: for a script invocation Python
# puts the SCRIPT's directory on sys.path, not /app, so without this the probe dies
# with ModuleNotFoundError (the combined container could never report healthy).
# Same pattern as tests/docker/image_probe.py and docker/slicer-smoke.py.
sys.path.insert(0, "/app")

from print_handy import go2rtc, settings
from print_handy.bot.health import check as bot_check
from print_handy.healthcheck import check as listener_check
from print_handy.healthcheck import listener_domain, listener_required
from print_handy.live_gate import live_config
from print_handy.webui_gate import start_blocked, webui_config


def main():
    healthy = True
    # The same decision the supervisor made (print_handy.bot_gate): the
    # configured platform's own token, not DISCORD_BOT_TOKEN alone.
    from print_handy.bot_gate import decide as bot_decide
    try:
        bot_started, _platform, bot_detail = bot_decide()
    except Exception as exc:
        print(f"FAIL  bot gate: configuration unresolved: {exc}")
        healthy = False
    else:
        if bot_started:
            healthy &= bot_check() == 0
        else:
            print(f"ok  {bot_detail}")
    # The listener is required exactly when the supervisor started it: the
    # listener registry (print_handy.listeners, #299) decides from the
    # instance's domain, mirroring the disabled-bot branch above. Since
    # #336 there is one instance per roster printer: a one-printer roster
    # keeps the original single row; a larger roster gets one row per
    # printer and every required printer's heartbeat must check out. An
    # unresolved configuration fails instead of passing.
    try:
        entries = settings.printers()
    except settings.SettingsError as exc:
        print(f"FAIL  listener: configuration unresolved: {exc}")
        entries, healthy = None, False
    if entries is not None and len(entries) <= 1:
        try:
            required = listener_required()
        except settings.SettingsError as exc:
            print(f"FAIL  listener: configuration unresolved: {exc}")
            required, healthy = None, False
        if required:
            ok, reason = listener_check()
            print(f"{'ok' if ok else 'FAIL'}  listener: {reason}")
            healthy &= ok
        elif required is not None:
            detail = ""
            try:
                domain = listener_domain()
            except Exception:
                domain = None
            if domain is not None:
                state = "has no listener yet" if domain.availability == "planned" \
                    else "has no listener"
                detail = f" (domain {domain.name!r} {state})"
            print(f"ok  listener: not required for this printer connection{detail}")
    elif entries is not None:
        data = settings.current()
        for entry in entries:
            printer_id = entry["id"]
            try:
                required = listener_required(data, printer_id)
            except settings.SettingsError as exc:
                print(f"FAIL  listener ({printer_id}): configuration unresolved: {exc}")
                healthy = False
                continue
            if required:
                ok, reason = listener_check(data, printer_id=printer_id)
                print(f"{'ok' if ok else 'FAIL'}  listener ({printer_id}): {reason}")
                healthy &= ok
            else:
                detail = ""
                try:
                    domain = listener_domain(data, printer_id)
                except Exception:
                    domain = None
                if domain is not None:
                    state = "has no listener yet" if domain.availability == "planned" \
                        else "has no listener"
                    detail = f" (domain {domain.name!r} {state})"
                print(f"ok  listener ({printer_id}): not required for this printer connection{detail}")
    # go2rtc (#246 stage 1) is covered exactly when the supervisor started
    # it: the same gate decision (print_handy.go2rtc), mirroring the
    # disabled-bot branch above. The gate is feature-aware since #311:
    # timelapse capture OR live viewing needing it starts the one child.
    # Neither feature needing it (or one needing it with no camera
    # source) is a healthy, reported state; a feature needing it in an
    # image without the binary (a WITH_GO2RTC=false build) fails with
    # the reason.
    gate = go2rtc.gate()
    if gate == 2:
        print("FAIL  go2rtc: configuration unresolved")
        healthy = False
    elif gate == 0:
        if not go2rtc.binary_present():
            print("FAIL  go2rtc: timelapse capture or live viewing is enabled but "
                  "go2rtc is not in this image (built with WITH_GO2RTC=false)")
            healthy = False
        else:
            ok, reason = go2rtc.check()
            print(f"{'ok' if ok else 'FAIL'}  go2rtc: {reason}")
            healthy &= ok
    try:
        url = ("http://127.0.0.1:" + os.environ.get("SPOOLMAN_PORT", "8000")
               if os.environ.get("PRINT_HANDY_BUNDLED_SPOOLMAN", "true") == "true"
               else os.environ["PRINT_HANDY__SERVICES__SPOOLMAN_URL"].rstrip("/"))
        with urllib.request.urlopen(url + "/api/v1/health", timeout=3) as response:
            healthy &= response.status == 200
            print(f"{'ok' if response.status == 200 else 'FAIL'}  Spoolman: HTTP {response.status}")
    except Exception as exc:
        print(f"FAIL  Spoolman: {exc}")
        healthy = False
    # The Web UI row mirrors the supervisor's webui gate (#286 stage 4):
    # skipped when disabled or portless, exactly like the listener row
    # above. When the supervisor started the server, any HTTP answer from
    # /healthz proves it is up: the status code itself is the bot
    # heartbeat's verdict (200 healthy, 503 stale), which the bot row
    # above already owns, so both mean the Web UI is serving. A token is
    # sent when the environment carries one, matching the server's rule
    # that a configured token guards every route.
    try:
        (webui_enabled, webui_host, webui_port, webui_env_name,
         webui_require_auth) = webui_config()
        webui_resolved = True
    except settings.SettingsError as exc:
        print(f"FAIL  webui: configuration unresolved: {exc}")
        webui_enabled, webui_resolved, healthy = False, False, False
    # #440: an enabled Web UI the gate blocked (non-loopback bind without
    # a token) was never started; that is a healthy, reported state with
    # the fix in the gate's message, not a probe failure.
    webui_blocked = (start_blocked(webui_host, webui_port, webui_env_name,
                                   require_auth=webui_require_auth)
                     if webui_resolved and webui_enabled and webui_port > 0 else None)
    if webui_blocked:
        print(f"ok  webui: not started: {webui_blocked}")
    elif webui_resolved and webui_enabled and webui_port > 0:
        probe_host = "127.0.0.1" if webui_host in ("0.0.0.0", "::") else webui_host
        request = urllib.request.Request(
            f"http://{probe_host}:{webui_port}/healthz")
        token = os.environ.get(webui_env_name, "")
        if token:
            request.add_header("X-WebUI-Token", token)
        status = None
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:
            print(f"FAIL  webui: {exc}")
            healthy = False
        if status is not None:
            if status in (200, 503):
                print(f"ok  webui: serving on {webui_host}:{webui_port} (HTTP {status})")
            else:
                print(f"FAIL  webui: HTTP {status} from /healthz")
                healthy = False
    elif webui_resolved and webui_enabled:
        print("ok  webui: enabled but no port chosen; not started")
    elif webui_resolved:
        print("ok  webui: disabled")
    # The live-view row mirrors the supervisor's live gate (#311 stage 4)
    # through the same shared helper (print_handy.live_gate.live_config):
    # skipped as healthy when the feature is disabled, reported with its
    # missing pieces when enabled but incompletely configured (the
    # supervisor started nothing then), and probed on its open /health
    # route when the supervisor started the Activity server. The probe
    # reveals no configuration: /health answers only {"ok": true, ...}.
    try:
        live = live_config()
        live_resolved = True
    except settings.SettingsError as exc:
        print(f"FAIL  live view: configuration unresolved: {exc}")
        live, live_resolved, healthy = None, False, False
    if live_resolved and live.ready:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{live.port}/health", timeout=3) as response:
                status = response.status
        except Exception as exc:
            print(f"FAIL  live view: {exc}")
            healthy = False
        else:
            if status == 200:
                print(f"ok  live view: Activity server serving on 127.0.0.1:{live.port}")
            else:
                print(f"FAIL  live view: HTTP {status} from /health")
                healthy = False
    elif live_resolved and live.enabled:
        print("ok  live view: enabled but not configured; not started ("
              + "; ".join(live.missing) + ")")
    elif live_resolved:
        print("ok  live view: disabled")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
