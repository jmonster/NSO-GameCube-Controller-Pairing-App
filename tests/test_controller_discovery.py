"""The picker and transports must agree on Bluetooth versus USB identifiers."""
import sys
import unittest
from unittest.mock import patch
from _support import fake_module, load_module


class ControllerDiscoveryTests(unittest.TestCase):
    def test_picker_uses_only_nintendos_assigned_bluetooth_company_id(self):
        with patch.dict(sys.modules, {'customtkinter': fake_module()}):
            module = load_module('ui_ble_scan_wizard.py', {
                'ui_theme': fake_module(), 'i18n': fake_module(t=lambda key: key)})
        for company, expected in (('1363', True), ('894', False), ('1406', False)):
            with self.subTest(company=company):
                self.assertEqual(module._is_likely_controller({
                    'manufacturer_data': {company: '01'}}), expected)
        for address in ('E0:EF:BF:00:00:01', '94:8E:6D:00:00:01'):
            self.assertTrue(module._is_likely_controller({'address': address}))

    def test_late_scan_response_promotes_candidate_and_updates_do_not_reset_timer(self):
        import types
        from unittest.mock import Mock
        from _support import load_definitions
        with patch.dict(sys.modules, {'customtkinter': fake_module()}):
            module = load_module('ui_ble_scan_wizard.py', {
                'ui_theme': fake_module(), 'i18n': fake_module(t=lambda key: key)})
        method = module.BLEControllerScanDialog.add_device
        dialog = types.SimpleNamespace(_closed=False, _exclude=set(), _seen_addresses=set(),
            _devices_by_address={}, _controllers=[], _other_devices=[],
            _update_tree=Mock(), _reset_auto_connect_timer=Mock())
        method(dialog, {'address': 'aa-bb', 'name': '', 'rssi': -70})
        self.assertFalse(dialog._controllers)
        method(dialog, {'address': 'aa-bb', 'manufacturer_data': {'1363': '01'}, 'rssi': -60})
        self.assertEqual(len(dialog._controllers), 1)
        self.assertFalse(dialog._other_devices)
        method(dialog, {'address': 'aa-bb', 'name': 'Nintendo', 'rssi': -55})
        method(dialog, {'address': 'aa-bb', 'name': '', 'manufacturer_data': {}, 'rssi': -50})
        self.assertEqual(dialog._controllers[0]['name'], 'Nintendo')
        self.assertEqual(dialog._controllers[0]['manufacturer_data'], {'1363': '01'})
        self.assertEqual(dialog._controllers[0]['rssi'], -50)
        dialog._reset_auto_connect_timer.assert_called_once()
        self.assertEqual(dialog._update_tree.call_count, 3)
        dialog._closed = True
        method(dialog, {'address': 'new', 'name': 'Nintendo'})
        self.assertEqual(len(dialog._controllers), 1)

    def test_tree_refresh_preserves_selected_controller_after_rssi_sort(self):
        import types
        from unittest.mock import Mock
        from _support import load_definitions
        class Tree:
            def __init__(self):
                self.rows = {'old': ('chosen', 'chosen-address', '-80 dBm')}
                self.selected = 'old'
                self.next_id = 0
            def selection(self): return [self.selected]
            def item(self, item, option): return self.rows[item]
            def get_children(self): return list(self.rows)
            def delete(self, item): self.rows.pop(item)
            def insert(self, *args, values):
                self.next_id += 1
                key = str(self.next_id)
                self.rows[key] = values
                return key
            def selection_set(self, item): self.selected = item
        dialog = types.SimpleNamespace(_controllers=[
            {'name': 'other', 'address': 'other-address', 'rssi': -20},
            {'name': 'chosen', 'address': 'chosen-address', 'rssi': -80}],
            _status_label=None, _tree_initialized=True, _tree=Tree(), _ensure_connect_btn=Mock())
        method = load_definitions('ui_ble_scan_wizard.py', {'_update_tree'},
            {'tk': types.SimpleNamespace(END='end'), 't': lambda key: key},
            class_name='BLEControllerScanDialog')['_update_tree']
        method(dialog)
        self.assertEqual(dialog._tree.rows[dialog._tree.selected][1], 'chosen-address')
