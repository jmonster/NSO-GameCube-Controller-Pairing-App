"""Missing standard streams must not sever a windowed BLE child's IPC."""
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch, Mock

from _support import SRC, ROOT, load_module

module = load_module('process_io.py')


class StandardStreamsTests(unittest.TestCase):
    def test_existing_streams_are_not_replaced(self):
        streams = types.SimpleNamespace(stdin=Mock(), stdout=Mock(), stderr=Mock(), platform='linux')
        originals = (streams.stdin, streams.stdout, streams.stderr)
        with patch.object(module, 'sys', streams), patch.object(module, '_inherited_pipe') as inherited:
            module.prepare_standard_streams(ipc=True)
        inherited.assert_not_called()
        self.assertEqual((streams.stdin, streams.stdout, streams.stderr), originals)

    def test_gui_missing_streams_are_safe_sinks_not_console_allocations(self):
        streams = types.SimpleNamespace(stdin=None, stdout=None, stderr=None, platform='linux')
        with patch.object(module, 'sys', streams), patch.object(module, '_inherited_pipe') as inherited:
            module.prepare_standard_streams()
        try:
            self.assertEqual(streams.stdin.read(), '')
            streams.stdout.write('diagnostic'); streams.stdout.flush()
            streams.stderr.write('error'); streams.stderr.flush()
            inherited.assert_not_called()
        finally:
            for name in ('stdin', 'stdout', 'stderr'): getattr(streams, name).close()

    def test_required_ipc_never_falls_back_to_devnull_and_partial_setup_closes(self):
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        streams = types.SimpleNamespace(stdin=None, stdout=None, stderr=None, platform='linux')
        with patch.object(module, 'sys', streams), patch.object(module, '_inherited_pipe',
                side_effect=[r, OSError('not a pipe')]):
            with self.assertRaises(OSError): module.prepare_standard_streams(ipc=True)
        self.assertIsNone(streams.stdin); self.assertIsNone(streams.stdout)
        with self.assertRaises(OSError): os.fstat(r)

    def run_restored_child(self, executable, clear=True):
        source = str(SRC.parent)
        code = (f'import sys; sys.path.insert(0, {source!r}); '
                'from gc_controller.process_io import prepare_standard_streams; '
                + ('sys.stdin=sys.stdout=sys.stderr=None; ' if clear else '')
                + 'prepare_standard_streams(ipc=True); '
                'data=sys.stdin.buffer.read(256); '
                'sys.stdout.buffer.write(data); sys.stdout.buffer.flush()')
        result = subprocess.run([str(executable), '-c', code], input=bytes(range(256)),
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, bytes(range(256)))

    def test_real_inherited_pipes_restore_all_binary_bytes(self):
        self.run_restored_child(sys.executable)

    @unittest.skipUnless(sys.platform == 'win32', 'Native pythonw is Windows only')
    def test_native_pythonw_receives_and_returns_redirected_pipes(self):
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        self.assertTrue(pythonw.is_file(), 'Windows runner must provide pythonw')
        self.run_restored_child(pythonw, clear=False)

    def test_source_and_packaged_dependencies_share_one_manifest(self):
        import tomllib
        manifest = tomllib.loads((ROOT / 'pyproject.toml').read_text())
        deps = manifest['project']['dependencies']
        self.assertTrue(any(d.startswith('bumble') and 'linux' in d for d in deps))
        windows = next(d for d in deps if d.startswith('vgamepad'))
        self.assertIn('3f910aa8bbde49a576683db74ad5e4a0879f8a80', windows)
        requirements = [line for line in (ROOT / 'requirements.txt').read_text().splitlines()
                        if line and not line.startswith('#')]
        self.assertEqual(requirements, ['.[build]'])
