import unittest
from copy import deepcopy
from functools import partial
from print_handy.printers.flashforge.zmod.native_idle import require_idle, require_idle_settled
from print_handy.core.model_job import _require_idle_before_mapped_launch

class NativeIdleTests(unittest.TestCase):
    def test_mapped_launch_guard_refusal_is_clean_pre_upload_exit(self):
        refusal = "REFUSED: idle guard failed: Moonraker bed.target == 0 (observed 60.0)"

        def reject(_moonraker, _native):
            raise RuntimeError(refusal)

        with self.assertRaises(SystemExit) as caught:
            _require_idle_before_mapped_launch(reject, lambda: {}, lambda: {})
        self.assertEqual(str(caught.exception), refusal + ". Nothing uploaded or started.")
        self.assertIsNone(caught.exception.__cause__)

    def test_mapped_launch_guard_does_not_reclassify_other_runtime_errors(self):
        def fail(_moonraker, _native):
            raise RuntimeError("telemetry unavailable")

        with self.assertRaisesRegex(RuntimeError, "telemetry unavailable"):
            _require_idle_before_mapped_launch(fail, lambda: {}, lambda: {})

    def test_settle_timeout_is_a_clean_idle_refusal(self):
        # P1-42: Moonraker is idle but the native post-cancel residue never
        # settles. Nothing was sent to the printer, so the timeout must be
        # classified like every other idle-guard refusal (REFUSED: prefix) —
        # otherwise it surfaces as a traceback and the plate claim is kept.
        moonraker = dict(state='ready', print=dict(state='standby'),
                         extruder=dict(target=0), bed=dict(target=0))
        native = dict(status='cancel', leftTargetTemp=0, rightTargetTemp=0, platTargetTemp=0,
                      chamberTargetTemp=0, errorCode='', printProgress=5, printDuration=0)
        ticks = iter([0.0, 100.0, 200.0])
        with self.assertRaisesRegex(RuntimeError, r"^REFUSED: idle guard failed: Printer screen still reports"):
            require_idle_settled(lambda: moonraker, lambda: native,
                                 sleep=lambda _s: None, clock=lambda: next(ticks, 300.0))
        fast = partial(require_idle_settled, timeout_s=0, sleep=lambda _s: None,
                       clock=lambda: 1000.0)
        with self.assertRaises(SystemExit) as caught:
            _require_idle_before_mapped_launch(fast, lambda: moonraker, lambda: native)
        self.assertTrue(str(caught.exception).startswith("REFUSED: idle guard failed:"))

    def setUp(self):
        self.m = dict(state='ready', print=dict(state='standby'), extruder=dict(target=0), bed=dict(target=0))
        self.n = dict(status='cancel', leftTargetTemp=0, rightTargetTemp=0, platTargetTemp=0, chamberTargetTemp=0, errorCode='', printProgress=0, printDuration=0)
    def test_cancel_is_corroborated_idle(self):
        self.assertTrue(require_idle(self.m,self.n))
    def test_ready_is_corroborated_idle(self):
        self.n['status']='ready'
        self.assertTrue(require_idle(self.m,self.n))
    def test_reject_active_native(self):
        for s in ['printing','paused','unknown']:
            n=deepcopy(self.n); n['status']=s
            with self.assertRaises(RuntimeError): require_idle(self.m,n)
    def test_reject_active_moonraker(self):
        m = deepcopy(self.m)
        m['print']['state']='printing'
        with self.assertRaises(RuntimeError): require_idle(m,self.n)
    def test_reject_heat(self):
        for key in ['leftTargetTemp','rightTargetTemp','platTargetTemp','chamberTargetTemp']:
            n=deepcopy(self.n); n[key]=60
            with self.assertRaises(RuntimeError): require_idle(self.m,n)
    def test_rejection_names_exact_failed_predicate_and_value(self):
        n = deepcopy(self.n)
        n['printDuration'] = 'not-a-number'
        with self.assertRaisesRegex(RuntimeError, r"native printDuration == 0 \(observed 'not-a-number'\)"):
            require_idle(self.m, n)

    def test_finished_job_states_are_idle_when_cold(self):
        # 2026-11-04: a cancel left print.state "cancelled" and the next launch
        # was refused until the printer was rebooted.
        for state in ("cancelled", "complete"):
            m = deepcopy(self.m)
            m["print"]["state"] = state
            self.assertTrue(require_idle(m, self.n), state)
            self.assertTrue(require_idle_settled(lambda: m, lambda: self.n, timeout_s=0), state)

    def test_finished_job_state_still_refused_when_hot(self):
        m = deepcopy(self.m)
        m["print"]["state"] = "cancelled"
        m["extruder"]["target"] = 215
        with self.assertRaisesRegex(RuntimeError, r"extruder\.target == 0"):
            require_idle(m, self.n)

    def test_error_state_is_not_idle(self):
        m = deepcopy(self.m)
        m["print"]["state"] = "error"
        with self.assertRaises(RuntimeError):
            require_idle(m, self.n)

    def test_rejection_names_moonraker_state_predicate(self):
        m = deepcopy(self.m)
        m['print']['state'] = 'printing'
        with self.assertRaisesRegex(RuntimeError, r"Moonraker print.state is standby/complete/cancelled \(observed 'printing'\)"):
            require_idle(m, self.n)

    def test_malformed_values_fail_closed_with_diagnostic(self):
        for n in (None, [], {'status':'cancel', 'leftTargetTemp': [], 'rightTargetTemp':0,
                             'platTargetTemp':0, 'chamberTargetTemp':0, 'errorCode':'',
                             'printProgress':0, 'printDuration':0}):
            with self.assertRaisesRegex(RuntimeError, 'REFUSED: idle guard failed'):
                require_idle(self.m, n)

    def test_accepts_numeric_native_activity_zero(self):
        for value in (0, 0.0):
            n = deepcopy(self.n)
            n['printProgress'] = value
            n['printDuration'] = value
            self.assertTrue(require_idle(self.m, n))

    def test_rejects_numeric_string_native_activity_zero(self):
        for key in ('printProgress', 'printDuration'):
            for value in ('0', '0.0'):
                n = deepcopy(self.n)
                n[key] = value
                with self.assertRaisesRegex(RuntimeError, f'native {key} == 0'):
                    require_idle(self.m, n)

    def test_reject_boolean_targets_and_activity(self):
        for key in ('leftTargetTemp', 'rightTargetTemp', 'platTargetTemp', 'chamberTargetTemp',
                    'printProgress', 'printDuration'):
            n = deepcopy(self.n)
            n[key] = False
            with self.assertRaises(RuntimeError):
                require_idle(self.m, n)

    def test_reject_error_or_unknown_activity(self):
        for key,value in [('errorCode','E1'),('errorCode',None),('printProgress',1),('printDuration',2),('printDuration',None)]:
            n=deepcopy(self.n); n[key]=value
            with self.assertRaises(RuntimeError): require_idle(self.m,n)
if __name__=='__main__': unittest.main()
