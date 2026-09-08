"""Factory and UI completion races use events, not sleep-based ordering."""
import errno
import logging
import threading
import types
import unittest
from unittest.mock import Mock

from _support import fake_module, load_definitions, load_module


class EmulationStartTests(unittest.TestCase):
    def setUp(self):
        self.factory = Mock()
        module = load_module('emulation_manager.py', {
            'virtual_gamepad': fake_module(VirtualGamepad=object, create_gamepad=self.factory),
            'controller_constants': fake_module(BUTTON_MAPPING={}, DOLPHIN_BUTTON_MAPPING={}),
            'calibration': fake_module(CalibrationManager=object),
        })
        self.manager = module.EmulationManager(Mock())
        self.pad = Mock()
        self.factory.return_value = self.pad

    def test_stop_cancels_factory_and_disposes_late_result(self):
        entered, release = threading.Event(), threading.Event()
        results = []
        def factory(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            self.assertTrue(kwargs['cancel_event'].is_set())
            return self.pad
        self.factory.side_effect = factory
        def start():
            try:
                self.manager.start('dolphin_pipe')
            except Exception as exc:
                results.append(exc)
        worker = threading.Thread(target=start)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.manager.stop()
            self.assertFalse(self.manager.is_emulating)
            self.assertTrue(self.manager.is_starting)
            with self.assertRaises(OSError) as error:
                self.manager.start()
            self.assertEqual(error.exception.errno, errno.EBUSY)
        finally:
            release.set(); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].errno, errno.ECANCELED)
        self.assertIsNone(self.manager.gamepad)
        self.assertFalse(self.manager.is_starting)
        self.pad.close.assert_called_once()
        self.manager.stop()
        self.pad.close.assert_called_once()

    def test_cancelled_queued_start_never_calls_factory(self):
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(OSError) as error:
            self.manager.start(cancel_event=cancel)
        self.assertEqual(error.exception.errno, errno.ECANCELED)
        self.factory.assert_not_called()

    def test_factory_failure_releases_reservation(self):
        self.factory.side_effect = OSError('no driver')
        with self.assertRaises(OSError): self.manager.start()
        self.assertFalse(self.manager.is_starting)
        self.factory.side_effect = None
        self.manager.start()
        self.assertIs(self.manager.gamepad, self.pad)
        self.manager.stop()

    def test_registration_failure_disposes_created_device(self):
        self.pad.set_rumble_callback.side_effect = RuntimeError('registration failed')
        with self.assertRaises(RuntimeError):
            self.manager.start(rumble_callback=Mock())
        self.pad.close.assert_called_once()
        self.assertFalse(self.manager.is_emulating)
        self.assertIsNone(self.manager.gamepad)
        self.assertFalse(self.manager.is_starting)

    def test_duplicate_start_does_not_replace_active_pad(self):
        self.manager.start()
        with self.assertRaises(OSError) as error: self.manager.start()
        self.assertEqual(error.exception.errno, errno.EBUSY)
        self.factory.assert_called_once()
        self.assertIs(self.manager.gamepad, self.pad)
        self.manager.stop()

    def test_rumble_from_retired_pad_is_ignored_after_restart(self):
        callback = Mock()
        self.manager.start(rumble_callback=callback)
        old_rumble = self.pad.set_rumble_callback.call_args.args[0]
        old_rumble(100, 0)
        callback.assert_called_once_with(100, 0)
        callback.reset_mock()
        self.manager.stop()
        self.factory.return_value = Mock()
        self.manager.start(rumble_callback=callback)
        old_rumble(255, 255)
        callback.assert_not_called()
        self.manager.stop()

    def test_stop_during_callback_registration_closes_result(self):
        self.pad.set_rumble_callback.side_effect = lambda callback: self.manager.stop()
        with self.assertRaises(OSError) as error:
            self.manager.start(rumble_callback=Mock())
        self.assertEqual(error.exception.errno, errno.ECANCELED)
        self.pad.close.assert_called_once()
        self.assertIsNone(self.manager.gamepad)


class PipeCallbackTests(unittest.TestCase):
    def setUp(self):
        methods = load_definitions('app.py', {'_on_pipe_connected', '_on_pipe_failed'},
            {'errno': errno, 't': lambda key, **kwargs: key}, class_name='GCControllerEnabler')
        self.token = threading.Event()
        self.slot = types.SimpleNamespace(_pipe_cancel=self.token, is_connected=True,
                                         emu_mgr=Mock(is_emulating=True), stop_emulation=Mock())
        self.app = types.SimpleNamespace(slots=[self.slot], ui=Mock(), _messagebox=Mock())
        for name in ('_on_pipe_connected', '_on_pipe_failed'):
            setattr(self.app, name, types.MethodType(methods[name], self.app))

    def test_old_failure_does_not_stop_replacement(self):
        self.slot._pipe_cancel = threading.Event()
        self.app._on_pipe_failed(0, self.slot, self.token, RuntimeError('old'))
        self.slot.stop_emulation.assert_not_called()
        self.app.ui.update_tab_status.assert_not_called()
        self.app._messagebox.showerror.assert_not_called()

    def test_old_success_does_not_overwrite_new_pending_attempt(self):
        replacement = self.slot._pipe_cancel = threading.Event()
        self.app._on_pipe_connected(0, self.slot, self.token)
        self.assertIs(self.slot._pipe_cancel, replacement)
        self.app.ui.update_tab_status.assert_not_called()

    def test_moved_slot_and_cancelled_callback_are_ignored(self):
        self.app._on_pipe_connected(0, object(), self.token)
        self.token.set()
        self.app._on_pipe_connected(0, self.slot, self.token)
        self.app.ui.update_tab_status.assert_not_called()

    def test_current_success_updates_ui_only_when_emulating(self):
        self.slot.emu_mgr.is_emulating = False
        self.app._on_pipe_connected(0, self.slot, self.token)
        self.app.ui.update_tab_status.assert_not_called()
        self.slot.emu_mgr.is_emulating = True
        self.app._on_pipe_connected(0, self.slot, self.token)
        self.assertIsNone(self.slot._pipe_cancel)
        self.app.ui.update_tab_status.assert_called_once()

    def test_slot_stop_cancels_worker_before_it_enters_manager(self):
        method = load_definitions('controller_slot.py', {'stop_emulation'},
                                  class_name='ControllerSlot')['stop_emulation']
        method(self.slot)
        self.assertTrue(self.token.is_set())
        self.assertIsNone(self.slot._pipe_cancel)
        self.slot.emu_mgr.stop.assert_called_once()
