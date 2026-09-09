"""BLE connection handoffs, modal dialogs and delayed callbacks retain ownership."""
import sys
from unittest import TestCase
from unittest.mock import Mock, patch

from _support import fake_module, load_definitions
from test_ble_helper_loss import gui, ipc, process, production_namespace


class DialogOwnershipTests(TestCase):
    def check_dialog(self, method, module, class_name):
        app, old, pad = gui()
        app._send_ble_cmd = Mock()
        replacement_callback = Mock()
        callbacks = {}

        def replace_helper():
            old.end('lost while dialog was open')
            app._ble_session = ipc.HelperSession(process())
            app._ble_pair_mode[0] = 'replacement'
            app._scan_stream_callback[0] = replacement_callback
            return 'old dialog selection'

        def dialog(*args, **kwargs):
            callbacks.update(kwargs)
            return Mock(show=Mock(side_effect=replace_helper))

        namespace = production_namespace() | {'__package__': '_ble_dialog_test'}
        functions = load_definitions('app.py', [method], namespace,
                                     class_name='GCControllerEnabler')
        package = fake_module(__path__=[])
        with patch.dict(sys.modules, {
                '_ble_dialog_test': package,
                '_ble_dialog_test.' + module: fake_module(**{class_name: dialog})}):
            args = [{'address': 'old'}] if method == '_on_devices_found' else []
            functions[method](app, 0, args)
            # A dismissed old wizard must not clear the replacement's callback.
            if 'on_stop_scan' in callbacks:
                callbacks['on_stop_scan']()
        self.assertEqual(app._ble_pair_mode[0], 'replacement')
        self.assertIs(app._scan_stream_callback[0], replacement_callback)
        app._send_ble_cmd.assert_not_called()

    def test_picker_return_cannot_connect_using_replacement_helper(self):
        self.check_dialog('_on_devices_found', 'ui_ble_dialog', 'BLEDevicePickerDialog')

    def test_wizard_return_and_stop_cannot_change_replacement_state(self):
        self.check_dialog('_show_controller_scan', 'ui_ble_scan_wizard', 'BLEControllerScanDialog')


class CallbackOwnershipTests(TestCase):
    def bind(self, app, name, **namespace):
        from types import MethodType
        functions = load_definitions('app.py', [name], production_namespace() | namespace,
                                     class_name='GCControllerEnabler')
        setattr(app, name, MethodType(functions[name], app))

    def test_reconnected_empty_slot_is_owned_as_ble_for_loss_cleanup(self):
        app, owner, pad = gui()
        slot = app.slots[0]
        slot.is_connected = False
        slot.connection_mode = 'usb'  # Empty target's default after slot reassignment.
        slot.reconnect_was_emulating = False
        app._select_slot_tab = Mock()
        self.bind(app, '_on_reconnect_complete', make_ble_device_identity=lambda mac: mac)
        app._on_reconnect_complete(0, 'test')
        self.assertEqual(slot.connection_mode, 'ble')
        app._ble_event_reader()
        app._ui_poll()
        self.assertFalse(pad.buttons)

    def test_delayed_usb_scan_from_old_ble_disconnect_does_not_run_again(self):
        app, owner, pad = gui()
        app.slots[0].is_connected = False
        app._last_seen_usb_paths = set()
        connection = Mock(enumerate_devices=Mock(return_value=[]))
        self.bind(app, '_start_rapid_usb_scan', ConnectionManager=connection)
        app._start_rapid_usb_scan(0)
        owner.end('lost')
        app._ble_session = ipc.HelperSession(process())
        app.root.drain()
        connection.enumerate_devices.assert_called_once()

    def test_delayed_rumble_test_stop_cannot_clear_replacement_rumble(self):
        app, owner, pad = gui()
        self.bind(app, 'test_rumble')
        app.test_rumble(0)
        app._sync_rumble_output.reset_mock()
        owner.end('lost')
        app._ble_session = app.slots[0].ble_session = ipc.HelperSession(process())
        app.slots[0].rumble_desired = 0.75
        app.root.drain()
        self.assertEqual(app.slots[0].rumble_desired, 0.75)
        app._sync_rumble_output.assert_not_called()
