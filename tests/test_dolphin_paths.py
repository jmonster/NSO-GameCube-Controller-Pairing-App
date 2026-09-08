import ntpath
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest.mock import patch

from _support import load_definitions


class DolphinTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def functions(self, platform='darwin'):
        return load_definitions('virtual_gamepad.py',
                                {'_get_all_dolphin_user_dirs', 'ensure_dolphin_pipe'},
                                {'os': os, 'stat': stat,
                                 'sys': types.SimpleNamespace(platform=platform),
                                 '_REAL_HOME': str(self.home),
                                 '_FLATPAK_DOLPHIN_DATA': str(self.home / 'flatpak')})


class DolphinDirectoryTests(DolphinTestCase):
    def test_macos_first_run_uses_application_support(self):
        funcs = self.functions()
        expected = self.home / 'Library/Application Support/Dolphin'
        self.assertFalse(expected.exists())
        self.assertEqual(funcs['_get_all_dolphin_user_dirs'](), [str(expected)])

    def test_macos_ignores_linux_xdg_default(self):
        os.environ['XDG_DATA_HOME'] = str(self.home / 'linux-only')
        dirs = self.functions()['_get_all_dolphin_user_dirs']()
        self.assertEqual(dirs, [str(self.home / 'Library/Application Support/Dolphin')])

    def test_explicit_override_is_authoritative_before_creation(self):
        custom = self.home / 'custom-user'
        os.environ['DOLPHIN_EMU_USERPATH'] = str(custom)
        for platform in ('darwin', 'linux'):
            with self.subTest(platform=platform):
                self.assertEqual(self.functions(platform)['_get_all_dolphin_user_dirs'](),
                                 [str(custom)])

    def test_override_expands_tilde_and_resolves_absolute_path(self):
        os.environ.update(HOME=str(self.home), USERPROFILE=str(self.home),
                          DOLPHIN_EMU_USERPATH='~/custom')
        self.assertEqual(self.functions()['_get_all_dolphin_user_dirs'](),
                         [str(self.home / 'custom')])

    def test_linux_fallback_uses_real_home_not_root_home(self):
        os.environ['HOME'] = '/root'
        self.assertEqual(self.functions('linux')['_get_all_dolphin_user_dirs'](),
                         [str(self.home / '.local/share/dolphin-emu')])

    def test_linux_honors_xdg_and_empty_xdg_is_not_relative(self):
        os.environ['XDG_DATA_HOME'] = str(self.home / 'data')
        get_dirs = self.functions('linux')['_get_all_dolphin_user_dirs']
        self.assertEqual(get_dirs(), [str(self.home / 'data/dolphin-emu')])
        os.environ['XDG_DATA_HOME'] = ''
        self.assertEqual(get_dirs(), [str(self.home / '.local/share/dolphin-emu')])

    def test_linux_keeps_multiple_existing_installations(self):
        flatpak = self.home / 'flatpak'
        legacy = self.home / '.dolphin-emu'
        flatpak.mkdir()
        legacy.mkdir()
        self.assertEqual(self.functions('linux')['_get_all_dolphin_user_dirs'](),
                         [str(flatpak), str(legacy)])

    @unittest.skipIf(os.name == 'nt', 'Symlink creation requires Windows privileges')
    def test_linux_deduplicates_real_paths(self):
        flatpak = self.home / 'flatpak'
        flatpak.mkdir()
        (self.home / '.dolphin-emu').symlink_to(flatpak, target_is_directory=True)
        self.assertEqual(self.functions('linux')['_get_all_dolphin_user_dirs'](), [str(flatpak)])

    def test_rejects_pipe_names_that_escape_directory(self):
        ensure = self.functions()['ensure_dolphin_pipe']
        for name in ('', '.', '..', '../outside', '/tmp/outside', 'a/b', 'a\\b', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ensure(name)


@unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO tests')
class DolphinFIFOTests(DolphinTestCase):
    def test_creates_all_four_first_run_pipes(self):
        ensure = self.functions()['ensure_dolphin_pipe']
        for index in range(1, 5):
            pipes = ensure(f'gc_controller_{index}')
            self.assertEqual(len(pipes), 1)
            path = Path(pipes[0])
            self.assertEqual(path.parent, self.home / 'Library/Application Support/Dolphin/Pipes')
            self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)
            self.assertEqual(ensure(f'gc_controller_{index}'), pipes)
        self.assertFalse((self.home / '.local').exists())

    def test_does_not_overwrite_regular_file(self):
        directory = self.home / 'Library/Application Support/Dolphin/Pipes'
        directory.mkdir(parents=True)
        path = directory / 'gc_controller'
        path.write_text('keep this')
        with self.assertRaises(RuntimeError):
            self.functions()['ensure_dolphin_pipe']()
        self.assertEqual(path.read_text(), 'keep this')

    def test_rejects_symlink_even_when_target_is_fifo(self):
        directory = self.home / 'Library/Application Support/Dolphin/Pipes'
        directory.mkdir(parents=True)
        outside = self.home / 'outside'
        os.mkfifo(outside)
        (directory / 'gc_controller').symlink_to(outside)
        with self.assertRaises(RuntimeError):
            self.functions()['ensure_dolphin_pipe']()
        self.assertTrue(stat.S_ISFIFO(outside.stat().st_mode))

    def test_explicit_override_creates_only_requested_directory(self):
        custom = self.home / 'custom'
        os.environ['DOLPHIN_EMU_USERPATH'] = str(custom)
        self.assertEqual(self.functions()['ensure_dolphin_pipe'](),
                         [str(custom / 'Pipes/gc_controller')])
        self.assertFalse((self.home / 'Library').exists())

    def test_unwritable_or_invalid_parent_reports_failure(self):
        custom = self.home / 'regular-file'
        custom.write_text('not a directory')
        os.environ['DOLPHIN_EMU_USERPATH'] = str(custom)
        with self.assertRaises(RuntimeError):
            self.functions()['ensure_dolphin_pipe']()


class DolphinPathSemanticsTests(unittest.TestCase):
    """Exercise Windows path semantics on every CI host, without filesystem IO."""

    def get_dirs(self, platform, environ=None):
        path_api = types.SimpleNamespace(join=ntpath.join, realpath=ntpath.normpath,
                                         isdir=lambda path: False)
        return load_definitions(
            'virtual_gamepad.py', {'_get_all_dolphin_user_dirs'},
            {'os': types.SimpleNamespace(path=path_api, environ=environ or {}),
             'sys': types.SimpleNamespace(platform=platform),
             '_REAL_HOME': r'C:\Users\runner',
             '_FLATPAK_DOLPHIN_DATA': r'C:\Users\runner\flatpak'},
        )['_get_all_dolphin_user_dirs']()

    def test_macos_default_does_not_embed_posix_separators(self):
        self.assertEqual(self.get_dirs('darwin'),
                         [r'C:\Users\runner\Library\Application Support\Dolphin'])

    def test_linux_default_does_not_embed_posix_separators(self):
        for environ in ({}, {'XDG_DATA_HOME': ''}):
            with self.subTest(environ=environ):
                self.assertEqual(self.get_dirs('linux', environ),
                                 [r'C:\Users\runner\.local\share\dolphin-emu'])
