"""Exercise the production settings-initialization block without opening Tk."""
import copy
import textwrap
import types
import unittest

from _support import SRC


def initialize_saved_collections(settings):
    # Isolate the startup block between its stable section markers. This is
    # production code, not a duplicate of its implementation. Importing the
    # entire application would initialize Tk, tray backends, and named pipes.
    source = (SRC / 'app.py').read_text(encoding='utf-8')
    start = source.index('        # Ensure known_ble_devices exists')
    end = source.index('        # Propagate per-slot global settings', start)
    app = types.SimpleNamespace(slot_calibrations=[settings])
    exec(compile(textwrap.dedent(source[start:end]), 'app.py:startup-settings', 'exec'),
         {'self': app})


class SavedAssignmentTests(unittest.TestCase):
    def test_ble_and_usb_assignments_survive_repeated_startup(self):
        settings = {
            'known_ble_devices': {'HOST-UUID': {'name': 'Controller'}},
            'slot_assignments': {'ble:HOST-UUID': 2, 'ble:AA:BB:CC:DD:EE:FF': 0,
                                 'usb:serial': 1},
            'device_links': {'usb:serial': 'ble:HOST-UUID'},
        }
        expected = copy.deepcopy(settings)
        assignments = settings['slot_assignments']
        for _ in range(3):
            initialize_saved_collections(settings)
            self.assertEqual(settings, expected)
            self.assertIs(settings['slot_assignments'], assignments)

    def test_missing_collections_created_independently(self):
        settings = {}
        initialize_saved_collections(settings)
        self.assertEqual(settings, {'known_ble_devices': {}, 'slot_assignments': {},
                                    'device_links': {}})
        self.assertIsNot(settings['known_ble_devices'], settings['slot_assignments'])

    def test_existing_preferences_are_not_replaced(self):
        settings = {'slot_assignments': {'ble:HOST-UUID': 3}, 'emulation_mode': 'dsu'}
        initialize_saved_collections(settings)
        self.assertEqual(settings['slot_assignments'], {'ble:HOST-UUID': 3})
        self.assertEqual(settings['emulation_mode'], 'dsu')
