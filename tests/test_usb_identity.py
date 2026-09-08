"""Device-specific USB feedback, including two controllers on one hub."""
from pathlib import Path
import plistlib
import os
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase, skipIf
from unittest.mock import Mock, patch

from _support import load_definitions
from test_usb_resources import load_connection_manager


class UsbIdentityTests(TestCase):
    def setUp(self):
        self.module = load_connection_manager()
        self.manager = self.module.ConnectionManager(Mock(), Mock())
        self.first = Mock(bus=4, address=2, serial_number='first')
        self.second = Mock(bus=4, address=3, serial_number='second')
        self.first.is_kernel_driver_active.return_value = False
        self.second.is_kernel_driver_active.return_value = False
        self.module.usb.core.find.return_value = [self.first, self.second]
        self.manager.device = Mock()

    def bind_second(self):
        self.module.IS_MACOS = True
        self.manager.build_hid_to_usb_address_map = Mock(return_value={202: (4, 3)})
        self.manager._usb_device = self.manager._resolve_usb_device(b'DevSrvsID:202')
        self.assertIs(self.manager._usb_device, self.second)

    def test_same_hub_rumble_targets_opened_controller_not_first(self):
        self.bind_second()
        # Once bound, no rediscovery may silently switch to another device.
        self.module.usb.core.find.reset_mock()
        self.assertTrue(self.manager.send_rumble(True))
        self.first.write.assert_not_called()
        self.assertEqual(self.second.write.call_args.args[1][8], 1)
        self.module.usb.core.find.assert_not_called()

    def test_led_and_rumble_share_same_binding(self):
        self.bind_second()
        self.assertTrue(self.manager.set_player_led(3))
        self.assertEqual(self.second.write.call_args.args[1][8], 7)
        self.assertTrue(self.manager.send_rumble(False))
        self.first.write.assert_not_called()

    def test_reversed_enumeration_does_not_change_identity(self):
        self.module.usb.core.find.return_value = [self.second, self.first]
        self.bind_second()

    def test_missing_address_does_not_fall_back_to_bus(self):
        self.module.IS_MACOS = True
        self.manager.build_hid_to_usb_address_map = Mock(return_value={})
        self.assertIsNone(self.manager._resolve_usb_device(b'DevSrvsID:202'))
        self.assertFalse(self.manager.send_rumble(True))
        self.assertFalse(self.manager.set_player_led(2))
        self.first.write.assert_not_called()
        self.second.write.assert_not_called()

    def test_one_visible_usb_device_is_not_proof_of_hid_identity(self):
        self.module.usb.core.find.return_value = [self.first]
        self.assertIsNone(self.manager._resolve_usb_device(b'unknown-hid-path'))

    def test_duplicate_bus_and_address_fails_closed(self):
        self.first.address = 3
        self.module.IS_MACOS = True
        self.manager.build_hid_to_usb_address_map = Mock(return_value={202: (4, 3)})
        self.assertIsNone(self.manager._resolve_usb_device(b'DevSrvsID:202'))

    def test_unique_serial_fallback(self):
        self.module.hid.enumerate.return_value = [
            {'path': b'hid-path', 'serial_number': 'second'}]
        self.assertIs(self.manager._resolve_usb_device(b'hid-path'), self.second)
        self.module.usb.util.dispose_resources.assert_any_call(self.first)
        self.module.usb.util.dispose_resources.assert_any_call(self.second)

    def test_duplicate_serial_fails_closed(self):
        self.first.serial_number = 'second'
        self.module.hid.enumerate.return_value = [
            {'path': b'hid-path', 'serial_number': 'second'}]
        self.assertIsNone(self.manager._resolve_usb_device(b'hid-path'))

    def test_unreadable_serial_does_not_establish_uniqueness(self):
        from unittest.mock import PropertyMock
        type(self.first).serial_number = PropertyMock(side_effect=OSError('denied'))
        self.module.hid.enumerate.return_value = [
            {'path': b'hid-path', 'serial_number': 'second'}]
        self.assertIsNone(self.manager._resolve_usb_device(b'hid-path'))
        self.module.usb.util.dispose_resources.assert_called_once_with(self.first)

    def test_rumble_failure_cleans_up_bound_device(self):
        self.bind_second()
        self.second.write.side_effect = OSError('removed')
        self.assertFalse(self.manager.send_rumble(True))
        self.module.usb.util.release_interface.assert_called_once_with(self.second, 1)
        self.module.usb.util.dispose_resources.assert_called_once_with(self.second)
        self.first.write.assert_not_called()

    def test_disconnect_invalidates_binding_even_if_close_fails(self):
        self.bind_second()
        self.manager.device.close.side_effect = OSError('gone')
        self.manager.disconnect()
        self.assertIsNone(self.manager.device)
        self.assertIsNone(self.manager.device_path)
        self.assertIsNone(self.manager._usb_device)
        self.assertFalse(self.manager.send_rumble(True))

    def test_failed_hid_open_closes_candidate_and_clears_state(self):
        self.manager.device = None
        candidate = self.module.hid.device.return_value
        candidate.open_path.side_effect = OSError('cannot open')
        self.assertFalse(self.manager.init_hid_device(b'path'))
        candidate.close.assert_called_once_with()
        self.assertIsNone(self.manager.device)
        self.assertIsNone(self.manager.device_path)
        self.assertIsNone(self.manager._usb_device)

    def test_identity_failure_does_not_disable_hid_input(self):
        self.manager.device = None
        self.manager._resolve_usb_device = Mock(side_effect=OSError('ioreg failed'))
        self.assertTrue(self.manager.init_hid_device(b'path'))
        self.assertIs(self.manager.device, self.module.hid.device.return_value)
        self.assertIsNone(self.manager._usb_device)
        self.assertFalse(self.manager.send_rumble(True))

    def test_default_open_uses_enumerated_path_for_identity(self):
        self.manager.device = None
        self.module.hid.enumerate.return_value = [{'path': b'path'}]
        self.manager._resolve_usb_device = Mock(return_value=self.second)
        self.assertTrue(self.manager.init_hid_device())
        self.module.hid.device.return_value.open_path.assert_called_once_with(b'path')
        self.manager._resolve_usb_device.assert_called_once_with(b'path')
        self.assertEqual(self.manager.device_path, b'path')

    def test_reopen_requires_explicit_disconnect(self):
        original = self.manager.device
        self.assertFalse(self.manager.init_hid_device(b'other'))
        self.assertIs(self.manager.device, original)
        self.module.hid.device.assert_not_called()


class UsbRegistryTests(TestCase):
    def setUp(self):
        self.module = load_connection_manager()
        self.module.IS_MACOS = True

    def registry_device(self, address, registry_ids):
        result = {'idVendor': self.module.VENDOR_ID,
                  'idProduct': self.module.PRODUCT_ID,
                  'locationID': 4 << 24,
                  'IORegistryEntryChildren': [{'IORegistryEntryChildren': [
                      {'IORegistryEntryName': 'AppleUserUSBHostHIDDevice',
                       'IORegistryEntryID': entry_id} for entry_id in registry_ids]}]}
        if address is not None:
            result['USB Address'] = address
        return result

    def test_all_hid_interfaces_map_to_unique_bus_address(self):
        devices = [self.registry_device(2, [100, 101]),
                   self.registry_device(3, [200, 201])]
        with patch.object(self.module.subprocess, 'run',
                          return_value=SimpleNamespace(stdout=plistlib.dumps(devices))):
            self.assertEqual(self.module.ConnectionManager.build_hid_to_usb_address_map(),
                             {100: (4, 2), 101: (4, 2), 200: (4, 3), 201: (4, 3)})

    def test_unknown_address_is_omitted_not_guessed(self):
        devices = [self.registry_device(None, [100]), self.registry_device(0, [200])]
        with patch.object(self.module.subprocess, 'run',
                          return_value=SimpleNamespace(stdout=plistlib.dumps(devices))):
            self.assertEqual(self.module.ConnectionManager.build_hid_to_usb_address_map(), {})

    def test_alternate_registry_address_key(self):
        dev = self.registry_device(3, [200])
        dev['USBAddress'] = dev.pop('USB Address')
        with patch.object(self.module.subprocess, 'run',
                          return_value=SimpleNamespace(stdout=plistlib.dumps([dev]))):
            self.assertEqual(self.module.ConnectionManager.build_hid_to_usb_address_map(),
                             {200: (4, 3)})

    def test_ioreg_failure_returns_no_binding(self):
        with patch.object(self.module.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertEqual(self.module.ConnectionManager.build_hid_to_usb_address_map(), {})

    @skipIf(os.name == 'nt', 'Symlink creation requires Windows privileges')
    def test_linux_follows_hidraw_ancestry(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / 'devices' / 'usb4' / '4-2'
            interface = device / '4-2:1.0' / 'hid-interface'
            interface.mkdir(parents=True)
            for name, value in {'idVendor': '057e', 'idProduct': '2073',
                                'busnum': '4', 'devnum': '3'}.items():
                (device / name).write_text(value)
            node = root / 'class' / 'hidraw2'
            node.mkdir(parents=True)
            (node / 'device').symlink_to(interface, target_is_directory=True)

            def test_path(value):
                return root / 'class' if value == '/sys/class/hidraw' else Path(value)

            with patch.object(self.module, 'Path', side_effect=test_path):
                self.assertEqual(self.module.ConnectionManager._linux_usb_address(b'/dev/hidraw2'),
                                 (4, 3))
                (device / 'idProduct').write_text('0000')
                self.assertIsNone(self.module.ConnectionManager._linux_usb_address(b'/dev/hidraw2'))


class SlotLedRoutingTests(TestCase):
    def test_app_routes_each_usb_slot_through_its_connection_manager(self):
        namespace = load_definitions('app.py', ['_sync_player_leds'],
                                     class_name='GCControllerEnabler')
        slots = [SimpleNamespace(is_connected=True, connection_mode='usb', conn_mgr=Mock()),
                 SimpleNamespace(is_connected=True, connection_mode='ble', conn_mgr=Mock()),
                 SimpleNamespace(is_connected=False, connection_mode='usb', conn_mgr=Mock()),
                 SimpleNamespace(is_connected=True, connection_mode='usb', conn_mgr=Mock())]
        namespace['_sync_player_leds'](SimpleNamespace(slots=slots))
        slots[0].conn_mgr.set_player_led.assert_called_once_with(1)
        slots[1].conn_mgr.set_player_led.assert_not_called()
        slots[2].conn_mgr.set_player_led.assert_not_called()
        slots[3].conn_mgr.set_player_led.assert_called_once_with(4)
