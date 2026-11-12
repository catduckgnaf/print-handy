"""#299: the listener registry behind the gate, supervisor and healthcheck.

One registry (print_handy.listeners) maps every vendor connection to its
listener domain: implemented (moonraker, bambulab_lan since Stage B, and
the four polling families since #460), planned (domain known, no listener
yet) or none (offline). The planned/none distinction is what lets the
supervisor idle a zero-listener configuration instead of failing it, while
an unresolved configuration still fails distinctly (#256 semantics).
Offline: registry, fake settings and mocks only.
"""
import importlib.util
import unittest
from unittest import mock

from print_handy import healthcheck, listeners, logs, printers, settings
from print_handy.printers import base as printer_base


def data(model):
    return {"printer": {"model": model}}


class RegistryShapeTests(unittest.TestCase):
    def test_every_connection_has_exactly_one_domain(self):
        self.assertEqual(set(listeners.DOMAINS), set(printer_base.CONNECTIONS))
        for connection, domain in listeners.DOMAINS.items():
            with self.subTest(connection=connection):
                self.assertEqual(domain.connection, connection)
                self.assertEqual(domain.name, connection)

    def test_implemented_domains_cover_every_connection_but_offline(self):
        implemented = [c for c, d in listeners.DOMAINS.items() if d.implemented]
        self.assertEqual(implemented, ["moonraker", "bambulab_lan", "prusalink",
                                       "elegoo_sdcp", "octoprint", "flashforge_stock"])
        self.assertEqual(listeners.implemented_connections(),
                         tuple(implemented))
        # Backwards-compatible name the #256 code and tests already use.
        self.assertEqual(healthcheck.LISTENER_CONNECTIONS,
                         tuple(implemented))

    def test_implemented_domain_points_at_a_real_module_and_runtime(self):
        for connection in ("moonraker", "bambulab_lan"):
            with self.subTest(connection=connection):
                domain = listeners.DOMAINS[connection]
                self.assertIsNotNone(importlib.util.find_spec(domain.module))
                self.assertIsNotNone(domain.runtime)
        self.assertEqual(listeners.DOMAINS["moonraker"].runtime,
                         healthcheck.RUNTIME)
        from print_handy.printers.bambulab import listener as bambu_listener
        self.assertEqual(listeners.DOMAINS["bambulab_lan"].runtime,
                         bambu_listener.RUNTIME)
        self.assertEqual(listeners.DOMAINS["bambulab_lan"].module,
                         "print_handy.printers.bambulab.listener")

    def test_family_domains_point_at_the_shared_polling_listener(self):
        # #460: one module serves the four polling families; the registry
        # runtime for each must equal the listener's own runtime table,
        # or the healthcheck would expect a heartbeat that never comes.
        from print_handy.runtime import family_event_listener
        for connection in ("prusalink", "elegoo_sdcp",
                           "octoprint", "flashforge_stock"):
            with self.subTest(connection=connection):
                domain = listeners.DOMAINS[connection]
                self.assertEqual(domain.availability, listeners.IMPLEMENTED)
                self.assertEqual(domain.module,
                                 "print_handy.runtime.family_event_listener")
                self.assertIsNotNone(importlib.util.find_spec(domain.module))
                self.assertEqual(domain.runtime,
                                 family_event_listener.RUNTIMES[connection])

    def test_planned_and_none_are_distinct_states(self):
        # No connection is PLANNED any more (#460 delivered the last four);
        # the state itself stays distinct, pinned on a fabricated domain.
        planned = listeners.ListenerDomain(
            name="someday", connection="prusalink",
            availability=listeners.PLANNED)
        self.assertEqual(planned.availability, listeners.PLANNED)
        self.assertIsNone(planned.module)
        self.assertFalse(planned.implemented)
        offline = listeners.DOMAINS["offline"]
        self.assertEqual(offline.availability, listeners.NONE)
        self.assertFalse(offline.implemented)


class RegistryClassificationTests(unittest.TestCase):
    def test_every_registered_model_is_classified_by_its_connection(self):
        for key, model in printers.MODELS.items():
            with self.subTest(model=key):
                domain = healthcheck.listener_domain(data(key))
                self.assertIs(domain, listeners.DOMAINS[model.vendor.connection])
                self.assertEqual(healthcheck.listener_required(data(key)),
                                 domain.availability == listeners.IMPLEMENTED)

    def test_moonraker_models_still_require_the_listener(self):
        # Behaviour-identical pin from #256: the same models, the same answer.
        for model in ("flashforge.ad5x", "klipper.generic", "sovol.sv06",
                      "elegoo.neptune4"):
            with self.subTest(model=model):
                self.assertTrue(healthcheck.listener_required(data(model)))

    def test_bambu_models_require_the_listener(self):
        # #299 Stage B: bambulab_lan flipped from planned to implemented.
        for model in ("bambulab.a1", "bambulab.p1s", "bambulab.x1c",
                      "bambulab.h2s"):
            with self.subTest(model=model):
                self.assertTrue(healthcheck.listener_required(data(model)))

    def test_family_models_require_the_listener(self):
        # #460: the polling families flipped from planned to implemented.
        for model in ("flashforge_stock.ad5x",
                      "octoprint.marlin", "prusa.mk4s", "prusa.coreone",
                      "elegoo_sdcp.centauricarbon"):
            with self.subTest(model=model):
                self.assertTrue(healthcheck.listener_required(data(model)))


class LauncherTests(unittest.TestCase):
    def test_planned_domain_is_nothing_to_run_not_a_failure(self):
        planned = listeners.ListenerDomain(
            name="someday", connection="prusalink",
            availability=listeners.PLANNED)
        with mock.patch.object(healthcheck, "listener_domain",
                               return_value=planned):
            self.assertEqual(listeners.launch({}), 3)

    def test_family_domain_runs_the_shared_polling_module(self):
        with mock.patch.object(healthcheck, "listener_domain",
                               return_value=listeners.DOMAINS["prusalink"]), \
                mock.patch.object(listeners.runpy, "run_module") as run_module:
            self.assertEqual(listeners.launch({}), 0)
        run_module.assert_called_once_with(
            "print_handy.runtime.family_event_listener", run_name="__main__")

    def test_bambu_domain_runs_its_registered_module(self):
        with mock.patch.object(healthcheck, "listener_domain",
                               return_value=listeners.DOMAINS["bambulab_lan"]), \
                mock.patch.object(listeners.runpy, "run_module") as run_module:
            self.assertEqual(listeners.launch({}), 0)
        run_module.assert_called_once_with(
            "print_handy.printers.bambulab.listener", run_name="__main__")

    def test_launch_drops_a_preimported_domain_module_before_runpy(self):
        # #409: printer discovery pre-imports the Bambu listener module
        # (it scans every vendor-package module for MODEL), so launch
        # must drop that stale sys.modules entry before runpy runs the
        # module as __main__, or runpy warns and reuses the import.
        import sys
        module_name = listeners.DOMAINS["bambulab_lan"].module
        missing = object()
        previous = sys.modules.get(module_name, missing)
        sys.modules[module_name] = object()  # a stale pre-import
        seen = {}

        def fake_run_module(name, run_name=None):
            seen["name"] = name
            seen["present_during_run"] = name in sys.modules

        try:
            with mock.patch.object(healthcheck, "listener_domain",
                                   return_value=listeners.DOMAINS["bambulab_lan"]), \
                    mock.patch.object(listeners.runpy, "run_module",
                                      side_effect=fake_run_module):
                self.assertEqual(listeners.launch({}), 0)
        finally:
            if previous is missing:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
        self.assertEqual(seen["name"], module_name)
        self.assertFalse(seen["present_during_run"])

    def test_none_domain_is_nothing_to_run(self):
        with mock.patch.object(healthcheck, "listener_domain",
                               return_value=listeners.DOMAINS["offline"]):
            self.assertEqual(listeners.launch({}), 3)

    def test_implemented_domain_runs_its_registered_module(self):
        with mock.patch.object(healthcheck, "listener_domain",
                               return_value=listeners.DOMAINS["moonraker"]), \
                mock.patch.object(listeners.runpy, "run_module") as run_module:
            self.assertEqual(listeners.launch({}), 0)
        run_module.assert_called_once_with(
            "print_handy.runtime.moonraker_event_listener", run_name="__main__")

    def test_unresolved_configuration_keeps_the_gate_failure_code(self):
        with mock.patch.object(healthcheck, "listener_domain",
                               side_effect=settings.SettingsError("bad model")):
            self.assertEqual(listeners.main(), 2)


class MultiPrinterLaunchTests(unittest.TestCase):
    """#336: a per-printer launch resolves that printer's settings view,
    applies its channel claim, and records the instance identity."""

    ROSTER_YAML = (
        "discord:\n  chat_id: \"111\"\n"
        "printers:\n"
        "  - id: moon-two\n    model: klipper.generic\n    chat_id: \"chat-two\"\n"
        "  - id: bambu-two\n    model: bambulab.a1\n")

    def roster(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            local = Path(td, "local.yaml")
            local.write_text(self.ROSTER_YAML)
            return settings.load(home=settings.HOME, local=local, environ={})

    def tearDown(self):
        settings.set_override(None)
        listeners._set_instance_printer_id(None)
        # launch() binds printer_id to the log context for the process
        # lifetime (#390 stage 4); in-process tests must not leak it into
        # unrelated log assertions in other suites.
        logs.clear_context()

    def test_launch_runs_the_domain_module_for_that_printer(self):
        with mock.patch.object(listeners.runpy, "run_module") as run_module:
            self.assertEqual(
                listeners.launch(self.roster(), printer_id="moon-two"), 0)
        run_module.assert_called_once_with(
            "print_handy.runtime.moonraker_event_listener", run_name="__main__")
        self.assertEqual(listeners.instance_printer_id(), "moon-two")
        self.assertEqual(logs.bound_context().get("printer_id"), "moon-two")

    def test_launch_installs_the_printer_view_with_its_channel_claim(self):
        seen = {}

        def fake_run_module(module, run_name=None):
            seen["chat_id"] = settings.current()["discord"]["chat_id"]
            seen["model"] = settings.current()["printer"]["model"]

        with mock.patch.object(listeners.runpy, "run_module",
                               side_effect=fake_run_module):
            self.assertEqual(
                listeners.launch(self.roster(), printer_id="moon-two"), 0)
        self.assertEqual(seen, {"chat_id": "chat-two", "model": "klipper.generic"})

    def test_bambu_printer_launches_the_bambu_module(self):
        with mock.patch.object(listeners.runpy, "run_module") as run_module:
            self.assertEqual(
                listeners.launch(self.roster(), printer_id="bambu-two"), 0)
        run_module.assert_called_once_with(
            "print_handy.printers.bambulab.listener", run_name="__main__")
        self.assertEqual(listeners.instance_printer_id(), "bambu-two")

    def test_unknown_printer_id_is_a_configuration_failure(self):
        with self.assertRaises(settings.SettingsError):
            listeners.launch(self.roster(), printer_id="ghost")
        self.assertIsNone(listeners.instance_printer_id())
        with mock.patch.object(settings, "current", return_value=self.roster()):
            self.assertEqual(listeners.main(["--printer", "ghost"]), 2)

    def test_printer_without_a_listener_domain_is_nothing_to_run(self):
        # Every real model has a listener since #460, so the nothing-to-run
        # path is pinned with a fabricated planned domain.
        planned = listeners.ListenerDomain(
            name="someday", connection="prusalink",
            availability=listeners.PLANNED)
        roster = {"printers": [{"id": "prusa-two", "model": "prusa.mk4s"}]}
        with mock.patch.object(healthcheck, "listener_domain",
                               return_value=planned), \
                mock.patch.object(listeners.runpy, "run_module") as run_module:
            self.assertEqual(listeners.launch(roster, printer_id="prusa-two"), 3)
        run_module.assert_not_called()

    def test_main_parses_the_printer_flag(self):
        with mock.patch.object(listeners, "launch", return_value=0) as launch:
            self.assertEqual(listeners.main(["--printer", "moon-two"]), 0)
        launch.assert_called_once_with(printer_id="moon-two")

    def test_main_printer_flag_without_a_value_is_a_usage_error(self):
        self.assertEqual(listeners.main(["--printer"]), 2)


class StatusContractTests(unittest.TestCase):
    def test_adapter_normalised_statuses_fit_the_contract(self):
        from print_handy.printers.bambulab.adapter import BambuAdapter
        from print_handy.printers.elegoo_sdcp.adapter import ElegooSdcpAdapter
        from print_handy.printers.prusa.adapter import PrusaAdapter

        for cls in (BambuAdapter, PrusaAdapter, ElegooSdcpAdapter):
            with self.subTest(adapter=cls.__name__):
                status = object.__new__(cls).normalize_status({})
                self.assertEqual(listeners.validate_status(status), [])
        self.assertEqual(
            listeners.validate_status(printer_base.basic_status({})), [])

    def test_validate_status_flags_contract_breaks(self):
        problems = listeners.validate_status(
            {"state": 3, "filename": "", "progress": 2, "hotend_c": None,
             "bed_c": None})
        self.assertTrue(any("state" in p for p in problems))
        self.assertTrue(any("progress" in p for p in problems))
        self.assertTrue(any("missing" in p
                            for p in listeners.validate_status({})))


if __name__ == "__main__":
    unittest.main()
