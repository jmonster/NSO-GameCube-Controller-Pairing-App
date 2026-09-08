import copy
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

from _support import load_module

schema = load_module('settings_schema.py')
manager_module = load_module('settings_manager.py')


class SettingsSchemaTests(unittest.TestCase):
    def document(self, value):
        return schema.decode_settings(json.dumps({'version': 4, 'global': value}).encode())

    def test_globals_accept_typed_bounds_and_supported_modes(self):
        for mode in ('xbox360', 'dolphin_pipe', 'dsu'):
            value = {'emulation_mode': mode, 'auto_scan_ble': True,
                     'rumble_intensity': 1.0, 'stick_deadzone': 0.0}
            self.assertEqual(self.document(value), value)

    def test_invalid_global_values_are_rejected(self):
        invalid = {'stick_deadzone': [-0.1, 1, 2, True, '0.1', None, float('inf')],
                   'rumble_intensity': [-1, 1.1, False, '1', float('nan')],
                   'emulation_mode': ['', [], {}, 'other', 1],
                   'auto_connect': [0, 1, 'false', None, []]}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.document({key: value})

    def test_slot_assignments_reject_bool_strings_and_out_of_range(self):
        for index in (True, False, -1, 4, 1.0, '1', None):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.document({'slot_assignments': {'ble:first': index}})
        self.assertEqual(self.document({'slot_assignments': {'ble:first': 3}})['slot_assignments'],
                         {'ble:first': 3})

    def test_links_allow_reciprocal_or_legacy_one_way_pairs_but_not_ambiguity(self):
        for value in ({'a': 'b'}, {'a': 'b', 'b': 'a'}):
            self.assertEqual(self.document({'device_links': value})['device_links'], value)
        for value in ({'a': 'a'}, {'a': 'b', 'b': 'c'}, {'a': 'c', 'b': 'c'}, {'': 'a'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.document({'device_links': value})

    def test_nested_calibration_shape_and_ranges_are_checked(self):
        invalid = {'stick_left_octagon': [[], [[0, 0]] * 7, [[0, 0, 0]] * 8,
                                         [[1.1, 0]] * 8, [['0', 0]] * 8],
                   'stick_left_center_x': [-1, 4096, True, '2048', 10**500],
                   'stick_left_range_x': [0, -1, 4096, None, float('nan')],
                   'trigger_left_base': [-1, 256, True, '0']}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    self.document({'known_ble_devices': {'first': {key: value}}})

    def test_valid_calibration_preserves_explicit_trigger_force_and_fresh_arrays(self):
        points = [[math.cos(i * math.pi / 4), math.sin(i * math.pi / 4)] for i in range(8)]
        calibration = {'stick_left_octagon': points, 'stick_right_octagon': None,
                       'stick_left_center_x': 2048.5, 'stick_left_range_x': 0.5,
                       'trigger_left_base': 100, 'trigger_left_bump': 100, 'trigger_left_max': 99}
        original = copy.deepcopy(calibration)
        output = schema.validate_global({'known_ble_devices': {'abc': calibration}})
        self.assertEqual(output['known_ble_devices']['ABC'], calibration)
        output['known_ble_devices']['ABC']['stick_left_octagon'][0][0] = 0
        self.assertEqual(calibration, original)

    def test_case_collisions_cannot_silently_replace_controller_calibration(self):
        with self.assertRaises(ValueError):
            self.document({'known_ble_devices': {'abc': {'trigger_left_base': 10},
                                                'ABC': {'trigger_left_base': 20}}})

    def test_duplicate_json_keys_nonfinite_and_overflow_are_rejected(self):
        for payload in (b'{"version":4,"version":1}', b'{"version":NaN}',
                        b'{"version":4,"global":{"ignored":1e999}}',
                        b'{"version":4,"global":{"auto_connect":true,"auto_connect":false}}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                schema.decode_settings(payload)

    def test_root_version_container_and_identifier_limits(self):
        for value in ([], None, 'string', {'version': True}, {'version': 5}, {'version': 0},
                      {'version': '4'}, {'version': 4, 'global': []},
                      {'version': 4, 'global': {'known_ble_devices': {'bad\n': {}}}},
                      {'version': 4, 'global': {'known_ble_devices': {str(i): {} for i in range(257)}}}):
            with self.subTest(value=str(value)[:70]), self.assertRaises(ValueError):
                schema.decode_settings(json.dumps(value).encode())
        with self.assertRaises(ValueError): schema.decode_settings(b' ' * (schema.MAX_SETTINGS_BYTES + 1))

    def test_all_supported_legacy_versions_normalize_without_mutation(self):
        v1 = {'bump_100_percent': True, 'auto_connect': False, 'left_base': 10}
        self.assertEqual(schema.normalize_settings(v1),
                         {'auto_connect': False, 'trigger_bump_100_percent': True})
        v2 = {'version': 2, 'global': {'known_ble_addresses': ['second'], 'auto_connect': True},
              'slots': {'0': {'preferred_ble_address': 'first', 'trigger_left_base': 10}}}
        original = copy.deepcopy(v2)
        value = schema.normalize_settings(v2)
        self.assertEqual(value['known_ble_devices'], {'FIRST': {'trigger_left_base': 10}, 'SECOND': {}})
        self.assertEqual(v2, original)
        self.assertEqual(schema.normalize_settings({'version': 3, 'global': {'auto_connect': False}}),
                         {'auto_connect': False})

    def test_invalid_legacy_containers_do_not_partially_migrate(self):
        for slots in ([], {'4': {}}, {'0': []}, {'0': {'preferred_ble_address': 0}}):
            with self.subTest(slots=slots), self.assertRaises(ValueError):
                schema.normalize_settings({'version': 2, 'slots': slots})
        with self.assertRaises(ValueError):
            schema.normalize_settings({'version': 2, 'global': {'known_ble_addresses': 'not-a-list'}})


class TransactionalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'gc_controller_settings.json'
        self.live = {'auto_connect': True, 'stick_deadzone': 0.1,
                     'known_ble_devices': {'EXISTING': {}}}
        self.manager = manager_module.SettingsManager([self.live], self.temp.name)

    def test_invalid_document_changes_no_live_fields_and_blocks_destructive_autosave(self):
        payload = b'{"version":4,"global":{"auto_connect":false,"slot_assignments":{"bad":99}}}'
        self.path.write_bytes(payload); original = copy.deepcopy(self.live)
        with self.assertLogs(manager_module.logger, 'WARNING'):
            self.assertFalse(self.manager.load())
        self.assertEqual(self.live, original)
        with self.assertRaises(ValueError): self.manager.save()
        self.assertEqual(self.path.read_bytes(), payload)

    def test_future_version_is_not_overwritten_with_defaults(self):
        payload = b'{"version":5,"global":{"future":true}}'
        self.path.write_bytes(payload)
        with self.assertLogs(manager_module.logger, 'WARNING'): self.manager.load()
        with self.assertRaises(ValueError): self.manager.save()
        self.assertEqual(self.path.read_bytes(), payload)

    def test_successful_explicit_reload_unblocks_save(self):
        self.path.write_bytes(b'bad')
        with self.assertLogs(manager_module.logger, 'WARNING'): self.manager.load()
        self.path.write_bytes(b'{"version":4,"global":{"auto_connect":false}}')
        self.assertTrue(self.manager.load())
        self.manager.save()
        self.assertFalse(json.loads(self.path.read_bytes())['global']['auto_connect'])

    def test_oversized_settings_are_rejected_before_read_or_state_change(self):
        self.path.write_bytes(b' ' * (schema.MAX_SETTINGS_BYTES + 1))
        with self.assertLogs(manager_module.logger, 'WARNING'):
            self.assertFalse(self.manager.load())
        self.assertTrue(self.live['auto_connect'])

    def test_unsafe_in_memory_value_never_replaces_valid_file(self):
        self.manager.save(); original = self.path.read_bytes()
        self.live['stick_deadzone'] = 1
        with self.assertRaises(ValueError): self.manager.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob('.gc-settings-*')))

    @unittest.skipIf(os.name == 'nt', 'Symlink creation requires Windows privileges')
    def test_settings_symlink_is_not_followed_or_replaced(self):
        other = self.path.with_name('other.json'); other.write_bytes(b'{}')
        self.path.symlink_to(other)
        with self.assertLogs(manager_module.logger, 'WARNING'): self.manager.load()
        with self.assertRaises(ValueError): self.manager.save()
        self.assertTrue(self.path.is_symlink()); self.assertEqual(other.read_bytes(), b'{}')
