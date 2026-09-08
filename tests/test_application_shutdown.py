import logging
from types import SimpleNamespace, MethodType
from unittest import TestCase
from unittest.mock import Mock

from _support import load_definitions


class ShutdownTests(TestCase):
    def make_app(self):
        self.transport_class = Mock()
        method = load_definitions('app.py', {'_actual_quit'},
                                  {'logger': logging.getLogger('shutdown-test'),
                                   'CommandTransport': self.transport_class},
                                  class_name='GCControllerEnabler')['_actual_quit']
        slots = [SimpleNamespace(stop_emulation=Mock(), input_proc=Mock(), conn_mgr=Mock()) for _ in range(2)]
        app = SimpleNamespace(_ui_dispatcher=Mock(), _stop_usb_hotplug=Mock(),
                              _stop_auto_scan=Mock(), _cleanup_tray=Mock(), slots=slots,
                              _reset_rumble=Mock(), _ble_commands=Mock(), _send_ble_cmd=Mock(),
                              _cleanup_ble=Mock(), root=Mock(), _messagebox=Mock())
        app._actual_quit = MethodType(method, app)
        return app

    def test_failures_do_not_skip_remaining_outputs_processes_or_window(self):
        app = self.make_app()
        app._stop_auto_scan.side_effect = RuntimeError('scan failed')
        app.slots[0].input_proc.stop.side_effect = RuntimeError('reader failed')
        with self.assertLogs('shutdown-test', 'ERROR'):
            app._actual_quit()
        for slot in app.slots:
            slot.stop_emulation.assert_called_once()
            slot.conn_mgr.disconnect.assert_called_once()
        app._ble_commands.close.assert_called_once_with(wait=True)
        self.transport_class.close_all.assert_called_once()
        app.root.destroy.assert_called_once()

    def test_neutralization_precedes_reader_wait_and_overload_dialog(self):
        app = self.make_app(); order = []
        app.slots[0].stop_emulation.side_effect = lambda: order.append('neutral')
        app.slots[0].input_proc.stop.side_effect = lambda: order.append('reader')
        app._messagebox.showerror.side_effect = lambda *args: order.append('dialog')
        app.root.destroy.side_effect = lambda: order.append('destroy')
        app._actual_quit(reason='overloaded')
        self.assertEqual(order, ['neutral', 'reader', 'dialog', 'destroy'])

    def test_reentrant_shutdown_is_idempotent_and_dialog_failure_still_destroys_root(self):
        app = self.make_app()
        app.slots[0].stop_emulation.side_effect = app._actual_quit
        app._messagebox.showerror.side_effect = RuntimeError('dialog failed')
        with self.assertRaises(RuntimeError): app._actual_quit(reason='overloaded')
        app.root.destroy.assert_called_once()
        app._actual_quit()
        app.slots[0].stop_emulation.assert_called_once()
