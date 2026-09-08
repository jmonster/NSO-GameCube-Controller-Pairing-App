"""Headless output creation must not occupy the event-processing thread."""
import threading
from unittest import TestCase
from unittest.mock import Mock

from _support import fake_module, load_module


class BackgroundOutputTests(TestCase):
    def setUp(self):
        self.factory = Mock(return_value=Mock())
        self.module = load_module('emulation_manager.py', {
            'virtual_gamepad': fake_module(VirtualGamepad=object, create_gamepad=self.factory),
            'controller_constants': fake_module(BUTTON_MAPPING={}, DOLPHIN_BUTTON_MAPPING={}),
            'calibration': fake_module(CalibrationManager=object),
        })
        self.manager = self.module.EmulationManager(Mock())
        self.results = []
        self.operation = self.module.OutputStart(self.manager, 'dolphin_pipe', 2, None,
                                                 lambda op, error: self.results.append((op, error)))

    def test_stop_before_launch_cancels_queued_factory(self):
        self.operation.stop()
        self.operation.launch(); self.operation.thread.join(2)
        self.factory.assert_not_called()
        self.assertFalse(self.manager.is_emulating)
        self.assertIs(self.results[0][0], self.operation)
        self.assertIsNotNone(self.results[0][1])

    def test_waiting_dolphin_factory_does_not_block_owner_and_disposes_after_stop(self):
        entered, release = threading.Event(), threading.Event()
        pad = Mock()
        def factory(*args, **kwargs):
            entered.set(); release.wait(2); return pad
        self.factory.side_effect = factory
        self.operation.launch()
        self.assertTrue(entered.wait(1))
        try:
            # Owning event loop remains free to process a disconnect/EOF.
            self.operation.stop()
            self.assertTrue(self.operation.cancel_event.is_set())
            self.assertFalse(self.manager.is_emulating)
        finally:
            release.set(); self.operation.thread.join(2)
        self.assertFalse(self.operation.thread.is_alive())
        self.assertIsNone(self.manager.gamepad)
        pad.close.assert_called_once()
        self.assertIs(self.results[0][0], self.operation)

    def test_current_success_retains_gamepad_and_tags_completion_owner(self):
        self.operation.launch(); self.operation.thread.join(2)
        self.assertTrue(self.manager.is_emulating)
        self.assertEqual(self.results, [(self.operation, None)])
        self.operation.stop()
        self.assertFalse(self.manager.is_emulating)

    def test_failed_factory_notifies_without_publishing_output(self):
        self.factory.side_effect = RuntimeError('no output driver')
        self.operation.launch(); self.operation.thread.join(2)
        self.assertEqual(self.results, [(self.operation, 'no output driver')])
        self.assertIsNone(self.manager.gamepad)
        self.assertFalse(self.manager.is_starting)
