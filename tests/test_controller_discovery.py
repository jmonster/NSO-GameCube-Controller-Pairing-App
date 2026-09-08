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
