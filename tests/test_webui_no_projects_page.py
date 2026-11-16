"""The Web UI has no Projects page (catduck, 2026-11-16; #1565).

The multi-part project tracking itself stays: the ``project_v1``
manifests, the print records, :mod:`print_handy.core.projects` and the
chat commands (``/print-next``, ``/print-again``) are untouched. Only
the page, its routes and its navigation entry are gone.
"""
import importlib.util

from print_handy.webui import routes, server, shell


def test_projects_is_not_in_the_web_ui_navigation():
    assert "/projects" not in {path for path, _label in shell.NAV}
    assert "Projects" not in {label for _path, label in shell.NAV}


def test_projects_has_no_route_of_its_own():
    for path in ("/projects", "/api/projects"):
        route, _capture = routes.resolve("GET", path)
        # Only the catch-all answers it now, like any unknown path.
        assert route is not None and route.handler == "_not_found"
    assert not hasattr(server._Handler, "_projects_route")
    assert importlib.util.find_spec("print_handy.webui.projects") is None


def test_the_internal_project_tracking_stays():
    from print_handy.core import projects
    assert callable(projects.from_files) and callable(projects.from_design)
    assert callable(projects.plate_suffix) and callable(projects.project_ref)
    assert projects.project_ref(82, "B") == "82B"
