import threading
import types
import unittest
from unittest.mock import Mock
from _support import fake_module, load_module


class OutputTeardownTests(unittest.TestCase):
    def manager(self):
        module = load_module('emulation_manager.py', {
            'virtual_gamepad': fake_module(VirtualGamepad=object, create_gamepad=Mock()),
            'controller_constants': fake_module(BUTTON_MAPPING={}, DOLPHIN_BUTTON_MAPPING={}),
            'calibration': fake_module(CalibrationManager=object),
        })
        return module.EmulationManager(types.SimpleNamespace(_calibration={},
                           calibrate_trigger_fast=lambda value, side: value))

    def test_stop_neutralizes_flushes_and_closes_in_order(self):
        manager = self.manager(); pad = manager.gamepad = Mock()
        manager.stop()
        self.assertEqual([c[0] for c in pad.mock_calls],
                         ['stop_rumble_listener', 'reset', 'update', 'close'])
        self.assertIsNone(manager.gamepad)
        manager.stop()
        self.assertEqual(pad.close.call_count, 1)

    def test_reset_failure_does_not_prevent_close(self):
        manager = self.manager(); pad = manager.gamepad = Mock()
        pad.reset.side_effect = OSError('device gone')
        manager.stop()
        pad.update.assert_called_once(); pad.close.assert_called_once()

    def test_stop_waits_for_inflight_update_then_neutralizes(self):
        manager = self.manager(); pad = manager.gamepad = Mock()
        entered, release = threading.Event(), threading.Event()
        def block(**kwargs):
            entered.set(); release.wait(2)
        pad.left_joystick.side_effect = block
        worker = threading.Thread(target=manager.update, args=(0, 0, 0, 0, 0, 0, {}))
        worker.start(); self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=manager.stop)
        stopper.start()
        try:
            pad.close.assert_not_called()
        finally:
            release.set(); worker.join(2); stopper.join(2)
        self.assertFalse(worker.is_alive()); self.assertFalse(stopper.is_alive())
        self.assertEqual([c[0] for c in pad.mock_calls][-4:],
                         ['stop_rumble_listener', 'reset', 'update', 'close'])
        count = len(pad.mock_calls)
        manager.update(0, 0, 0, 0, 0, 0, {})
        self.assertEqual(len(pad.mock_calls), count)
