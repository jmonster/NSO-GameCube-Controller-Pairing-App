import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from _support import fake_module, load_module

storage = load_module('settings_storage.py')


class SettingsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home, self.cwd = self.root / 'home', self.root / 'checkout'
        self.cwd.mkdir()
        self.legacy = self.cwd / storage.SETTINGS_NAME

    def directory(self, platform='linux', **kwargs):
        return Path(storage.get_settings_dir(platform=platform, home=self.home,
                    cwd=self.cwd, environ=kwargs.pop('environ', {}), **kwargs))

    def test_development_and_frozen_share_platform_paths(self):
        expected = {'darwin': self.home / 'Library' / 'Application Support',
                    'win32': self.home, 'linux': self.home / '.config'}
        for platform, base in expected.items():
            with self.subTest(platform=platform):
                self.assertEqual(self.directory(platform, frozen=False), base / 'NSO-GC-Controller')
                self.assertEqual(self.directory(platform, frozen=True), base / 'NSO-GC-Controller')

    def test_empty_and_relative_environment_bases_do_not_use_working_directory(self):
        for value in ('', 'relative'):
            self.assertEqual(self.directory(environ={'XDG_CONFIG_HOME': value}),
                             self.home / '.config' / 'NSO-GC-Controller')
            self.assertEqual(self.directory('win32', environ={'APPDATA': value}),
                             self.home / 'NSO-GC-Controller')

    def test_explicit_absolute_config_base_is_respected(self):
        base = self.root / 'custom'
        for platform, key in (('linux', 'XDG_CONFIG_HOME'), ('win32', 'APPDATA')):
            self.assertEqual(self.directory(platform, environ={key: str(base)}),
                             base / 'NSO-GC-Controller')

    def test_legacy_migration_preserves_bytes_original_and_new_settings(self):
        payload = b'{"version":4,"global":{"known_ble_devices":{"test":{}}}}'
        self.legacy.write_bytes(payload)
        directory = self.directory(frozen=False)
        target = directory / storage.SETTINGS_NAME
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(self.legacy.read_bytes(), payload)
        self.legacy.write_text('{}')
        self.directory(frozen=False)
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(list(directory.glob('.gc-settings-*')))

    def test_packaged_launch_never_imports_working_directory_settings(self):
        self.legacy.write_text('{}')
        directory = self.directory(frozen=True)
        self.assertFalse((directory / storage.SETTINGS_NAME).exists())

    def test_bad_legacy_json_is_not_published(self):
        for payload in (b'not json', b'[]', b'\xff', '{}'.encode('utf-16'), b' ' * (storage.MAX_LEGACY_BYTES + 1)):
            self.legacy.write_bytes(payload)
            with self.assertLogs(storage._logger, 'WARNING'):
                directory = self.directory(frozen=False)
            self.assertFalse((directory / storage.SETTINGS_NAME).exists())

    @unittest.skipIf(os.name == 'nt', 'Symlink creation requires Windows privileges')
    def test_symlink_legacy_is_not_imported(self):
        other = self.root / 'other.json'
        other.write_text('{}')
        self.legacy.symlink_to(other)
        with self.assertLogs(storage._logger, 'WARNING'):
            directory = self.directory(frozen=False)
        self.assertFalse((directory / storage.SETTINGS_NAME).exists())

    def test_failed_migration_keeps_original_without_partial_target(self):
        self.legacy.write_text('{}')
        with patch.object(storage.os, 'link', side_effect=OSError('no hard links')):
            with self.assertLogs(storage._logger, 'WARNING'):
                directory = self.directory(frozen=False)
        self.assertEqual(self.legacy.read_text(), '{}')
        self.assertFalse((directory / storage.SETTINGS_NAME).exists())
        self.assertFalse(list(directory.glob('.gc-settings-*')))

    def test_concurrent_migration_never_clobbers_winner(self):
        self.legacy.write_text('{}')
        link = os.link
        def competing_publication(source, target):
            Path(target).write_bytes(b'{"winner":true}')
            return link(source, target)
        with patch.object(storage.os, 'link', side_effect=competing_publication):
            directory = self.directory(frozen=False)
        self.assertEqual((directory / storage.SETTINGS_NAME).read_bytes(), b'{"winner":true}')
        self.assertFalse(list(directory.glob('.gc-settings-*')))

    def test_atomic_write_preserves_old_file_on_flush_or_replace_error(self):
        target = self.root / 'settings.json'
        target.write_bytes(b'old')
        for operation in ('fsync', 'replace'):
            with self.subTest(operation=operation):
                with patch.object(storage.os, operation, side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        storage.atomic_write(target, b'new')
                self.assertEqual(target.read_bytes(), b'old')
                self.assertFalse(list(self.root.glob('.gc-settings-*')))

    def test_manager_save_and_load_use_atomic_utf8_v4_format(self):
        module = load_module('settings_manager.py', {
            'controller_constants': fake_module(DEFAULT_CALIBRATION={}, MAX_SLOTS=4, BLE_DEVICE_CAL_KEYS=[]),
            'settings_storage': storage})
        calibration = {'known_ble_devices': {'controller-\u00e9': {}}, 'stick_deadzone': 0.1}
        manager = module.SettingsManager([calibration], str(self.root))
        manager.save()
        target = self.root / storage.SETTINGS_NAME
        saved = target.read_bytes()
        self.assertEqual(json.loads(saved)['global'], calibration)
        calibration['stick_deadzone'] = float('nan')
        with self.assertRaises(ValueError):
            manager.save()
        self.assertEqual(target.read_bytes(), saved)
        calibration.clear()
        manager.load()
        self.assertEqual(calibration['stick_deadzone'], 0.1)
