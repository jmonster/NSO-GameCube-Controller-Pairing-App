"""Selection-scoped initialization, session migration and headless cancellation."""
import ast
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from _support import SRC, load_definitions
from test_usb_resources import load_connection_manager


class TargetedInitializationTests(TestCase):
    def setUp(self):
        self.module = load_connection_manager()
        self.manager = self.module.ConnectionManager(Mock(), Mock())
        self.first = Mock(bus=4, address=2, serial_number='first')
        self.second = Mock(bus=4, address=3, serial_number='second')
        self.module.usb.core.find.return_value = [self.first, self.second]
        self.module.hid.enumerate.return_value = [
            {'path': b'first', 'serial_number': 'first'},
            {'path': b'second', 'serial_number': 'second'}]

    def test_connect_initializes_only_selected_controller_on_shared_hub(self):
        self.assertTrue(self.manager.connect_hid(b'second'))
        self.first.write.assert_not_called()
        self.assertEqual(self.second.write.call_count, 2)
        self.module.hid.device.return_value.open_path.assert_called_once_with(b'second')
        self.assertIs(self.manager._usb_device, self.second)

    def test_reversed_enumeration_does_not_change_initialization_target(self):
        self.module.usb.core.find.return_value = [self.second, self.first]
        self.test_connect_initializes_only_selected_controller_on_shared_hub()

    def test_existing_session_is_not_reinitialized(self):
        self.manager.device = Mock()
        self.assertFalse(self.manager.connect_hid(b'second'))
        self.module.usb.core.find.assert_not_called()
        self.first.write.assert_not_called(); self.second.write.assert_not_called()

    def test_ambiguous_peer_leaves_hid_available_but_never_initializes_another(self):
        self.module.hid.enumerate.return_value = []
        self.assertTrue(self.manager.connect_hid(b'unknown'))
        self.first.write.assert_not_called(); self.second.write.assert_not_called()
        self.assertIsNone(self.manager._usb_device)

    def test_unavailable_libusb_does_not_break_native_hid_fallback(self):
        self.manager._resolve_usb_device = Mock(side_effect=RuntimeError('no backend'))
        self.assertTrue(self.manager.connect_hid(b'second'))
        self.first.write.assert_not_called(); self.second.write.assert_not_called()

    def test_initialization_failure_releases_target_and_still_tries_hid(self):
        self.second.write.side_effect = OSError('permission denied')
        self.assertTrue(self.manager.connect_hid(b'second'))
        self.module.usb.util.release_interface.assert_called_once_with(self.second, 1)
        self.first.write.assert_not_called()

    def test_compatibility_entry_point_rejects_mismatched_devices(self):
        self.assertFalse(self.manager.connect(usb_device=self.first, device_path=b'second'))
        self.first.write.assert_not_called(); self.second.write.assert_not_called()
        self.module.hid.device.assert_not_called()

    def test_no_target_can_never_initialize_first_match(self):
        self.assertFalse(self.manager.initialize_via_usb())
        self.assertFalse(self.manager.connect())
        self.module.usb.core.find.assert_not_called()
        self.first.write.assert_not_called(); self.second.write.assert_not_called()

    def test_transfer_moves_hid_path_and_feedback_binding_atomically(self):
        self.assertTrue(self.manager.connect_hid(b'second'))
        handle = self.manager.device
        other = self.module.ConnectionManager(Mock(), Mock())
        self.assertTrue(self.manager.transfer_to(other))
        self.assertIsNone(self.manager.device)
        self.assertIsNone(self.manager.device_path)
        self.assertIsNone(self.manager._usb_device)
        self.assertIs(other.device, handle)
        self.assertEqual(other.device_path, b'second')
        self.assertIs(other._usb_device, self.second)
        handle.close.assert_not_called()
        self.assertFalse(self.manager.send_rumble(True))
        self.assertTrue(other.send_rumble(True))
        self.first.write.assert_not_called()

    def test_transfer_cannot_overwrite_a_live_session(self):
        self.manager.device = Mock()
        other = self.module.ConnectionManager(Mock(), Mock()); other.device = Mock()
        original = other.device
        self.assertFalse(self.manager.transfer_to(other))
        self.assertIs(other.device, original)
        self.assertIsNotNone(self.manager.device)
        self.assertFalse(self.manager.transfer_to(self.manager))


class ApplicationInitializationTests(TestCase):
    def test_application_cannot_bulk_initialize_unselected_usb_devices(self):
        tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
        calls = [node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn('initialize_via_usb', calls)
        self.assertNotIn('enumerate_usb_devices', calls)
        self.assertEqual(calls.count('connect_hid'), 6)

    def test_unknown_transport_pair_is_not_linked_by_connection_timing(self):
        method = load_definitions('app.py', {'_try_cross_transport_migration'},
                                  {'time': time}, class_name='GCControllerEnabler')['_try_cross_transport_migration']
        app = SimpleNamespace(slots=[SimpleNamespace(device_identity='ble:first', is_connected=False)],
                              slot_calibrations=[{'device_links': {}}],
                              _recent_usb_hotplug={1: (time.monotonic(), 'usb:unrelated')})
        method(app, 0)  # No UI access, handle movement or implicit link.
        self.assertEqual(app.slot_calibrations[0]['device_links'], {})
        self.assertIn(1, app._recent_usb_hotplug)

    def test_every_headless_factory_receives_shutdown_cancellation(self):
        tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
        func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_headless')
        calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == 'start'
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == 'emu_mgr']
        self.assertEqual(len(calls), 3)
        for call in calls:
            cancel = next((k.value for k in call.keywords if k.arg == 'cancel_event'), None)
            self.assertIsInstance(cancel, ast.Name)
            self.assertEqual(cancel.id, 'stop_event')
