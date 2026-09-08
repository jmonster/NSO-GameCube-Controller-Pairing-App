"""USB slot migration must preserve the handle's feedback identity."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from _support import load_definitions
from test_usb_resources import load_connection_manager


class UsbMigrationTests(TestCase):
    def setUp(self):
        module = load_connection_manager()
        self.source = module.ConnectionManager(Mock(), Mock())
        self.target = module.ConnectionManager(Mock(), Mock())
        self.handle, self.peer = Mock(), Mock()
        self.source.device = self.handle
        self.source.device_path = b'DevSrvsID:202'
        self.source._usb_device = self.peer

    def test_transfer_moves_handle_path_and_feedback_without_closing(self):
        self.target._usb_device = Mock()  # A retired session must not survive.
        self.assertTrue(self.source.transfer_to(self.target))
        self.assertIs(self.target.device, self.handle)
        self.assertEqual(self.target.device_path, b'DevSrvsID:202')
        self.assertIs(self.target._usb_device, self.peer)
        self.assertIsNone(self.source.device)
        self.assertIsNone(self.source.device_path)
        self.assertIsNone(self.source._usb_device)
        self.handle.close.assert_not_called()
        self.assertFalse(self.source.send_rumble(True))
        self.assertTrue(self.target.send_rumble(True))
        self.peer.write.assert_called_once()

    def test_transfer_never_overwrites_a_live_destination(self):
        existing = self.target.device = Mock()
        self.assertFalse(self.source.transfer_to(self.target))
        self.assertIs(self.source.device, self.handle)
        self.assertIs(self.target.device, existing)
        self.assertIs(self.source._usb_device, self.peer)

    def test_self_or_empty_transfer_does_not_modify_session(self):
        self.assertFalse(self.source.transfer_to(self.source))
        self.assertFalse(self.target.transfer_to(self.source))
        self.assertIs(self.source.device, self.handle)
        self.assertIs(self.source._usb_device, self.peer)

    def migrate(self, occupied=False):
        funcs = load_definitions('app.py', ['_try_cross_transport_migration'],
                                {'time': SimpleNamespace(monotonic=lambda: 10),
                                 'logger': Mock(), 't': lambda key: key},
                                class_name='GCControllerEnabler')
        source = SimpleNamespace(conn_mgr=self.source, device_path=self.source.device_path,
                                 device_identity='usb:202', is_connected=True,
                                 connection_mode='usb', input_proc=Mock(), emu_mgr=Mock())
        source.emu_mgr.is_emulating = False
        target = SimpleNamespace(conn_mgr=self.target, device_path=None,
                                 device_identity='ble:previous', is_connected=False,
                                 connection_mode='ble', input_proc=Mock())
        if occupied:
            self.target.device = Mock()
        app = SimpleNamespace(slots=[target, source], ui=Mock(slots=[Mock(), Mock()]),
                              _recent_usb_hotplug={1: (9, 'usb:202')},
                              slot_calibrations=[{}, {}], _save_slot_assignment=Mock(),
                              _sync_player_leds=Mock(), _link_devices=Mock(),
                              toggle_emulation=Mock())
        funcs['_try_cross_transport_migration'](app, 0)
        return app, source, target

    def test_app_migration_preserves_feedback_and_releases_claimed_path(self):
        app, source, target = self.migrate()
        self.assertIs(self.target._usb_device, self.peer)
        self.assertEqual(self.target.device_path, target.device_path)
        self.assertIsNone(self.source.device_path)
        self.assertIsNone(self.source._usb_device)
        target.input_proc.start.assert_called_once()
        app._sync_player_leds.assert_called_once()
        self.assertTrue(self.target.set_player_led(1))
        self.peer.write.assert_called_once()
        self.handle.close.assert_not_called()

    def test_app_keeps_source_usable_when_destination_is_occupied(self):
        app, source, target = self.migrate(occupied=True)
        self.assertIs(self.source.device, self.handle)
        self.assertEqual(source.device_path, b'DevSrvsID:202')
        source.input_proc.start.assert_called_once()
        target.input_proc.start.assert_not_called()
        app.ui.reset_slot_ui.assert_not_called()
        app._sync_player_leds.assert_not_called()
