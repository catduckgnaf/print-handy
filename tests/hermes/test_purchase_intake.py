"""Purchase spool intake (#801): a shop/order link or a purchase screenshot
proposes spools, confirm-first, when an AI route is configured.

Recorded fixtures only: every page is a string in this file, every AI
answer is a stub, and the Spoolman side is a fake client behind
spool_sync.create_spool with a write log, so cancel/refusal tests can
prove zero writes happened. No live network anywhere.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.runtime import purchase_intake as pi
from print_handy.runtime import spool_label, spool_sync

SPEC = importlib.util.spec_from_file_location(
    "pf_purchase_801", Path(__file__).parents[2] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


# ------------------------------------------------------------------ fixtures

JSONLD_PAGE = """<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product",
 "name": "Sunlu PLA+ Filament Black 1kg",
 "brand": {"@type": "Brand", "name": "Sunlu"},
 "offers": {"@type": "Offer", "price": "19.99", "priceCurrency": "USD"}}
</script></head><body><h1>Sunlu PLA+ Filament Black 1kg</h1></body></html>"""

OG_ONLY_PAGE = """<html><head>
<meta property="og:title" content="Premium Filament Store — Deals">
<meta property="og:type" content="product">
</head><body><p>Our best filament deals this week. Free shipping over $50.</p></body></html>"""

OG_COMPLETE_PAGE = """<html><head>
<meta property="og:title" content="eSun PETG Filament Teal 1kg">
<meta property="product:price:amount" content="21.50">
</head><body></body></html>"""

BLOCKED_PAGE = "<html><body><p>Enter the characters you see below to continue (captcha).</p></body></html>"

ORDER_ANSWER = {"items": [
    {"brand": "Sunlu", "material": "PLA+", "color": "Black", "grams": 1000,
     "quantity": 2, "price": 19.99, "nozzle_temp": 215, "bed_temp": 60},
    {"brand": "Sunlu", "material": "Silk PLA", "color": "Gold", "grams": 1000,
     "quantity": 1, "price": 24.99},
]}


def _fetcher(status, page, final=None):
    return lambda url: (status, final or url, page)


def _ai(answer, calls=None):
    def call(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return answer
    return call


def _ai_boom(**kwargs):
    raise AssertionError("the AI route must not be called")


class FakeSpoolman:
    """The spoolman callable shape with a write log; created filaments join
    the list, so a second roll of a line reuses the first roll's filament."""

    def __init__(self):
        self.writes = []
        self.filaments = []
        self.vendors = [{"id": 1, "name": "Sunlu"}]
        self.next_filament = 10
        self.next_spool = 20

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/filament":
            return [dict(f) for f in self.filaments]
        if method == "GET" and path == "/api/v1/vendor":
            return [dict(v) for v in self.vendors]
        self.writes.append((method, path, body))
        if method == "POST" and path == "/api/v1/vendor":
            vendor = {"id": len(self.vendors) + 1, "name": body["name"]}
            self.vendors.append(vendor)
            return {"id": vendor["id"]}
        if method == "POST" and path == "/api/v1/filament":
            filament = {"id": self.next_filament, "name": body["name"],
                        "material": body["material"],
                        "vendor": next((v for v in self.vendors if v["id"] == body.get("vendor_id")), None)}
            self.next_filament += 1
            self.filaments.append(filament)
            return {"id": filament["id"]}
        if method == "POST" and path == "/api/v1/spool":
            spool = {"id": self.next_spool}
            self.next_spool += 1
            return spool
        return {}


def _create(fake):
    real = spool_sync.create_spool  # captured now: dispatch tests patch the module attribute
    return lambda spec: real(spec, client=fake)


# ------------------------------------------------------- screenshot acceptance

def test_screenshot_order_proposes_three_spools_on_two_filament_types():
    calls = []
    lines = pi.propose_from_image(b"screenshot-bytes", ai_call=_ai(ORDER_ANSWER, calls))
    assert sum(line["quantity"] for line in lines) == 3
    assert {(line["brand"], line["material"], line["color"]) for line in lines} == {
        ("Sunlu", "PLA+", "Black"), ("Sunlu", "Silk PLA", "Gold")}
    # The purchase prompt, not the #514 label prompt, and the image went with it.
    assert calls[0]["prompt"] == pi.PURCHASE_PROMPT
    assert calls[0]["prompt"] != spool_label.LABEL_PROMPT
    assert calls[0]["image"] == b"screenshot-bytes"
    text = pi.confirm_text(lines)
    assert "Black · PLA+ · Sunlu · 1000 g × 2" in text
    assert "Gold · Silk PLA · Sunlu · 1000 g × 1" in text


def test_create_all_creates_exactly_the_proposed_spools_on_the_shelf():
    fake = FakeSpoolman()
    lines = pi.propose_from_image(b"screenshot-bytes", ai_call=_ai(ORDER_ANSWER))
    created = pi.create_purchase(lines, create_spool=_create(fake))
    assert [entry["spool_id"] for entry in created] == [20, 21, 22]
    spool_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/spool"]
    filament_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/filament"]
    assert len(spool_posts) == 3 and len(filament_posts) == 2  # 3 spools, 2 filament types
    assert all(body["location"] == "Shelf" for body in spool_posts)
    assert [body["price"] for body in spool_posts] == [19.99, 19.99, 24.99]
    # The PLA+ line's label temperatures ride the new filament (the existing
    # create_spool rule); the silk line's filament has none recorded.
    assert filament_posts[0]["settings_extruder_temp"] == 215.0
    assert "settings_extruder_temp" not in filament_posts[1]


def test_unreadable_screenshot_refuses_with_the_manual_walkthrough():
    with pytest.raises(pi.PurchaseError) as caught:
        pi.propose_from_image(b"blur", ai_call=_ai(None))
    assert caught.value.reason == "unreadable"
    assert "/spool new" in str(caught.value)
    with pytest.raises(pi.PurchaseError) as caught:
        pi.propose_from_image(b"blur", ai_call=_ai({"items": []}))
    assert caught.value.reason == "unreadable"


# ----------------------------------------------------------------- link routes

def test_jsonld_complete_data_resolves_without_any_ai_call():
    lines = pi.propose_from_link("https://shop.example/products/sunlu-pla-plus-black",
                                 fetcher=_fetcher(200, JSONLD_PAGE), ai_call=_ai_boom)
    assert len(lines) == 1
    line = lines[0]
    assert (line["brand"], line["material"], line["color"], line["grams"]) == \
        ("Sunlu", "PLA+", "Black", 1000.0)
    assert line["price"] == 19.99
    assert line["purchase_url"] == "https://shop.example/products/sunlu-pla-plus-black"


def test_link_create_records_price_and_purchase_url_on_the_spool():
    fake = FakeSpoolman()
    lines = pi.propose_from_link("https://shop.example/products/sunlu-pla-plus-black",
                                 fetcher=_fetcher(200, JSONLD_PAGE), ai_call=_ai_boom)
    pi.create_purchase(lines, create_spool=_create(fake))
    body = next(b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/spool")
    assert body["price"] == 19.99
    assert body["comment"] == (spool_sync.CREATED_SPOOL_COMMENT
                               + "; purchased: https://shop.example/products/sunlu-pla-plus-black")


def test_og_only_incomplete_data_falls_back_to_the_ai_call():
    calls = []
    lines = pi.propose_from_link("https://shop.example/deals",
                                 fetcher=_fetcher(200, OG_ONLY_PAGE),
                                 ai_call=_ai(ORDER_ANSWER, calls))
    assert len(calls) == 1 and calls[0]["prompt"] == pi.PURCHASE_PROMPT
    assert "filament deals" in calls[0]["text"]  # the trimmed page text went along
    assert sum(line["quantity"] for line in lines) == 3
    assert all(line["purchase_url"] == "https://shop.example/deals" for line in lines)


def test_og_complete_title_resolves_without_any_ai_call():
    lines = pi.propose_from_link("https://shop.example/p/esun-petg-teal",
                                 fetcher=_fetcher(200, OG_COMPLETE_PAGE), ai_call=_ai_boom)
    assert [(line["material"], line["color"], line["grams"], line["price"]) for line in lines] == \
        [("PETG", "Teal", 1000.0, 21.50)]


def test_blocked_page_refuses_plainly_and_asks_for_a_screenshot():
    with pytest.raises(pi.PurchaseError) as caught:
        pi.propose_from_link("https://www.amazon.example/dp/B0EXAMPLE",
                             fetcher=_fetcher(403, BLOCKED_PAGE), ai_call=_ai_boom)
    assert caught.value.reason == "blocked"
    assert "screenshot" in str(caught.value)


def test_login_wall_counts_as_blocked():
    with pytest.raises(pi.PurchaseError) as caught:
        pi.propose_from_link("https://shop.example/orders/123",
                             fetcher=_fetcher(200, "<html><body>Welcome</body></html>",
                                              final="https://shop.example/ap/signin?next=orders"),
                             ai_call=_ai_boom)
    assert caught.value.reason == "blocked"


def test_fetch_failure_asks_for_a_screenshot():
    def boom(url):
        raise OSError("unreachable")

    with pytest.raises(pi.PurchaseError) as caught:
        pi.propose_from_link("https://shop.example/p/1", fetcher=boom, ai_call=_ai_boom)
    assert caught.value.reason == "fetch_failed"
    assert "screenshot" in str(caught.value)


# ------------------------------------------------------------- pack variants

def test_pack_variant_splits_into_one_line_per_color():
    lines = pi.validate_purchase_answer({"items": [
        {"brand": "eSun", "material": "PLA", "color": "Black/White/Gray/Red",
         "grams": 1000, "quantity": 4, "price": 15.0}]})
    assert [(line["color"], line["quantity"]) for line in lines] == [
        ("Black", 1), ("White", 1), ("Gray", 1), ("Red", 1)]
    assert all(line["price"] == 15.0 for line in lines)  # per-roll price carries over


def test_pack_variant_quantity_distributes_and_total_price_divides():
    # The structured-data flag: a pack's total price divides per roll, and
    # an uneven quantity gives the first colors the remainder.
    line = {"brand": "eSun", "material": "PLA", "color": "Black/White", "hex": None,
            "grams": 1000.0, "quantity": 3, "price": 30.0, "price_is_total": True,
            "nozzle_temp": None, "bed_temp": None, "purchase_url": None}
    lines = pi._split_pack(dict(line))
    assert [(entry["color"], entry["quantity"], entry["price"]) for entry in lines] == [
        ("Black", 2, 10.0), ("White", 1, 10.0)]


# ---------------------------------------------------------------- validation

def test_unknown_material_is_refused_and_creates_nothing():
    fake = FakeSpoolman()
    with pytest.raises(pi.PurchaseError) as caught:
        pi.validate_purchase_answer({"items": [
            {"brand": "Acme", "material": "Unobtainium", "color": "Black",
             "grams": 1000, "quantity": 1}]})
    assert caught.value.reason == "unknown_material"
    assert "Unobtainium" in str(caught.value)
    assert fake.writes == []


def test_missing_fields_are_named():
    with pytest.raises(pi.PurchaseError) as caught:
        pi.validate_purchase_answer({"items": [
            {"material": "PLA", "color": "Black", "quantity": 1}]})
    assert caught.value.reason == "missing_fields"
    assert "weight" in str(caught.value)


def test_pla_plus_uses_the_existing_variant_rule_not_a_duplicate():
    # PLA+ resolves as the PLA family through printer_state, whose variant
    # adjustment is the +5 C nozzle delta purchase intake must not reimplement.
    from print_handy.core import printer_state
    family, targets, _sources = printer_state.resolve_material_temperatures("PLA+", {})
    plain, plain_targets, _ = printer_state.resolve_material_temperatures("PLA", {})
    assert family == plain == "PLA"
    assert targets["nozzle"] == plain_targets["nozzle"] + 5
    lines = pi.validate_purchase_answer({"items": [
        {"material": "PLA+", "color": "Black", "grams": 1000, "quantity": 1}]})
    assert lines[0]["material"] == "PLA+"  # the label itself is kept as bought


def test_purchase_comment_still_reads_as_print_handy_made():
    from print_handy.runtime import moonraker_event_listener as mel
    comment = spool_sync.CREATED_SPOOL_COMMENT + "; purchased: https://shop.example/p/1"
    assert mel.created_by_print_handy({"comment": comment}) is True
    assert mel.created_by_print_handy({"comment": "bought at the fair"}) is False


# ------------------------------------------------------------- gate + routing

def test_ai_route_gate_follows_the_vision_configuration(monkeypatch):
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(pi._config, "service_url", lambda name: "")
    assert pi.ai_route_configured() is False
    monkeypatch.setattr(pi._config, "service_url", lambda name: "http://vision.test/v1")
    assert pi.ai_route_configured() is True
    monkeypatch.setattr(pi._config, "service_url", lambda name: "")
    monkeypatch.setenv("PRINT_VISION_URL", "http://vision.test/extract")
    assert pi.ai_route_configured() is True


def test_model_link_is_never_a_purchase_offer():
    assert pi.purchase_url("https://www.printables.com/model/123456-thing") is None
    assert pi.purchase_url("https://makerworld.com/en/models/123-thing") is None
    assert pi.looks_like_purchase("https://www.printables.com/model/123456-thing",
                                  has_image=False) is False


def test_recognition_needs_a_shop_signal_or_filament_words():
    assert pi.purchase_url("https://example.com/random/page") is None
    assert pi.purchase_url("ordered filament: https://shop.example/cart") == "https://shop.example/cart"
    assert pi.looks_like_purchase("screenshot of my order", has_image=True) is True
    assert pi.looks_like_purchase("", has_image=True) is False
    assert pi.looks_like_purchase("nice photo", has_image=True) is False
    assert pi.looks_like_purchase("nice photo", has_image=False) is False


# ------------------------------------------------------------ dispatch wiring

class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.clarifies = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kw):
        self.clarifies.append(kw)
        return S(success=True)


def _event(text, media_urls=None):
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
            profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=src, text=text, internal=False, media_urls=media_urls or [])


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "live-session")


def _dispatch(text, answers, monkeypatch, fake=None, media_urls=None, route=True):
    adapter = FakeAdapter()
    fake = fake or FakeSpoolman()
    queue = list(answers)
    from print_handy.core import prompts as cm

    def wait(cid, timeout=None):
        return queue.pop(0) if queue else None

    monkeypatch.setattr(cm, "wait_for_response", wait)
    if route:
        monkeypatch.setenv("PRINT_VISION_URL", "http://vision.test/extract")
    else:
        monkeypatch.delenv("PRINT_VISION_URL", raising=False)
        monkeypatch.setattr(pi._config, "service_url", lambda name: "")
    monkeypatch.setattr(pi, "fetch_page", _fetcher(200, JSONLD_PAGE))
    monkeypatch.setattr(plugin, "_purchase_ai_call", lambda: _ai(ORDER_ANSWER))
    monkeypatch.setattr(spool_sync, "create_spool", _create(fake))
    out = asyncio.run(plugin.purchase_dispatch(_event(text, media_urls), _gateway(adapter)))
    return out, adapter, fake


SHOP_TEXT = "bought filament again: https://shop.example/products/sunlu-pla-plus-black"


def test_dispatch_no_ai_route_means_no_offer_and_no_change(monkeypatch):
    out, adapter, fake = _dispatch(SHOP_TEXT, [], monkeypatch, route=False)
    assert out is None
    assert adapter.clarifies == [] and adapter.sent == []
    assert fake.writes == []


def test_dispatch_model_link_is_not_claimed(monkeypatch):
    out, adapter, fake = _dispatch("https://www.printables.com/model/123456-thing", [], monkeypatch)
    assert out is None
    assert adapter.clarifies == [] and fake.writes == []


def test_dispatch_offer_declined_creates_nothing(monkeypatch):
    out, adapter, fake = _dispatch(SHOP_TEXT, [pi.OFFER_NO], monkeypatch)
    assert out["reason"] == "purchase intake handled"
    assert "nothing was added" in adapter.sent[-1]
    assert fake.writes == []


def test_dispatch_yes_then_create_all_creates_the_proposal(monkeypatch):
    out, adapter, fake = _dispatch(SHOP_TEXT, [pi.OFFER_YES, "Create all"], monkeypatch)
    assert out["reason"] == "purchase intake handled"
    questions = [c["question"] for c in adapter.clarifies]
    assert questions[0] == pi.OFFER_QUESTION
    assert "Black · PLA+ · Sunlu · 1000 g × 1" in questions[1]
    assert adapter.clarifies[1]["choices"] == ["Create all", "Edit", "Cancel"]
    spool_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/spool"]
    assert len(spool_posts) == 1 and spool_posts[0]["location"] == "Shelf"
    assert "Created spool(s) #20 on the shelf." in adapter.sent[-1]


def test_dispatch_yes_then_cancel_creates_nothing(monkeypatch):
    out, adapter, fake = _dispatch(SHOP_TEXT, [pi.OFFER_YES, "Cancel"], monkeypatch)
    assert "Nothing was created" in adapter.sent[-1]
    assert fake.writes == []


def test_dispatch_edit_hands_off_to_the_normal_new_spool_form(monkeypatch):
    out, adapter, fake = _dispatch(SHOP_TEXT, [pi.OFFER_YES, "Edit"], monkeypatch)
    assert "Nothing was created" in adapter.sent[-1]
    assert "/spool new Sunlu, PLA+, Black, 1000" in adapter.sent[-1]
    assert fake.writes == []


def test_dispatch_screenshot_uses_the_vision_route(monkeypatch, tmp_path):
    shot = tmp_path / "order.png"
    shot.write_bytes(b"screenshot-bytes")
    out, adapter, fake = _dispatch("screenshot of my filament order",
                                   [pi.OFFER_YES, "Create all"], monkeypatch,
                                   media_urls=[str(shot)])
    questions = [c["question"] for c in adapter.clarifies]
    assert "Black · PLA+ · Sunlu · 1000 g × 2" in questions[1]
    spool_posts = [b for m, p, b in fake.writes if m == "POST" and p == "/api/v1/spool"]
    assert len(spool_posts) == 3
    assert "Created spool(s) #20, #21, #22 on the shelf." in adapter.sent[-1]


# ------------------------------------------------------------- Codex P2 fixes on PR #803

JSONLD_PACK_PAGE = """<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "Product",
 "name": "Sunlu PLA Filament Black 4 x 1 kg",
 "brand": {"@type": "Brand", "name": "Sunlu"},
 "offers": {"@type": "Offer", "price": "60", "priceCurrency": "USD"}}
</script></head><body><h1>Sunlu PLA Filament Black 4 x 1 kg</h1></body></html>"""


def test_jsonld_pack_count_comes_from_the_product_name_and_the_price_divides():
    # No explicit quantity property: "4 x 1 kg" is the pack evidence, the
    # offer price is the pack total, and no AI call is needed.
    lines = pi.propose_from_link("https://shop.example/products/sunlu-pla-black-4pack",
                                 fetcher=_fetcher(200, JSONLD_PACK_PAGE), ai_call=_ai_boom)
    assert len(lines) == 1
    assert lines[0]["quantity"] == 4
    assert lines[0]["grams"] == 1000.0
    assert lines[0]["price"] == 15.0


def test_one_multicolor_roll_is_not_split_into_separate_rolls():
    lines = pi.validate_purchase_answer({"items": [
        {"brand": "Acme", "material": "PLA", "color": "Red/Black",
         "grams": 1000, "quantity": 1, "price": 22.0}]})
    assert len(lines) == 1
    assert lines[0]["color"] == "Red/Black"
    assert lines[0]["quantity"] == 1
    assert lines[0]["price"] == 22.0


def test_multicolor_flag_keeps_a_listed_blend_as_one_roll():
    lines = pi.validate_purchase_answer({"items": [
        {"brand": "Acme", "material": "PLA", "color": "Red/Black",
         "colors": ["Red", "Black"], "multicolor": True,
         "grams": 1000, "quantity": 2, "price": 22.0}]})
    assert len(lines) == 1
    assert lines[0]["quantity"] == 2


def test_partial_creation_names_the_spools_that_did_land():
    calls = {"n": 0}

    def flaky(spec):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("spoolman down")
        return {"spool_id": 41, "filament_id": 7, "reused_filament": False, "hex": None}

    lines = [{"brand": "Sunlu", "material": "PLA", "color": "Black", "grams": 1000.0,
              "quantity": 3, "price": None, "purchase_url": None, "hex": None,
              "nozzle_temp": None, "bed_temp": None}]
    with pytest.raises(pi.PartialCreationError) as caught:
        pi.create_purchase(lines, create_spool=flaky)
    assert [entry["spool_id"] for entry in caught.value.created] == [41]
    assert "#41" in str(caught.value)
    assert "nothing was created" not in str(caught.value)


def test_creation_failure_before_any_spool_reraises_the_original_error():
    def down(spec):
        raise OSError("spoolman down")

    lines = [{"brand": "Sunlu", "material": "PLA", "color": "Black", "grams": 1000.0,
              "quantity": 1, "price": None, "purchase_url": None, "hex": None,
              "nozzle_temp": None, "bed_temp": None}]
    with pytest.raises(OSError):
        pi.create_purchase(lines, create_spool=down)
