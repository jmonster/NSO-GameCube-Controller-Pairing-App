"""Stop must publish neutral state after the last in-flight update."""
import threading
import unittest
from unittest.mock import Mock

from _support import fake_module, load_module


class OutputNeutralizationTests(unittest.TestCase):
    def manager(self):
        module = load_module('emulation_manager.py', {
            'virtual_gamepad': fake_module(VirtualGamepad=object, create_gamepad=Mock()),
            'controller_constants': fake_module(BUTTON_MAPPING={}),
            'calibration': fake_module(CalibrationManager=object),
        })
        manager = module.EmulationManager(Mock())
        manager.is_emulating = True
        manager.gamepad = Mock()
        return manager

    def test_stop_resets_flushes_closes_once(self):
        manager = self.manager()
        pad = manager.gamepad
        manager.stop()
        manager.stop()
        self.assertEqual([c[0] for c in pad.method_calls],
                         ['stop_rumble_listener', 'reset', 'update', 'close'])
        self.assertIsNone(manager.gamepad)
        self.assertFalse(manager.is_emulating)

    def test_each_teardown_failure_still_attempts_remaining_steps(self):
        for failed in ('stop_rumble_listener', 'reset', 'update', 'close'):
            with self.subTest(failed=failed):
                manager = self.manager()
                pad = manager.gamepad
                getattr(pad, failed).side_effect = OSError(failed)
                manager.stop()
                self.assertEqual([c[0] for c in pad.method_calls],
                                 ['stop_rumble_listener', 'reset', 'update', 'close'])
                self.assertIsNone(manager.gamepad)

    def test_stop_cannot_be_overwritten_by_inflight_input(self):
        manager = self.manager()
        pad = manager.gamepad
        manager._cal_mgr.calibrate_trigger_fast.return_value = 0
        manager._cal_mgr._calibration = {}
        entered, release = threading.Event(), threading.Event()
        pad.left_joystick.side_effect = lambda **kw: (entered.set(), release.wait(2))
        update = threading.Thread(target=manager.update, args=(1, 0, 0, 0, 0, 0, {}))
        stop = threading.Thread(target=manager.stop)
        try:
            update.start()
            self.assertTrue(entered.wait(1))
            stop.start()
            release.set()
            update.join(2)
            stop.join(2)
            self.assertFalse(update.is_alive() or stop.is_alive())
            self.assertEqual([c[0] for c in pad.method_calls][-4:],
                             ['stop_rumble_listener', 'reset', 'update', 'close'])
            before = list(pad.method_calls)
            manager.update(1, 0, 0, 0, 0, 0, {})
            self.assertEqual(pad.method_calls, before)
        finally:
            release.set()
            update.join(2)
            if stop.ident is not None:
                stop.join(2)
