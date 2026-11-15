"""#1536 checklist item 1: the Spoolman client reads filaments and vendors
from whichever instance it is pointed at (the bundled one or an outside
one), alongside the spool reads and writes it already had."""
import pytest

from print_handy.core import spoolman_client


@pytest.fixture
def transport(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, timeout=8, base_url=None):
        calls.append((method, path, body))
        if path == "/api/v1/filament":
            return [{"id": 3, "name": "Galaxy Black", "vendor_id": 9}]
        if path == "/api/v1/filament/3":
            return {"id": 3, "name": "Galaxy Black", "vendor_id": 9}
        if path == "/api/v1/vendor":
            return [{"id": 9, "name": "Prusament"}]
        if path == "/api/v1/vendor/9":
            return {"id": 9, "name": "Prusament"}
        raise spoolman_client.SpoolmanUnavailable("no such route")

    monkeypatch.setattr(spoolman_client, "request", fake_request)
    return calls


def test_filament_and_vendor_reads_go_through_the_configured_instance(transport):
    assert spoolman_client.list_filaments() == [
        {"id": 3, "name": "Galaxy Black", "vendor_id": 9}]
    assert spoolman_client.get_filament(3)["name"] == "Galaxy Black"
    assert spoolman_client.list_vendors() == [{"id": 9, "name": "Prusament"}]
    assert spoolman_client.get_vendor(9)["name"] == "Prusament"
    assert [path for _method, path, _body in transport] == [
        "/api/v1/filament", "/api/v1/filament/3",
        "/api/v1/vendor", "/api/v1/vendor/9"]


def test_catalog_reads_are_best_effort_when_the_instance_is_down(transport):
    assert spoolman_client.get_filament(404) is None
    assert spoolman_client.get_vendor(404) is None
