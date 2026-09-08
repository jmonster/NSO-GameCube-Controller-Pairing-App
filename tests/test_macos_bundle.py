import ast
import unittest

from _support import ROOT


class MacOSBundleTests(unittest.TestCase):
    def test_bluetooth_privacy_description_in_app_bundle(self):
        tree = ast.parse((ROOT / 'gc_controller_enabler.spec').read_text(encoding='utf-8'))
        bundles = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name) and n.func.id == 'BUNDLE']
        self.assertTrue(bundles, 'Expected a macOS BUNDLE build target')
        for bundle in bundles:
            values = next(k.value for k in bundle.keywords if k.arg == 'info_plist')
            plist = ast.literal_eval(values)
            description = plist.get('NSBluetoothAlwaysUsageDescription', '')
            self.assertTrue(description.strip(), 'Packaged Bluetooth access needs a purpose string')
            self.assertIn('controller', description.lower())
            self.assertFalse(plist['LSUIElement'], 'Keep the Dock-based window restoration path')
