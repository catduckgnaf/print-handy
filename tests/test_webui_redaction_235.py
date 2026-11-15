"""Credentials embedded in connection errors never reach status cards."""
import pytest
from print_handy import settings
from print_handy.webui import dashboard


@pytest.mark.parametrize("detail", [
    "connect https://alice:secret-pass@printer.example:7125/server failed",
    "OctoPrint http://alice:p%40ss@printer.example/api?apikey=secret-token failed",
    "bad http://alice:secret-pass@[broken URL",
])
@pytest.mark.parametrize("raises", [True, False])
def test_dashboard_redacts_connection_error_urls(tmp_path, detail, raises):
    path = tmp_path / "local.yaml"
    path.write_text(f"paths:\n  state_home: {tmp_path}/state\n")
    data = settings.load(local=path, environ={})

    def read_status(_pid):
        if raises:
            raise OSError(detail)
        return 200, {"status": {"state": "unavailable", "detail": detail}}

    result = dashboard.build_dashboard(
        data, status_for=read_status,
        spools_for=lambda _pid: (200, {"ok": True}), hardware_for=lambda _pid: None)
    shown = result["cards"][0]["detail"]
    for secret in ("alice", "secret-pass", "p%40ss", "secret-token"):
        assert secret not in shown
