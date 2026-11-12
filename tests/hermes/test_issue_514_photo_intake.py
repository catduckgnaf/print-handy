"""Photo/OCR spool intake for the new-spool walkthrough (#514).

Split out of #512 §4: when a vision endpoint is available, the walkthrough's
first step offers "Use a photo of the label"; the operator sends one photo,
spool_label extracts a validated spec through the shared vision call, and
the values pre-fill the walkthrough at its confirmation step. These tests
pin the extraction parser and its named failures, the availability gate,
the confirm-before-create flow with a fake vision call, and the rule that
label temperatures are recorded on the Spoolman filament. No live Discord,
Spoolman or vision endpoint is used anywhere here.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.runtime import moonraker_event_listener as mel
from print_handy.runtime import print_alert_buttons as pab
from print_handy.runtime import spool_codes, spool_label, spool_sync, visual_watch as vw


# These tests exercise the vision endpoint routes, which run only with AI
# camera checks on (vision.ai_enabled, off by default since #1376).
pytestmark = pytest.mark.usefixtures("vision_ai_on")

SPEC = importlib.util.spec_from_file_location(
    "pf_issue_514", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


# ------------------------------------------------------------- extraction parser

def _vision(answer):
    return lambda image, prompt: answer


def test_extract_full_label_spec():
    spec = spool_label.extract_spool_spec(b"jpeg", _vision({
        "brand": "Sunlu", "material": "Silk PLA", "color": "Rose Red Blue Green",
        "grams": 500, "nozzle_temp": 220, "bed_temp": 60}))
    assert spec == {"brand": "Sunlu", "material": "Silk PLA", "color": "Rose Red Blue Green",
                    "grams": 500.0, "nozzle_temp": 220.0, "bed_temp": 60.0}


def test_extract_generic_material_without_temps_or_brand():
    spec = spool_label.extract_spool_spec(b"jpeg", _vision({
        "material": "PETG", "color": "Teal", "grams": 1000}))
    assert spec["material"] == "PETG" and spec["brand"] is None
    assert spec["nozzle_temp"] is None and spec["bed_temp"] is None


def test_extract_prompt_asks_for_json_only():
    seen = {}

    def call(image, prompt):
        seen["prompt"] = prompt
        return {"material": "PLA", "color": "Black", "grams": 1000}

    spool_label.extract_spool_spec(b"jpeg", call)
    assert "JSON" in seen["prompt"] and "grams" in seen["prompt"]


def test_extract_accepts_fenced_json_string_answer():
    spec = spool_label.extract_spool_spec(b"jpeg", _vision(
        '```json\n{"material": "PLA+", "color": "Matte White", "grams": 1000}\n```'))
    assert spec["material"] == "PLA+" and spec["grams"] == 1000.0


def test_extract_weight_units():
    assert spool_label.extract_spool_spec(b"j", _vision(
        {"material": "PLA", "color": "Black", "grams": "1 kg"}))["grams"] == 1000.0
    assert spool_label.extract_spool_spec(b"j", _vision(
        {"material": "PLA", "color": "Black", "grams": "250 g"}))["grams"] == 250.0


def test_extract_temp_range_reads_midpoint_and_bad_temp_is_dropped():
    spec = spool_label.extract_spool_spec(b"j", _vision({
        "material": "PLA", "color": "Black", "grams": 1000,
        "nozzle_temp": "215-230", "bed_temp": 900}))
    assert spec["nozzle_temp"] == 222.5
    assert spec["bed_temp"] is None


@pytest.mark.parametrize("answer", [None, {"unreadable": True}])
def test_extract_unreadable_failures(answer):
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec(b"jpeg", _vision(answer))
    assert caught.value.reason == "unreadable"
    assert "couldn't read" in str(caught.value)


def test_extract_vision_call_raising_is_unreadable():
    def boom(image, prompt):
        raise RuntimeError("endpoint down")

    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec(b"jpeg", boom)
    assert caught.value.reason == "unreadable"


def test_extract_empty_image_is_unreadable_without_calling():
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec(b"", _vision({"material": "PLA", "color": "B", "grams": 1}))
    assert caught.value.reason == "unreadable"


@pytest.mark.parametrize("answer", ["not json at all", "[1, 2]", 42])
def test_extract_non_json_failures(answer):
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec(b"jpeg", _vision(answer))
    assert caught.value.reason == "non_json"


@pytest.mark.parametrize("answer,missing", [
    ({"color": "Black", "grams": 1000}, "material"),
    ({"material": "PLA", "grams": 1000}, "colour"),
    ({"material": "PLA", "color": "Black"}, "weight"),
    ({"material": "PLA", "color": "Black", "grams": None}, "weight"),
])
def test_extract_missing_fields_are_named(answer, missing):
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec(b"jpeg", _vision(answer))
    assert caught.value.reason == "missing_fields"
    assert missing in str(caught.value)


# ------------------------------------------------------------- shared vision call

class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def test_visual_extract_vision_url_route_returns_object(monkeypatch):
    requests = []

    def fake_urlopen(req, timeout=None):
        requests.append((req, timeout))
        return _Response(json.dumps({"material": "PLA", "color": "Black", "grams": 1000}).encode())

    monkeypatch.setenv("PRINT_VISION_URL", "http://vision.invalid/extract")
    monkeypatch.setattr(vw.urllib.request, "urlopen", fake_urlopen)
    out = vw.visual_extract(b"jpeg", "prompt", chat_url=lambda: "unused",
                            model="m", api_key=lambda: None, context="")
    assert out == {"material": "PLA", "color": "Black", "grams": 1000}
    body = json.loads(requests[0][0].data)
    assert set(body) == {"image", "prompt"} and requests[0][1] == 30


def test_visual_extract_chat_route_returns_raw_string_when_not_json(monkeypatch):
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    payload = {"choices": [{"message": {"content": "sorry, blurry"}}]}

    def fake_urlopen(req, timeout=None):
        return _Response(json.dumps(payload).encode())

    monkeypatch.setattr(vw.urllib.request, "urlopen", fake_urlopen)
    out = vw.visual_extract(b"jpeg", "prompt", chat_url=lambda: "http://chat.invalid",
                            model="m", api_key=lambda: "key", context="")
    assert out == "sorry, blurry"


def test_visual_extract_failure_returns_none(monkeypatch):
    monkeypatch.setenv("PRINT_VISION_URL", "http://vision.invalid/extract")

    def boom(req, timeout=None):
        raise OSError("unreachable")

    monkeypatch.setattr(vw.urllib.request, "urlopen", boom)
    assert vw.visual_extract(b"jpeg", "p", chat_url=lambda: "u", model="m",
                             api_key=lambda: None, context="") is None


def test_visual_verdict_still_reads_the_shared_transport(monkeypatch):
    monkeypatch.setenv("PRINT_VISION_URL", "http://vision.invalid/extract")
    monkeypatch.setattr(vw.urllib.request, "urlopen",
                         lambda req, timeout=None: _Response(
                             json.dumps({"verdict": "OK", "reason": "fine"}).encode()))
    verdict, detail = vw.visual_verdict(b"frame", "prompt", chat_url=lambda: "u",
                                        model="m", api_key=lambda: None, context="")
    assert (verdict, detail) == ("ok", "fine")


# ------------------------------------------------------------- availability gate

def test_photo_button_only_when_offered():
    plain = [b["custom_id"] for row in pab.intake_material_components("aid") for b in row["components"]]
    assert plain[0].startswith(pab.INTAKE_SELECT_PREFIX)  # the material dropdown
    assert not any(":photo:" in cid for cid in plain)
    offered = [b["custom_id"] for row in pab.intake_material_components("aid", photo=True)
               for b in row["components"]]
    assert "pbintake:photo:aid" in offered
    state = {"step": "material", "photo_offered": True}
    stepped = [b["custom_id"] for row in pab.intake_step_components("aid", state)
               for b in row["components"]]
    assert "pbintake:photo:aid" in stepped
    assert pab.parse_intake_custom_id("pbintake:photo:aid") == ("photo", "aid")
    assert pab.parse_intake_custom_id("pbintake:edit:aid") == ("edit", "aid")


def test_photo_confirm_text_shows_every_extracted_field():
    state = {"step": "confirm", "slot": 2, "predicted_id": 18, "brand": "Sunlu",
             "material": "Silk PLA", "color": "Rose Red Blue Green", "grams": 500,
             "from_photo": True, "nozzle_temp": 220, "bed_temp": 60, "disposition": None}
    text = pab.intake_step_text(state)
    assert "Sunlu" in text and "Silk PLA" in text and "Rose Red Blue Green" in text
    # No spool number before creation (#1063), even from the photo path:
    # the confirm text names the extracted fields, never a predicted id.
    assert "500 g" in text and "a new spool" in text and "Spool #18" not in text
    assert "nozzle 220°C" in text and "bed 60°C" in text and "saved on the filament" in text
    labels = [b["label"] for b in pab.intake_step_components("aid", state)[0]["components"]]
    assert "Edit details" in labels and "Create & load" in labels


def test_manual_confirm_has_no_edit_button_or_photo_text():
    state = {"step": "confirm", "slot": 2, "predicted_id": 18, "brand": None,
             "material": "PLA", "color": "Black", "grams": 1000}
    labels = [b["label"] for b in pab.intake_step_components("aid", state)[0]["components"]]
    assert "Edit details" not in labels
    assert "label photo" not in pab.intake_step_text(state)


# ------------------------------------------------------------- label temps are data

def _spool_client(existing_filaments, captured):
    def client(method, path, body=None):
        captured.append((method, path, body))
        if method == "GET" and path == "/api/v1/filament":
            return existing_filaments
        if method == "GET" and path == "/api/v1/vendor":
            return []
        if method == "POST" and path == "/api/v1/filament":
            return {"id": 9}
        if method == "POST" and path == "/api/v1/spool":
            return {"id": 18}
        return {}
    return client


def test_create_spool_records_label_temps_on_a_new_filament():
    captured = []
    made = spool_sync.create_spool(
        {"brand": None, "material": "Silk PLA", "color": "Rose", "grams": 500.0,
         "nozzle_temp": 220.0, "bed_temp": 60.0},
        client=_spool_client([], captured))
    assert made["spool_id"] == 18 and made["reused_filament"] is False
    body = next(b for m, p, b in captured if m == "POST" and p == "/api/v1/filament")
    assert body["settings_extruder_temp"] == 220.0 and body["settings_bed_temp"] == 60.0


def test_create_spool_reused_filament_keeps_its_own_temps():
    captured = []
    existing = [{"id": 4, "name": "Rose", "material": "Silk PLA", "vendor": None,
                 "settings_extruder_temp": 215, "settings_bed_temp": 55}]
    made = spool_sync.create_spool(
        {"brand": None, "material": "Silk PLA", "color": "Rose", "grams": 500.0,
         "nozzle_temp": 220.0, "bed_temp": 60.0},
        client=_spool_client(existing, captured))
    assert made["reused_filament"] is True
    assert not any(m == "POST" and p == "/api/v1/filament" for m, p, _ in captured)


def test_create_and_bind_passes_label_temps_into_the_spec():
    seen = {}

    class Sync:
        @staticmethod
        def parse_new(text):
            return {"material": "PLA", "color": "Black", "grams": 1000}

        @staticmethod
        def create_spool(spec):
            seen.update(spec)
            return {"spool_id": 18}

        @staticmethod
        def bind_slot(slot, spool_id, **kwargs):
            return {"record": {"name": "Black PLA"}, "prior_spool_id": None}

    pab.create_and_bind_insertion(1, {"brand": "", "material": "PLA", "color": "Black",
                                      "grams": "1000", "lot": "",
                                      "nozzle_temp": 215.0, "bed_temp": None}, Sync)
    assert seen["nozzle_temp"] == 215.0 and "bed_temp" not in seen


# ------------------------------------------------- end-to-end photo walkthrough

class _FakeResponse:
    def __init__(self, sink):
        self.sink = sink

    async def defer(self, **kw):
        self.sink.append(("defer", None, None))

    async def send_message(self, content=None, view=None, ephemeral=False, **kw):
        self.sink.append(("send", content, view))

    async def edit_message(self, content=None, view=None, **kw):
        self.sink.append(("edit", content, view))

    async def send_modal(self, modal):
        self.sink.append(("modal", modal))


class _FakeMessage:
    async def edit(self, view=None, **kw):
        return None


class _FakeChannel:
    def __init__(self, sink):
        self.id = 99
        self.sink = sink

    async def send(self, content=None, view=None, **kw):
        self.sink.append(("channel", content, view))
        return S(id="1000")


class _FakeAttachment:
    filename = "label.jpg"
    content_type = "image/jpeg"

    async def read(self):
        return b"label-image-bytes"


def _custom_ids(view):
    return [item.custom_id for item in (view.children if view else [])]


def _photo_harness(monkeypatch, tmp_path, vision_answer):
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    monkeypatch.setattr(plugin, "_photo_intake_ready", lambda: True)
    monkeypatch.setattr(mel, "_vision_ready", lambda *a, **k: True)
    monkeypatch.setattr(plugin, "_spool_label_vision_call",
                        lambda: (lambda image, prompt: vision_answer))
    monkeypatch.setattr(spool_sync, "next_spool_id", lambda: 18)
    monkeypatch.setattr(spool_sync, "filament_types", lambda: [])
    monkeypatch.setattr(spool_sync, "load_state", lambda: {
        "filament_slots": {"2": {"spoolman_id": 13, "name": "Matte White PLA+", "remaining_grams": 0.0}}})
    created, binds = [], []

    def fake_create(spec):
        created.append(spec)
        return {"spool_id": 18}

    def fake_bind(slot, spool_id, **kwargs):
        binds.append((slot, spool_id, kwargs))
        return {"record": {"name": "Rose Red Blue Green Silk PLA"}, "prior_spool_id": 13}

    monkeypatch.setattr(spool_sync, "create_spool", fake_create)
    monkeypatch.setattr(spool_sync, "bind_slot", fake_bind)

    channel_sink = []
    channel = _FakeChannel(channel_sink)
    photo_message = S(author=S(id="42"), channel=channel, attachments=[_FakeAttachment()])

    async def wait_for(event, check=None, timeout=None):
        assert event == "message" and check(photo_message)
        return photo_message

    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn),
            wait_for=wait_for)
    plugin.alert_button_handlers(bot, S())
    return bot.listeners["on_interaction"], channel_sink, created, binds, channel


def _interaction(sink, custom_id, channel, values=None):
    async def _edit_original(content=None, view=None, **kw):
        sink.append(("edit", content, view))

    async def _followup_send(content=None, view=None, **kw):
        sink.append(("send", content, view))

    return S(user=S(id="42", bot=False, display_name="catduck"),
             data={"custom_id": custom_id, "values": values or []},
             response=_FakeResponse(sink), message=_FakeMessage(),
             edit_original_response=_edit_original,
             followup=S(send=_followup_send),
             channel_id="99", channel=channel, guild=None)


def test_photo_walkthrough_prefills_and_creates_only_on_confirm(tmp_path, monkeypatch):
    answer = {"brand": "Sunlu", "material": "Silk PLA", "color": "Rose Red Blue Green",
              "grams": 500, "nozzle_temp": 220, "bed_temp": 60}
    on_interaction, channel_sink, created, binds, channel = _photo_harness(
        monkeypatch, tmp_path, answer)

    async def main():
        sink = []
        insertion_aid = pab.record("insertion", "", slot=2)
        await on_interaction(_interaction(sink, f"pbinsert:new:{insertion_aid}", channel))
        kind, content, view = sink[-1]
        assert "Spool #18" not in content and "actual number" in content  # #1063: never predicted
        photo = next(cid for cid in _custom_ids(view) if ":photo:" in cid)

        sink.clear()
        await on_interaction(_interaction(sink, photo, channel))
        kind, content, view = sink[-1]
        assert "photo of the spool label" in content and view is None
        for _ in range(100):
            if channel_sink:
                break
            await asyncio.sleep(0.05)
        assert channel_sink, "the photo step never posted"
        kind, content, view = channel_sink[-1]
        # The label-read multi-color parse earns the confirm-or-correct step first (#792 part 2).
        assert "Check that color" in content and "Rose Red Blue Green" in content
        color_ok = next(cid for cid in _custom_ids(view) if ":color_ok:" in cid)
        sink.clear()
        await on_interaction(_interaction(sink, color_ok, channel))
        kind, content, view = sink[-1]
        assert "#13" in content  # the replaced spool's disposition is still asked
        depleted = next(cid for cid in _custom_ids(view) if ":disp_depleted:" in cid)

        sink.clear()
        await on_interaction(_interaction(sink, depleted, channel))
        kind, content, view = sink[-1]
        assert "Read from the label photo" in content
        assert "Sunlu" in content and "Rose Red Blue Green" in content and "500 g" in content
        assert "nozzle 220°C" in content and "bed 60°C" in content
        assert created == []  # confirm-before-create: nothing exists yet
        confirm = next(cid for cid in _custom_ids(view) if ":confirm:" in cid)

        sink.clear()
        await on_interaction(_interaction(sink, confirm, channel))
        kind, content, view = sink[-1]
        assert "Created spool #18" in content
        assert created and created[0]["material"] == "Silk PLA"
        assert created[0]["nozzle_temp"] == 220.0 and created[0]["bed_temp"] == 60.0
        assert binds and binds[0][2].get("prior_disposition") == "depleted"

    asyncio.run(main())


def test_photo_failure_falls_back_to_manual_steps(tmp_path, monkeypatch):
    on_interaction, channel_sink, created, binds, channel = _photo_harness(
        monkeypatch, tmp_path, None)  # the vision call could not be completed

    async def main():
        sink = []
        insertion_aid = pab.record("insertion", "", slot=2)
        await on_interaction(_interaction(sink, f"pbinsert:new:{insertion_aid}", channel))
        photo = next(cid for cid in _custom_ids(sink[-1][2]) if ":photo:" in cid)
        await on_interaction(_interaction(sink, photo, channel))
        for _ in range(100):
            if len(channel_sink) >= 2:
                break
            await asyncio.sleep(0.05)
        texts = [c for _, c, _ in channel_sink]
        assert any("couldn't read that label photo" in t for t in texts)
        kind, content, view = channel_sink[-1]
        assert "Material" in content  # the manual walkthrough is re-posted
        assert any(cid.startswith(pab.INTAKE_SELECT_PREFIX) for cid in _custom_ids(view))
        assert created == []

    asyncio.run(main())


def test_photo_button_absent_without_a_vision_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    monkeypatch.setattr(plugin, "_photo_intake_ready", lambda: False)
    monkeypatch.setattr(spool_sync, "next_spool_id", lambda: 18)
    monkeypatch.setattr(spool_sync, "filament_types", lambda: [])
    monkeypatch.setattr(spool_sync, "load_state", lambda: {"filament_slots": {}})
    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn))
    plugin.alert_button_handlers(bot, S())
    sink = []
    insertion_aid = pab.record("insertion", "", slot=2)
    asyncio.run(bot.listeners["on_interaction"](
        _interaction(sink, f"pbinsert:new:{insertion_aid}", _FakeChannel([]))))
    assert not any(":photo:" in cid for cid in _custom_ids(sink[-1][2]))
    assert any(cid.startswith(pab.INTAKE_SELECT_PREFIX) for cid in _custom_ids(sink[-1][2]))


# ------------------------------------------------- label text matching (OCR route)

LABEL_TEXT = ("SUNLU\nSILK PLA\nRose Red Blue Green\nNet Weight: 500g\n"
              "Nozzle Temp: 215-230C\nBed Temp: 60C\nDiameter: 1.75mm")


def test_text_clean_label():
    spec = spool_label.spec_from_label_text(LABEL_TEXT)
    assert spec == {"brand": "Sunlu", "material": "Silk PLA", "color": "Rose Red Blue Green",
                    "grams": 500.0, "nozzle_temp": 222.5, "bed_temp": 60.0}


def test_text_noisy_ocr_still_matches_vocabulary():
    spec = spool_label.spec_from_label_text("PETG\nTeal\n1 kg spool\nPrint Temperature: 240C\nBed: 85C")
    assert spec["material"] == "PETG" and spec["color"] == "Teal" and spec["grams"] == 1000.0
    assert spec["nozzle_temp"] == 240.0 and spec["bed_temp"] == 85.0


def test_text_known_type_is_a_wholesale_match():
    types = [{"brand": "Sunlu", "material": "Silk PLA", "color": "Rose Red Blue Green", "hex": None}]
    spec = spool_label.spec_from_label_text("Silk PLA Rose Red Blue Green 500g", types)
    assert spec["brand"] == "Sunlu" and spec["color"] == "Rose Red Blue Green"


def test_label_temps_follow_the_downstream_contract():
    # P2-199: a temperature recorded on a filament is honoured downstream
    # only when 0 < t <= 300 (printer_state.resolve_material_temperatures
    # raises otherwise), so label validation must drop what the contract
    # would reject instead of persisting it.
    spec = spool_label.validate_label_answer({
        "material": "PLA", "color": "Black", "grams": 1000,
        "nozzle_temp": 320, "bed_temp": 0})
    assert spec["nozzle_temp"] is None and spec["bed_temp"] is None
    spec = spool_label.validate_label_answer({
        "material": "PC", "color": "Black", "grams": 1000,
        "nozzle_temp": 300, "bed_temp": 110})
    assert spec["nozzle_temp"] == 300.0 and spec["bed_temp"] == 110.0
    # The OCR/text route shares the contract.
    spec = spool_label.spec_from_label_text("PLA Black 1 kg Nozzle: 320C Bed: 0C")
    assert spec["nozzle_temp"] is None and spec["bed_temp"] is None


def test_label_temps_keep_a_plausible_nozzle_floor():
    # 2.2.5 review: the 300 ceiling must not drop the nozzle floor; a misread
    # 20 or 55 is not a nozzle temperature, while a low real bed is kept.
    for nozzle in (20, 55):
        spec = spool_label.validate_label_answer({
            "material": "PLA", "color": "Black", "grams": 1000,
            "nozzle_temp": nozzle, "bed_temp": 55})
        assert spec["nozzle_temp"] is None and spec["bed_temp"] == 55.0
    spec = spool_label.spec_from_label_text("PLA Black 1 kg Nozzle: 20C Bed: 55C")
    assert spec["nozzle_temp"] is None and spec["bed_temp"] == 55.0
    spec = spool_label.validate_label_answer({
        "material": "PLA", "color": "Black", "grams": 1000, "nozzle_temp": 80, "bed_temp": 200})
    assert spec["nozzle_temp"] == 80.0 and spec["bed_temp"] is None


def test_text_material_matching_respects_token_boundaries():
    # P2-202: 'pla' inside 'plate' is not the material PLA; the complete
    # token PC printed on the label is the material.
    spec = spool_label.spec_from_label_text("PC Black 1 kg\nBuild plate: 100C")
    assert spec["material"] == "PC"
    assert spec["bed_temp"] == 100.0


def test_text_known_type_match_checks_the_printed_brand():
    # P2-203: with two known PLA Black types, the brand printed on the
    # label decides — filament_types() order must not pick eSun for a
    # Sunlu label (the created spool would reuse eSun's filament record).
    types = [{"brand": "eSun", "material": "PLA", "color": "Black"},
             {"brand": "Sunlu", "material": "PLA", "color": "Black"}]
    spec = spool_label.spec_from_label_text("Sunlu PLA Black 1kg", types)
    assert spec["brand"] == "Sunlu" and spec["material"] == "PLA"
    # A label naming a brand no known type carries must not inherit a
    # conflicting brand from a material+colour match alone.
    spec = spool_label.spec_from_label_text("Polymaker PLA Black 1kg", types)
    assert spec["brand"] == "Polymaker" and spec["material"] == "PLA"


def test_text_partial_label_names_what_is_missing():
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.spec_from_label_text("PLA Black")
    assert caught.value.reason == "missing_fields" and "weight" in str(caught.value)


@pytest.mark.parametrize("text", ["", "   ", "asdf qwer zxcv"])
def test_text_unreadable_when_nothing_matches(text):
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.spec_from_label_text(text)
    assert caught.value.reason == "unreadable"


def test_ocr_route_with_injected_text():
    spec = spool_label.extract_via_ocr(b"img", text_fn=lambda image: LABEL_TEXT)
    assert spec["material"] == "Silk PLA" and spec["grams"] == 500.0


def test_ocr_route_empty_text_and_missing_binary():
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_via_ocr(b"img", text_fn=lambda image: "")
    assert caught.value.reason == "unreadable"


@pytest.mark.skipif(not spool_label.tesseract_available(), reason="no tesseract binary")
def test_ocr_route_real_tesseract_on_a_rendered_label(tmp_path):
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if not font_path.exists():
        pytest.skip("no TrueType font to render a label with")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 480), "white")
    draw = ImageDraw.Draw(img)
    bold = ImageFont.truetype(str(font_path), 44)
    plain = ImageFont.truetype(str(font_path), 36)
    draw.text((40, 30), "SUNLU", fill="black", font=bold)
    draw.text((40, 100), "SILK PLA", fill="black", font=bold)
    draw.text((40, 170), "Rose Red Blue Green", fill="black", font=plain)
    draw.text((40, 240), "Net Weight: 500g", fill="black", font=plain)
    draw.text((40, 300), "Nozzle Temp: 215-230C", fill="black", font=plain)
    draw.text((40, 360), "Bed Temp: 60C", fill="black", font=plain)
    import io as _io
    buffer = _io.BytesIO()
    img.save(buffer, format="PNG")
    spec = spool_label.extract_via_ocr(buffer.getvalue())
    assert spec["material"] == "Silk PLA" and spec["color"] == "Rose Red Blue Green"
    assert spec["grams"] == 500.0 and spec["bed_temp"] == 60.0


# ------------------------------------------------------------- barcode / QR route

def test_codes_json_payload_uses_the_shared_validation():
    spec = spool_codes.extract_via_codes(
        b"img", reader=lambda image: ['{"brand": "Sunlu", "material": "PLA", "color": "Black", "grams": 1000}'])
    assert spec["material"] == "PLA" and spec["grams"] == 1000.0


def test_codes_text_payload_uses_label_text_matching():
    spec = spool_codes.extract_via_codes(
        b"img", reader=lambda image: ["Sunlu Silk PLA Rose Red Blue Green 500g"])
    assert spec["color"] == "Rose Red Blue Green" and spec["grams"] == 500.0


def test_codes_bare_product_number_carries_no_spec():
    with pytest.raises(spool_label.SpoolLabelError):
        spool_codes.extract_via_codes(b"img", reader=lambda image: ["6971234567890"])


def test_codes_no_code_in_photo_is_unreadable():
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_codes.extract_via_codes(b"img", reader=lambda image: [])
    assert caught.value.reason == "unreadable"


@pytest.mark.skipif(not spool_codes.codes_available(), reason="zxing-cpp not installed")
def test_codes_real_qr_roundtrip():
    import io as _io
    import numpy as np
    import zxingcpp
    from PIL import Image
    payload = json.dumps({"brand": "Sunlu", "material": "Silk PLA",
                          "color": "Rose Red Blue Green", "grams": 500,
                          "nozzle_temp": 220, "bed_temp": 60})
    qr = Image.fromarray(np.asarray(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode), scale=8),
        dtype=np.uint8), mode="L")
    buffer = _io.BytesIO()
    qr.save(buffer, format="PNG")
    spec = spool_codes.extract_via_codes(buffer.getvalue())
    assert spec["material"] == "Silk PLA" and spec["nozzle_temp"] == 220.0
    blank = Image.new("RGB", (100, 100), "white")
    buffer = _io.BytesIO()
    blank.save(buffer, format="PNG")
    assert spool_codes.decode_payloads(buffer.getvalue()) == []


# ------------------------------------------------------------- route chain + gate

def _fail(reason, missing=()):
    def route(image):
        raise spool_label.SpoolLabelError(reason, missing)
    return route


def _ok(image):
    return {"brand": None, "material": "PLA", "color": "Black", "grams": 1000.0,
            "nozzle_temp": None, "bed_temp": None}


def test_chain_first_success_wins_and_names_the_route():
    spec, route = spool_label.extract_spool_spec_any(
        b"x", [("code", _fail("unreadable")), ("vision", _ok), ("ocr", _fail("unreadable"))])
    assert route == "vision" and spec["material"] == "PLA"


def test_chain_all_fail_prefers_the_actionable_missing_fields():
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec_any(
            b"x", [("code", _fail("unreadable")), ("ocr", _fail("missing_fields", ("weight",)))])
    assert caught.value.reason == "missing_fields"


def test_chain_no_routes_is_unreadable():
    with pytest.raises(spool_label.SpoolLabelError) as caught:
        spool_label.extract_spool_spec_any(b"x", [])
    assert caught.value.reason == "unreadable"


def test_gate_opens_on_any_route(monkeypatch):
    monkeypatch.setattr(mel, "_vision_ready", lambda *a, **k: False)
    monkeypatch.setattr(spool_codes, "codes_available", lambda: True)
    monkeypatch.setattr(spool_label, "tesseract_available", lambda: False)
    assert plugin._photo_intake_ready() is True
    monkeypatch.setattr(spool_codes, "codes_available", lambda: False)
    monkeypatch.setattr(spool_label, "tesseract_available", lambda: True)
    assert plugin._photo_intake_ready() is True
    monkeypatch.setattr(spool_label, "tesseract_available", lambda: False)
    assert plugin._photo_intake_ready() is False


def test_confirm_text_names_the_reading_route():
    base = {"step": "confirm", "slot": 2, "predicted_id": 18, "brand": "Sunlu",
            "material": "Silk PLA", "color": "Rose Red Blue Green", "grams": 500,
            "from_photo": True, "nozzle_temp": None, "bed_temp": None, "disposition": None}
    assert "barcode/QR code" in pab.intake_step_text({**base, "photo_route": "code"})
    assert "local OCR" in pab.intake_step_text({**base, "photo_route": "ocr"})
    assert "vision endpoint" in pab.intake_step_text({**base, "photo_route": "vision"})


# ------------------------------------------- end-to-end: the code route reads it

class _QrAttachment:
    filename = "label.png"
    content_type = "image/png"

    def __init__(self, data):
        self._data = data

    async def read(self):
        return self._data


@pytest.mark.skipif(not spool_codes.codes_available(), reason="zxing-cpp not installed")
def test_photo_walkthrough_code_route_prefills(tmp_path, monkeypatch):
    import io as _io
    import numpy as np
    import zxingcpp
    from PIL import Image
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(plugin, "_prompt_brand", lambda: False)
    monkeypatch.setattr(plugin, "_photo_intake_ready", lambda: True)
    monkeypatch.setattr(mel, "_vision_ready", lambda *a, **k: False)
    monkeypatch.setattr(spool_sync, "next_spool_id", lambda: 18)
    monkeypatch.setattr(spool_sync, "filament_types", lambda: [])
    monkeypatch.setattr(spool_sync, "load_state", lambda: {
        "filament_slots": {"2": {"spoolman_id": 13, "name": "Matte White PLA+", "remaining_grams": 0.0}}})
    created = []
    monkeypatch.setattr(spool_sync, "create_spool",
                        lambda spec: created.append(spec) or {"spool_id": 18})
    monkeypatch.setattr(spool_sync, "bind_slot",
                        lambda slot, spool_id, **kw: {"record": {"name": "Rose Silk PLA"},
                                                      "prior_spool_id": 13})
    payload = json.dumps({"brand": "Sunlu", "material": "Silk PLA",
                          "color": "Rose Red Blue Green", "grams": 500,
                          "nozzle_temp": 220, "bed_temp": 60})
    qr = Image.fromarray(np.asarray(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.QRCode), scale=8),
        dtype=np.uint8), mode="L")
    buffer = _io.BytesIO()
    qr.save(buffer, format="PNG")

    channel_sink = []
    channel = _FakeChannel(channel_sink)
    photo_message = S(author=S(id="42"), channel=channel,
                      attachments=[_QrAttachment(buffer.getvalue())])

    async def wait_for(event, check=None, timeout=None):
        assert check(photo_message)
        return photo_message

    bot = S(listeners={}, add_listener=lambda fn, name: bot.listeners.__setitem__(name, fn),
            wait_for=wait_for)
    plugin.alert_button_handlers(bot, S())
    on_interaction = bot.listeners["on_interaction"]

    async def main():
        sink = []
        insertion_aid = pab.record("insertion", "", slot=2)
        await on_interaction(_interaction(sink, f"pbinsert:new:{insertion_aid}", channel))
        photo = next(cid for cid in _custom_ids(sink[-1][2]) if ":photo:" in cid)
        await on_interaction(_interaction(sink, photo, channel))
        for _ in range(100):
            if channel_sink:
                break
            await asyncio.sleep(0.05)
        assert channel_sink, "the code route never posted"
        kind, content, view = channel_sink[-1]
        assert "Check that color" in content  # the code-read multi-color parse is confirmed first
        color_ok = next(cid for cid in _custom_ids(view) if ":color_ok:" in cid)
        sink.clear()
        await on_interaction(_interaction(sink, color_ok, channel))
        kind, content, view = sink[-1]
        depleted = next(cid for cid in _custom_ids(view) if ":disp_depleted:" in cid)
        sink.clear()
        await on_interaction(_interaction(sink, depleted, channel))
        kind, content, view = sink[-1]
        assert "barcode/QR code" in content and "Sunlu" in content
        assert created == []
        confirm = next(cid for cid in _custom_ids(view) if ":confirm:" in cid)
        await on_interaction(_interaction(sink, confirm, channel))
        assert created and created[0]["nozzle_temp"] == 220.0

    asyncio.run(main())
