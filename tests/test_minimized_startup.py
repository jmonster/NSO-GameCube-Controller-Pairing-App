import types
import unittest
from unittest.mock import Mock

from _support import load_definitions


class MinimizedStartupTests(unittest.TestCase):
    def run_app(self, minimized, tray_available, tray):
        run = load_definitions('app.py', {'run'}, {'_TRAY_AVAILABLE': tray_available},
                               class_name='GCControllerEnabler')['run']
        app = types.SimpleNamespace(_start_minimized=minimized, _tray_icon=tray, root=Mock())
        run(app)
        app.root.mainloop.assert_called_once_with()
        return app

    def test_no_tray_uses_native_minimization(self):
        app = self.run_app(True, False, None)
        app.root.iconify.assert_called_once_with()
        app.root.withdraw.assert_not_called()

    def test_failed_tray_creation_remains_recoverable(self):
        app = self.run_app(True, True, None)
        app.root.iconify.assert_called_once_with()
        app.root.withdraw.assert_not_called()

    def test_working_tray_keeps_existing_behavior(self):
        tray = types.SimpleNamespace(visible=False)
        app = self.run_app(True, True, tray)
        app.root.withdraw.assert_called_once_with()
        app.root.iconify.assert_not_called()
        self.assertTrue(tray.visible)

    def test_normal_startup_does_not_hide_window(self):
        app = self.run_app(False, False, None)
        app.root.withdraw.assert_not_called()
        app.root.iconify.assert_not_called()
