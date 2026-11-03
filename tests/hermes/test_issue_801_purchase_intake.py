"""Purchase intake: add spools from a shop link or an order screenshot (#801).

Extends the #514 photo label intake and the #512 new-spool walkthrough. These
tests pin the pure module (structured page reading, answer validation, pack
expansion, the bounded fetch and its blocked-page error), the availability
gate (no AI route, no offer), the routing rule (a model link stays a model
link), and the whole channel flow with a stateful fake Spoolman. Everything
runs on recorded fixtures (tests/fixtures/purchase_intake): no live network,
Discord, Spoolman or vision endpoint is used anywhere here.
"""
import asyncio
import importlib.util
import io
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import purchase_intake as pi
from print_handy.hermes import spool_label, spool_sync, visual_watch as vw

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures" / "purchase_intake"
SPEC = importlib.util.spec_from_file_location(
    "pf_issue_801", HERE.parents[1] / "plugins/print-feedback/__init__.py")
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


ORDER_ANSWER = json.loads(_fixture("order_screenshot_answer.json"))


# ------------------------------------------------------------- structured page reading

def test_jsonld_product_page_is_complete_without_the_ai_call():
    items = pi.extract_from_page(_fixture("product_jsonld.html"), "https://shop.invalid/p/1")
    assert items == [{"brand": "SUNLU", "material": "PLA+", "color": "Black", "hex": None,
                      "grams": 1000.0, "quantity": 1, "price": 17.99,
                      "nozzle_temp": None, "bed_temp": None}]


def test_read_link_uses_page_data_and_never_calls_the_ai():
    ai = mock.Mock(side_effect=AssertionError("structured data is complete: no AI call"))
    read = pi.read_link("https://shop.invalid/p/1", ai,
                        fetcher=lambda url: (url, _fixture("product_jsonld.html")))
    assert read.source == "page data" and read.url == "https://shop.invalid/p/1"
    assert [pi.line_text(i) for i in read.items] == ["Black · PLA+ · SUNLU · 1 kg × 1"]
    ai.assert_not_called()


def test_incomplete_page_goes_to_the_ai_with_trimmed_text():
    html = _fixture("product_incomplete.html")
    assert pi.extract_from_page(html) is None  # no weight in the name, no single color
    seen = {}

    def ai(image, prompt):
        seen["image"], seen["prompt"] = image, prompt
        return {"items": [{"brand": "Overture", "material": "PLA", "color": "Rock Black",
                           "weight_g": 1000, "quantity": 1, "price": 19.99}]}

    read = pi.read_link("https://shop.invalid/p/2", ai, fetcher=lambda url: (url, html))
    assert read.source == "page text" and seen["image"] is None
    assert "Rock Brown" in seen["prompt"] and "JSON" in seen["prompt"]
    assert [pi.line_text(i) for i in read.items] == ["Rock Black · PLA · Overture · 1 kg × 1"]


def test_finish_word_the_material_did_not_capture_is_not_complete():
    # "Matte Black PLA" would read as plain PLA: the AI path decides instead.
    assert pi.extract_from_page(_fixture("product_matte_group.html")) is None


def test_open_graph_product_needs_the_same_completeness():
    html = ('<html><head><meta property="og:type" content="product">'
            '<meta property="og:title" content="Polymaker PETG Teal 1kg">'
            '<meta property="product:brand" content="Polymaker">'
            '<meta property="product:price:amount" content="22.50"></head></html>')
    assert pi.extract_from_page(html) == [{
        "brand": "Polymaker", "material": "PETG", "color": "Teal", "hex": None, "grams": 1000.0,
        "quantity": 1, "price": 22.5, "nozzle_temp": None, "bed_temp": None}]
    assert pi.extract_from_page(html.replace("Teal ", "")) is None  # no color named


def test_a_title_with_no_product_data_reads_nothing():
    assert pi.extract_from_page("<html><head><title>Hello</title></head></html>") is None
    assert pi.extract_from_page("") is None


# ------------------------------------------------------------- answer validation

def test_screenshot_answer_proposes_three_spools_on_two_types():
    items, skipped = pi.validate_items(ORDER_ANSWER)
    assert skipped == []
    assert [pi.line_text(i) for i in items] == [
        "Black · PLA+ · Sunlu · 1 kg × 2", "Gold · Silk PLA · Sunlu · 1 kg × 1"]
    assert pi.roll_count(items) == 3 and pi.type_count(items) == 2
    assert [i["price"] for i in items] == [17.99, 21.99]


def test_plus_stays_pla_family_and_unknown_materials_are_refused():
    assert pi.material_supported("PLA+") and pi.material_supported("Silk PLA")
    assert pi.material_supported("Shiny Silk PLA") and pi.material_supported("PETG")
    assert not pi.material_supported("PLA/PHA")
    assert not pi.material_supported("Unobtainium")
    items, skipped = pi.validate_items({"items": [
        {"material": "PLA+", "color": "Black", "weight_g": 1000},
        {"material": "PLA/PHA", "color": "Green", "weight_g": 1000, "quantity": 2},
        {"material": "PETG", "color": "Teal"}]})
    assert [i["material"] for i in items] == ["PLA+"]
    assert len(skipped) == 2 and "PLA/PHA" in skipped[0] and "no weight" in skipped[1]
    assert "Left out" in pi.proposal_text(pi.PurchaseRead(items, "screenshot", None, skipped))


def test_nothing_usable_is_a_named_refusal():
    with pytest.raises(pi.PurchaseIntakeError) as exc:
        pi.validate_items({"items": [{"material": "Unobtainium", "color": "Red", "weight_g": 1000}]})
    assert exc.value.reason == "no_items" and "Unobtainium" in str(exc.value)
    for answer, reason in (({"unreadable": True}, "unreadable"), ("not json", "non_json"),
                           ({"items": "no"}, "non_json"), ({"items": []}, "no_items")):
        with pytest.raises(pi.PurchaseIntakeError) as bad:
            pi.validate_items(answer)
        assert bad.value.reason == reason


def test_answer_accepts_fenced_json_and_dedupes_repeated_lines():
    fenced = "```json\n" + json.dumps({"items": [
        {"brand": "Sunlu", "material": "PLA", "color": "Red", "grams": "1 kg", "quantity": 1},
        {"brand": "sunlu", "material": "pla", "color": "red", "weight_g": 1000, "quantity": 2}]}) + "\n```"
    items, _ = pi.validate_items(fenced)
    assert len(items) == 1 and items[0]["quantity"] == 3 and items[0]["grams"] == 1000.0


def test_absurd_totals_are_refused_not_created():
    with pytest.raises(pi.PurchaseIntakeError) as exc:
        pi.validate_items({"items": [
            {"material": "PLA", "color": c, "weight_g": 1000, "quantity": 20} for c in ("Red", "Blue", "Green")]})
    assert exc.value.reason == "too_many"


def test_pack_variants_become_one_line_per_color():
    items, _ = pi.validate_items({"items": [{
        "brand": "Elegoo", "material": "PLA", "weight_g": 1000, "quantity": 1, "price": 40,
        "colors": ["Black", "White", "Gray", "Red"]}]})
    assert [i["color"] for i in items] == ["Black", "White", "Gray", "Red"]
    assert all(i["quantity"] == 1 and i["price"] == 10.0 for i in items)
    assert pi.roll_count(items) == 4 and pi.type_count(items) == 4
    two_packs, _ = pi.validate_items({"items": [{
        "material": "PLA", "weight_g": 1000, "quantity": 2, "colors": ["Black", "White"]}]})
    assert [i["quantity"] for i in two_packs] == [2, 2]


def test_prices_read_from_common_shop_formats():
    assert pi._price("$1,299.00") == 1299.0
    assert pi._price("21,99 €") == 21.99
    assert pi._price(0) is None and pi._price("free") is None and pi._price(True) is None


def test_roll_specs_expand_quantity_and_carry_price_and_url():
    items, _ = pi.validate_items(ORDER_ANSWER)
    specs = pi.roll_specs(items, "https://shop.invalid/order/9")
    assert [(s["color"], s["price"]) for s in specs] == [("Black", 17.99), ("Black", 17.99), ("Gold", 21.99)]
    assert all(s["purchase_url"] == "https://shop.invalid/order/9" and s["grams"] == 1000.0 for s in specs)
    assert "purchase_url" not in pi.roll_specs(items)[0]


def test_screenshot_uses_the_purchase_prompt_not_the_label_prompt():
    seen = {}

    def ai(image, prompt):
        seen["image"], seen["prompt"] = image, prompt
        return ORDER_ANSWER

    read = pi.extract_from_image(b"png", ai)
    assert read.source == "screenshot" and seen["image"] == b"png"
    assert seen["prompt"] != spool_label.LABEL_PROMPT
    assert "weight_g" in seen["prompt"] and "quantity" in seen["prompt"] and "JSON" in seen["prompt"]
    for bad in (None, RuntimeError("down")):
        def failing(image, prompt, bad=bad):
            if isinstance(bad, Exception):
                raise bad
            return bad
        with pytest.raises(pi.PurchaseIntakeError) as exc:
            pi.extract_from_image(b"png", failing)
        assert exc.value.reason == "unreadable"
    with pytest.raises(pi.PurchaseIntakeError):
        pi.extract_from_image(b"", ai)


# ------------------------------------------------------------- recognizing the input

@pytest.mark.parametrize("text", [
    "https://www.amazon.com/dp/B0ABC12345?ref=x",
    "https://store.sunlu.com/products/pla-plus-black",
    "just ordered this filament https://example.invalid/blog/new-spools",
    "https://example.invalid/account/orders/1234",
])
def test_shop_and_order_links_are_purchase_links(text):
    assert pi.purchase_link(text) and pi.purchase_link(text).startswith("https://")


@pytest.mark.parametrize("text", [
    "https://makerworld.com/en/models/123456-benchy#profileId-1",
    "https://www.printables.com/model/1234-benchy",
    "https://example.invalid/blog/new-post",
    "https://www.amazon.com/dp/B0A https://www.amazon.com/dp/B0B",
    "no link here, just 2 spools of filament",
    "https://user:pw@shop.invalid/products/x",
])
def test_model_links_and_other_text_are_not_purchase_links(text):
    assert pi.purchase_link(text) is None


# ------------------------------------------------------------- the bounded fetch

class _Page:
    def __init__(self, body=b"<html></html>", status=200, content_type="text/html; charset=utf-8", headers=None):
        self.body, self.status = body, status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.read_limit = None
        self.closed = False

    def read(self, n=-1):
        self.read_limit = n
        return self.body[:n] if n >= 0 else self.body

    def close(self):
        self.closed = True


def _http_error(url, code, location=None):
    headers = {"Location": location} if location else {}
    return urllib.error.HTTPError(url, code, "x", headers, io.BytesIO(b""))


def test_fetch_is_one_bounded_plain_get():
    calls = []
    page = _Page(b"<html>" + b"x" * 5000 + b"</html>")

    def opener(url, timeout, user_agent):
        calls.append((url, timeout, user_agent))
        return page

    final, html = pi.fetch_page("https://shop.invalid/p", max_bytes=100, open_once=opener)
    assert final == "https://shop.invalid/p" and len(html) == 100 and page.read_limit == 100
    assert calls == [("https://shop.invalid/p", pi.PAGE_TIMEOUT_S, pi.PAGE_UA)] and page.closed
    assert "Mozilla" not in pi.PAGE_UA and pi.PAGE_TIMEOUT_S <= 10 and pi.PAGE_MAX_BYTES <= 2 << 20


def test_fetch_follows_a_few_redirects_and_gives_up():
    hops = []

    def opener(url, timeout, user_agent):
        hops.append(url)
        if url.endswith("/start"):
            raise _http_error(url, 302, "/final")
        return _Page(b"<html>ok</html>")

    final, _ = pi.fetch_page("https://shop.invalid/start", open_once=opener)
    assert final == "https://shop.invalid/final" and len(hops) == 2

    def loop(url, timeout, user_agent):
        raise _http_error(url, 302, "/again")

    with pytest.raises(pi.PurchaseIntakeError) as exc:
        pi.fetch_page("https://shop.invalid/loop", open_once=loop)
    assert exc.value.reason == "fetch_failed"


@pytest.mark.parametrize("code", [403, 429, 503])
def test_refused_pages_are_blocked_with_no_retry(code):
    calls = []

    def opener(url, timeout, user_agent):
        calls.append(url)
        raise _http_error(url, code)

    with pytest.raises(pi.PageBlockedError) as exc:
        pi.fetch_page("https://shop.invalid/p", open_once=opener)
    assert len(calls) == 1 and exc.value.reason == "blocked"
    assert "screenshot" in str(exc.value) and "/spool new" in str(exc.value)


def test_other_fetch_failures_are_named():
    def missing(url, timeout, user_agent):
        raise _http_error(url, 404)

    def slow(url, timeout, user_agent):
        raise TimeoutError("timed out")

    for opener, text in ((missing, "HTTP 404"), (slow, "did not answer")):
        with pytest.raises(pi.PurchaseIntakeError) as exc:
            pi.fetch_page("https://shop.invalid/p", open_once=opener)
        assert exc.value.reason == "fetch_failed" and text in str(exc.value)
    with pytest.raises(pi.PurchaseIntakeError) as exc:
        pi.fetch_page("https://shop.invalid/file.zip",
                      open_once=lambda url, timeout, user_agent: _Page(b"PK", content_type="application/zip"))
    assert "not a web page" in str(exc.value)


def test_private_addresses_are_never_fetched():
    # The default transport is core.model_job's pinned, public-address-only open.
    with pytest.raises(pi.PurchaseIntakeError) as exc:
        pi.fetch_page("http://127.0.0.1:7125/server/info")
    assert exc.value.reason == "fetch_failed"


def test_bot_wall_pages_are_blocked_but_recaptcha_scripts_are_not():
    with pytest.raises(pi.PageBlockedError):
        pi.read_link("https://shop.invalid/p", mock.Mock(),
                     fetcher=lambda url: (url, _fixture("blocked_robot_check.html")))
    assert not pi.looks_blocked(pi.parse_page(_fixture("product_jsonld.html")))  # script mentions captcha
    with pytest.raises(pi.PageBlockedError):  # a JavaScript-only shell has nothing to read
        pi.read_link("https://shop.invalid/p", mock.Mock(),
                     fetcher=lambda url: (url, "<html><body><div id=app></div></body></html>"))


# ------------------------------------------------------------- the shared vision call

class _Reply:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def test_visual_extract_text_only_and_token_budget(monkeypatch):
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data))
        return _Reply(json.dumps({"choices": [{"message": {"content": '{"items": []}'}}]}).encode())

    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    monkeypatch.setattr(vw.urllib.request, "urlopen", fake_urlopen)
    out = vw.visual_extract(None, "p", chat_url=lambda: "http://chat.invalid", model="m",
                            api_key=lambda: "k", context="c", max_tokens=1500)
    assert out == {"items": []}
    assert sent[0]["max_tokens"] == 1500
    assert [part["type"] for part in sent[0]["messages"][0]["content"]] == ["text"]
    vw.visual_extract(b"jpeg", "p", chat_url=lambda: "http://chat.invalid", model="m",
                      api_key=lambda: "k", context="c")
    assert sent[1]["max_tokens"] == 200
    assert [part["type"] for part in sent[1]["messages"][0]["content"]] == ["text", "image_url"]


def test_visual_extract_text_only_needs_the_chat_endpoint(monkeypatch):
    monkeypatch.setenv("PRINT_VISION_URL", "http://vision.invalid/extract")
    monkeypatch.setattr(vw.urllib.request, "urlopen", mock.Mock(side_effect=AssertionError("no call")))
    assert vw.visual_extract(None, "p", chat_url=lambda: "u", model="m",
                             api_key=lambda: None, context="") is None


# ------------------------------------------------------------- recording price and the link

class ShopSpoolman:
    """A stateful fake behind spool_sync's client shape: types created by one
    roll are found by the next, as in real Spoolman."""

    def __init__(self, filaments=(), spools=({"id": 7},)):
        self.filaments = [dict(f) for f in filaments]
        self.vendors = [{"id": 1, "name": "Sunlu"}] if self.filaments else []
        self.spools = [dict(s) for s in spools]
        self.writes = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/filament":
            return [dict(f) for f in self.filaments]
        if method == "GET" and path == "/api/v1/vendor":
            return [dict(v) for v in self.vendors]
        if method == "GET" and path.startswith("/api/v1/spool"):
            return [dict(s) for s in self.spools]
        self.writes.append((method, path, body))
        if method == "POST" and path == "/api/v1/vendor":
            vendor = {"id": len(self.vendors) + 1, "name": body["name"]}
            self.vendors.append(vendor)
            return vendor
        if method == "POST" and path == "/api/v1/filament":
            vendor = next((v for v in self.vendors if v["id"] == body.get("vendor_id")), None)
            made = {"id": 100 + len(self.filaments), "name": body["name"], "material": body["material"],
                    "color_hex": body.get("color_hex"), "vendor": {"name": vendor["name"]} if vendor else None}
            self.filaments.append(made)
            return made
        if method == "POST" and path == "/api/v1/spool":
            made = {"id": max(s["id"] for s in self.spools) + 1}
            self.spools.append(made)
            return made
        return {}

    def posts(self, path):
        return [b for m, p, b in self.writes if m == "POST" and p == path]


def test_create_spool_records_price_and_the_purchase_link():
    fake = ShopSpoolman()
    spec = {"brand": "Sunlu", "material": "PLA+", "color": "Black", "grams": 1000.0, "price": 17.99,
            "purchase_url": "https://shop.invalid/order/9"}
    spool_sync.create_spool(spec, client=fake)
    body = fake.posts("/api/v1/spool")[0]
    assert body["price"] == 17.99 and body["location"] == "Shelf"
    assert body["comment"] == "added from Discord /spool new; purchased from https://shop.invalid/order/9"
    assert mel.created_by_print_handy({"comment": body["comment"]})


def test_create_spool_without_a_purchase_is_unchanged():
    fake = ShopSpoolman()
    spool_sync.create_spool({"brand": None, "material": "PLA", "color": "Red", "grams": 1000.0}, client=fake)
    body = fake.posts("/api/v1/spool")[0]
    assert "price" not in body and body["comment"] == spool_sync.CREATED_SPOOL_COMMENT


# ------------------------------------------------------------- the channel flow

class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.clarifies = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kw):
        self.clarifies.append(kw)
        return S(success=True)


def _event(text, media=None):
    src = S(platform=S(value="discord"), profile="3dprinting", is_bot=False, profile_route_rejected=False,
            user_id="u", chat_id="c", thread_id=None)
    return S(source=src, text=text, internal=False, media_urls=list(media or []))


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "live-session")


def _screenshot(tmp_path):
    path = tmp_path / "order.png"
    path.write_bytes(b"\x89PNG fake screenshot")
    return str(path)


def _flow(event, answers, *, fake=None, ready=True, ai=None, page=None, new_recent=False):
    """Run purchase_intake_dispatch with the clarify answers queued, Spoolman faked
    and the AI/page transports replaced. Returns (outcome, adapter, fake, ai calls)."""
    adapter = FakeAdapter()
    fake = fake if fake is not None else ShopSpoolman()
    queue = list(answers)
    calls = []
    from tools import clarify_gateway as cm

    def wait(cid, timeout=None):
        return queue.pop(0) if queue else None

    def call(image, prompt):
        calls.append((image, prompt))
        return ai

    real_types, real_next, real_create = spool_sync.filament_types, spool_sync.next_spool_id, spool_sync.create_spool
    patches = [
        mock.patch.object(cm, "wait_for_response", side_effect=wait),
        mock.patch.object(mel, "_vision_ready", return_value=ready),
        mock.patch.object(plugin, "_purchase_ai_call", return_value=call),
        mock.patch.object(spool_sync, "filament_types", side_effect=lambda: real_types(client=fake)),
        mock.patch.object(spool_sync, "next_spool_id", side_effect=lambda: real_next(client=fake)),
        mock.patch.object(spool_sync, "create_spool", side_effect=lambda spec: real_create(spec, client=fake)),
    ]
    if page is not None:
        patches.append(mock.patch.object(pi, "fetch_page", side_effect=page))
    plugin._recent_spool_new.clear()
    if new_recent:
        plugin._note_spool_new(event.source)
    for patch in patches:
        patch.start()
    try:
        out = asyncio.run(plugin.purchase_intake_dispatch(event, _gateway(adapter)))
    finally:
        for patch in patches:
            patch.stop()
    return out, adapter, fake, calls


def _questions(adapter):
    return [c["question"] for c in adapter.clarifies]


def test_screenshot_order_creates_three_spools_on_two_types(tmp_path):
    event = _event("just got my filament order", [_screenshot(tmp_path)])
    out, adapter, fake, calls = _flow(event, ["Yes", "Create all"], ai=ORDER_ANSWER)
    assert out == {"action": "skip", "reason": "purchase intake handled"}
    assert adapter.clarifies[0]["choices"] == ["Yes", "No"]
    assert "Add spools from this purchase?" in _questions(adapter)[0]
    proposal = _questions(adapter)[1]
    assert "3 spools on 2 filament types" in proposal
    assert "1. Black · PLA+ · Sunlu · 1 kg × 2" in proposal
    assert "2. Gold · Silk PLA · Sunlu · 1 kg × 1" in proposal
    assert adapter.clarifies[1]["choices"] == ["Create all", "Edit", "Cancel"]
    assert calls[0][0] == b"\x89PNG fake screenshot" and "weight_g" in calls[0][1]
    # Exactly those: two new filament types, three spools, all on the Shelf.
    assert len(fake.posts("/api/v1/filament")) == 2
    spools = fake.posts("/api/v1/spool")
    assert [s["price"] for s in spools] == [17.99, 17.99, 21.99]
    assert {s["location"] for s in spools} == {"Shelf"}
    assert spools[0]["filament_id"] == spools[1]["filament_id"] != spools[2]["filament_id"]
    assert all(s["initial_weight"] == 1000.0 and "purchased from" not in s["comment"] for s in spools)
    assert "Created 3 spools on the Shelf: #8, #9, #10." in adapter.sent[-1]
    assert "2 new filament types added" in adapter.sent[-1]


def test_existing_filament_types_are_matched_not_duplicated(tmp_path):
    fake = ShopSpoolman([{"id": 3, "name": "Black", "material": "PLA+", "color_hex": "000000",
                          "vendor": {"name": "Sunlu"}}])
    event = _event("my filament order", [_screenshot(tmp_path)])
    _flow(event, ["Yes", "Create all"], fake=fake, ai=ORDER_ANSWER)
    assert len(fake.posts("/api/v1/filament")) == 1  # only the new Silk PLA gold
    assert [s["filament_id"] for s in fake.posts("/api/v1/spool")][:2] == [3, 3]


def test_product_link_with_structured_data_makes_no_ai_call_and_records_the_link():
    url = "https://store.sunlu.com/products/pla-plus-black"
    event = _event(f"got this {url}")
    out, adapter, fake, calls = _flow(event, ["Yes", "Create all"], ai=None,
                                      page=lambda link: (link, _fixture("product_jsonld.html")))
    assert calls == []
    assert "From page data, I read 1 spool on 1 filament type" in _questions(adapter)[1]
    body = fake.posts("/api/v1/spool")[0]
    assert body["price"] == 17.99 and body["comment"].endswith(f"purchased from {url}")
    assert "Created 1 spool on the Shelf: #8." in adapter.sent[-1]


def test_blocked_page_refuses_with_the_reason_and_offers_the_manual_walkthrough():
    def blocked(link):
        raise pi.PageBlockedError("HTTP 403")

    event = _event("https://www.amazon.com/dp/B0ABC12345")
    out, adapter, fake, _ = _flow(event, ["Yes", "Done"], page=blocked)
    refusal = _questions(adapter)[1]
    assert "wouldn't let Print Handy read the page (HTTP 403)" in refusal
    assert "screenshot" in refusal and "/spool new" in refusal
    assert adapter.clarifies[1]["choices"] == ["Add a spool by hand", "Done"]
    assert fake.writes == [] and adapter.sent[-1] == "Okay. Nothing was created."


def test_refusal_can_start_the_manual_walkthrough(tmp_path):
    fake = ShopSpoolman([{"id": 3, "name": "Black", "material": "PLA+", "color_hex": "000000",
                          "vendor": {"name": "Sunlu"}}])
    event = _event("filament screenshot", [_screenshot(tmp_path)])
    out, adapter, fake, _ = _flow(event, ["Yes", "Add a spool by hand", "1", "1000", "Create spool #8"],
                                  fake=fake, ai={"unreadable": True})
    assert "couldn't read that purchase" in _questions(adapter)[1]
    assert "Which filament is the new spool?" in _questions(adapter)[2]
    assert len(fake.posts("/api/v1/spool")) == 1 and "Created spool #" in adapter.sent[-1]


def test_unreadable_answer_never_creates_anything(tmp_path):
    event = _event("filament", [_screenshot(tmp_path)])
    out, adapter, fake, _ = _flow(event, ["Yes", "Done"], ai="sorry, I cannot read that")
    assert "wasn't usable" in _questions(adapter)[1] and fake.writes == []


def test_cancel_and_timeout_create_nothing(tmp_path):
    event = _event("filament", [_screenshot(tmp_path)])
    for answers, text in ((["Yes", "Cancel"], "Cancelled. Nothing changed."),
                          (["Yes"], "Cancelled. Nothing changed.")):
        out, adapter, fake, _ = _flow(event, answers, ai=ORDER_ANSWER)
        assert fake.writes == [] and adapter.sent[-1] == text


def test_declining_the_offer_lets_the_message_go_on_untouched(tmp_path):
    for answers in (["No"], []):
        event = _event("https://store.sunlu.com/products/pla-plus-black")
        out, adapter, fake, calls = _flow(event, answers, ai=ORDER_ANSWER)
        assert out is None and fake.writes == [] and calls == [] and adapter.sent == []


def test_a_failed_create_stops_and_names_what_exists(tmp_path):
    event = _event("filament", [_screenshot(tmp_path)])
    fake = ShopSpoolman()
    real = fake.__call__

    def flaky(method, path, body=None):
        if method == "POST" and path == "/api/v1/spool" and len(fake.posts("/api/v1/spool")) == 1:
            fake.writes.append((method, path, body))
            raise spool_sync.SpoolError("Spoolman is down")
        return real(method, path, body)

    fake_client = flaky
    adapter = FakeAdapter()
    from tools import clarify_gateway as cm
    queue = ["Yes", "Create all"]
    real_create = spool_sync.create_spool
    with mock.patch.object(cm, "wait_for_response", side_effect=lambda cid, timeout=None: queue.pop(0)), \
            mock.patch.object(mel, "_vision_ready", return_value=True), \
            mock.patch.object(plugin, "_purchase_ai_call", return_value=lambda i, p: ORDER_ANSWER), \
            mock.patch.object(spool_sync, "create_spool", side_effect=lambda s: real_create(s, client=fake_client)):
        asyncio.run(plugin.purchase_intake_dispatch(event, _gateway(adapter)))
    assert "Created #8." in adapter.sent[-1] and "Spool 2 of 3" in adapter.sent[-1]
    assert "Spoolman is down" in adapter.sent[-1] and "the rest were not created" in adapter.sent[-1]


def test_edit_walks_the_normal_steps_per_roll(tmp_path):
    fake = ShopSpoolman([{"id": 3, "name": "Black", "material": "PLA+", "color_hex": "000000",
                          "vendor": {"name": "Sunlu"}}])
    answer = {"items": [{"brand": "Sunlu", "material": "PLA+", "color": "Black", "weight_g": 1000, "quantity": 1}]}
    event = _event("my filament", [_screenshot(tmp_path)])
    out, adapter, fake, _ = _flow(event, ["Yes", "Edit", "1", "1000", "Create spool #8"], fake=fake, ai=answer)
    assert any("Spool 1 of 1: Black · PLA+ · Sunlu · 1 kg × 1" in m for m in adapter.sent)
    assert "Which filament is the new spool?" in _questions(adapter)[2]
    assert len(fake.posts("/api/v1/spool")) == 1
    assert "Created 1 spool on the Shelf: #" in adapter.sent[-1]


# ------------------------------------------------------------- when the offer is made

def test_no_ai_route_means_no_offer_and_no_change(tmp_path):
    shot = _event("my filament order", [_screenshot(tmp_path)])
    link = _event("https://www.amazon.com/dp/B0ABC12345")
    for event in (shot, link):
        out, adapter, fake, calls = _flow(event, ["Yes", "Create all"], ready=False, ai=ORDER_ANSWER,
                                          page=lambda url: pytest.fail("no fetch without an AI route"))
        assert out is None and adapter.clarifies == [] and adapter.sent == [] and fake.writes == []


def test_label_readers_alone_do_not_open_the_purchase_offer(tmp_path):
    # Barcode and OCR routes read a spool label (#514); they cannot read an order.
    with mock.patch.object(spool_label, "tesseract_available", return_value=True):
        out, adapter, _, _ = _flow(_event("filament", [_screenshot(tmp_path)]), ["Yes"],
                                   ready=False, ai=ORDER_ANSWER)
    assert out is None and adapter.clarifies == []


def test_model_links_keep_their_routing():
    for link in ("https://makerworld.com/en/models/123456-benchy#profileId-1",
                 "https://www.printables.com/model/1234-benchy",
                 "https://www.thingiverse.com/thing:4711 filament"):
        out, adapter, fake, calls = _flow(_event(link), ["Yes"], ai=ORDER_ANSWER,
                                          page=lambda url: pytest.fail("a model link is never fetched here"))
        assert out is None and adapter.clarifies == [] and calls == []
        from print_handy.core.link_intake import model_link
        assert model_link(link)


def test_image_needs_filament_words_or_a_fresh_spool_new(tmp_path):
    shot = _screenshot(tmp_path)
    out, adapter, *_ = _flow(_event("look at this", [shot]), ["Yes"], ai=ORDER_ANSWER)
    assert out is None and adapter.clarifies == []
    out, adapter, *_ = _flow(_event("", [shot]), ["Yes", "Cancel"], ai=ORDER_ANSWER, new_recent=True)
    assert out["reason"] == "purchase intake handled" and adapter.clarifies[0]["choices"] == ["Yes", "No"]
    out, adapter, *_ = _flow(_event("2 spools of PLA+", [shot]), ["No"], ai=ORDER_ANSWER)
    assert adapter.clarifies[0]["choices"] == ["Yes", "No"]


def test_spool_new_marks_the_channel_but_only_for_a_while():
    source = _event("x").source
    plugin._recent_spool_new.clear()
    assert not plugin._spool_new_recent(source)
    plugin._note_spool_new(source)
    assert plugin._spool_new_recent(source)
    with mock.patch.object(plugin.time, "monotonic", return_value=plugin.time.monotonic() + 3600):
        assert not plugin._spool_new_recent(source)


def test_commands_files_and_plain_text_are_ignored(tmp_path):
    for event in (_event("/spool new https://www.amazon.com/dp/B0ABC12345"), _event("hello"),
                  _event("filament", [str(tmp_path / "missing.png")]),
                  _event("filament", [str(tmp_path / "x.stl")])):
        out, adapter, *_ = _flow(event, ["Yes"], ai=ORDER_ANSWER)
        assert out is None and adapter.clarifies == []


def test_hook_runs_right_after_the_model_link_hook():
    hooks = list(plugin._DISPATCH_HOOKS)
    assert hooks.index(plugin.purchase_intake_dispatch) == hooks.index(plugin.model_link_dispatch) + 1
