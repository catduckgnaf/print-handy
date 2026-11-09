import re
from pathlib import Path

import pytest

from print_handy.printers.flashforge.zmod import stock

ROOT = Path(__file__).resolve().parents[2]


def test_stock_detail_always_posts_auth_fields(monkeypatch):
    monkeypatch.setattr(stock, "_stock_credentials", lambda: ("serial", "code"))
    seen = {}

    def post(url, body):
        seen.update(url=url, body=body)
        return {"code": 0, "detail": {"status": "ready"}}

    assert stock.read_stock_detail({"moonraker_url": "http://printer:7125"}, post=post) == {"status": "ready"}
    assert seen["body"] == {"serialNumber": "serial", "checkCode": "code"}
    assert seen["url"].endswith("/detail")


def test_stock_detail_missing_credentials_never_posts(monkeypatch):
    def missing():
        raise RuntimeError("stock IFS credentials missing (AD5X_CHECK_CODE)")

    monkeypatch.setattr(stock, "_stock_credentials", missing)
    with pytest.raises(RuntimeError, match="credentials missing"):
        stock.read_stock_detail({"moonraker_url": "http://printer:7125"},
                                post=lambda *_: pytest.fail("POST attempted without credentials"))


@pytest.mark.parametrize("reply", [{"code": 400, "message": "bad request"}, None, {"code": 0}])
def test_stock_detail_refusal_raises_instead_of_reporting_state(monkeypatch, reply):
    monkeypatch.setattr(stock, "_stock_credentials", lambda: ("serial", "code"))
    with pytest.raises(RuntimeError):
        stock.read_stock_detail({"moonraker_url": "http://printer:7125"}, post=lambda *_: reply)
