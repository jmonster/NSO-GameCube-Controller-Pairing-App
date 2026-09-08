"""Recovery is tested without system services, radios or elevated commands."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from _support import load_module

bluez = load_module('ble/bluez.py')


class MemoryJournal:
    def __init__(self): self.state = None; self.writes = []; self.fail = False
    def read(self): return copy.deepcopy(self.state)
    def write(self, state):
        if self.fail: raise OSError('journal unavailable')
        self.state = copy.deepcopy(state); self.writes.append(copy.deepcopy(state))
    def clear(self): self.state = None


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.journal = MemoryJournal(); self.commands = []; self.locked = False
        self.failure = None

    @contextmanager
    def lock(self):
        self.assertFalse(self.locked); self.locked = True
        try: yield
        finally: self.locked = False

    def run_command(self, args, **kwargs):
        self.assertTrue(self.locked)
        self.commands.append(args)
        self.assertEqual(kwargs['cwd'], '/')
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['env'], bluez._SAFE_ENV)
        self.assertTrue(args[0].startswith('/usr/bin/'))
        if self.failure and self.failure(args): raise RuntimeError('injected failure')
        if args[1] == 'show': output = 'active\n'
        elif args[-1] == 'info': output = ' current settings: powered le\n'
        else:
            self.assertIsNotNone(self.journal.state, 'Mutation must have a durable recovery record')
            output = ''
        return types.SimpleNamespace(returncode=0, stdout=output, stderr='')

    def lease(self, index=2):
        return bluez.BlueZLease(index, run=self.run_command, journal=self.journal,
                               lock_factory=self.lock, resolve_tool=lambda n: '/usr/bin/' + n)

    def test_record_precedes_stop_and_power_changes(self):
        lease = self.lease(); lease.acquire()
        self.assertEqual(self.journal.state, dict(version=1, hci_index=2,
                           original_power=True, restore_service=True, restore_adapter=True))
        lease.release(); self.assertIsNone(self.journal.state); self.assertFalse(self.locked)

    def test_interrupted_other_adapter_is_restored_before_new_snapshot(self):
        self.journal.state = dict(version=1, hci_index=7, original_power=False,
                                  restore_service=True, restore_adapter=True)
        lease = self.lease(); lease.acquire()
        self.assertEqual(self.commands[:3], [
            ['/usr/bin/systemctl', 'start', 'bluetooth.service'],
            ['/usr/bin/btmgmt', '--index', '7', 'power', 'off'],
            ['/usr/bin/systemctl', 'show', 'bluetooth.service', '--property=ActiveState', '--value']])
        self.assertEqual(self.journal.state['hci_index'], 2)
        lease.release()

    def test_failed_recovery_never_starts_a_new_takeover(self):
        old = dict(version=1, hci_index=7, original_power=False,
                   restore_service=True, restore_adapter=True)
        self.journal.state = old.copy()
        self.failure = lambda args: args[1] == 'start'
        lease = self.lease()
        with self.assertRaisesRegex(RuntimeError, 'restore'): lease.acquire()
        self.assertTrue(self.locked)
        self.assertEqual(self.journal.state, old)
        self.assertFalse(any('show' in command or 'stop' in command for command in self.commands))
        self.failure = None; lease.release()
        self.assertFalse(self.locked); self.assertIsNone(self.journal.state)

    def test_failed_journal_write_performs_no_mutation(self):
        self.journal.fail = True
        with self.assertRaises(OSError): self.lease().acquire()
        self.assertEqual(len(self.commands), 2)
        self.assertFalse(self.locked)

    def test_invalid_journal_never_executes_commands(self):
        self.journal.state = {'version': 99, 'command': 'arbitrary'}
        with self.assertRaises(RuntimeError): self.lease().acquire()
        self.assertEqual(self.commands, []); self.assertFalse(self.locked)

    def test_failed_clear_retains_ownership_until_retry(self):
        lease = self.lease(); lease.acquire()
        with patch.object(self.journal, 'clear', side_effect=OSError('read only')):
            with self.assertRaises(RuntimeError): lease.release()
            self.assertTrue(self.locked); self.assertIsNotNone(self.journal.state)
        count = len(self.commands); lease.release()
        self.assertEqual(len(self.commands), count)
        self.assertFalse(self.locked)

    def test_service_start_failure_preserves_conservative_durable_record(self):
        lease = self.lease(); lease.acquire(); before = self.journal.state.copy()
        self.failure = lambda args: args[1] == 'start'
        with self.assertRaises(RuntimeError): lease.release()
        self.assertEqual(before, self.journal.state)
        self.failure = None; lease.release()

    def test_caller_environment_is_never_forwarded(self):
        with patch.dict(os.environ, {'PATH': '/attacker', 'LD_PRELOAD': 'evil', 'PYTHONPATH': '/evil'}):
            lease = self.lease(); lease.acquire(); lease.release()


class ToolResolutionTests(unittest.TestCase):
    def test_rejects_names_outside_fixed_allowlist(self):
        for name in ('/tmp/systemctl', '../btmgmt', 'sh', 'systemctl --help'):
            with self.subTest(name=name), self.assertRaises(ValueError): bluez._system_tool(name)

    def test_unprivileged_or_writable_executables_are_not_selected(self):
        for uid, mode in ((1000, 0o100755), (0, 0o100777)):
            fake = types.SimpleNamespace(st_uid=uid, st_mode=mode)
            with patch.object(bluez.os, 'stat', return_value=fake), self.assertRaises(RuntimeError):
                bluez._system_tool('btmgmt')

    def test_selected_tool_ignores_callers_path_and_checks_parent_ownership(self):
        def safe_stat(path):
            return types.SimpleNamespace(st_uid=0, st_mode=0o100755 if path.endswith('btmgmt') else 0o40755)
        with patch.dict(os.environ, {'PATH': '/tmp/attacker'}), \
             patch.object(bluez.os.path, 'realpath', side_effect=lambda p: p), \
             patch.object(bluez.os, 'stat', side_effect=safe_stat), \
             patch.object(bluez.os, 'access', return_value=True):
            self.assertEqual(bluez._system_tool('btmgmt'), os.path.join('/usr/sbin', 'btmgmt'))


@unittest.skipIf(sys.platform == 'win32', 'POSIX protected directory and descriptor semantics')
class DiskJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / 'state'
        self.journal = bluez.RecoveryJournal(str(self.directory), owner_uid=os.getuid())
        self.state = dict(version=1, hci_index=3, original_power=False,
                          restore_service=False, restore_adapter=True)

    def test_roundtrip_has_restrictive_permissions_and_removes_journal(self):
        self.assertIsNone(self.journal.read())
        self.journal.write(self.state); self.assertEqual(self.journal.read(), self.state)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / self.journal.name).stat().st_mode), 0o600)
        self.journal.clear(); self.journal.clear(); self.assertIsNone(self.journal.read())

    def test_rejects_duplicate_fields_symlinks_hardlinks_and_oversized_data(self):
        self.journal.write(self.state); path = self.directory / self.journal.name
        original = path.read_bytes()
        path.write_bytes(b'{"version":1,"version":1}')
        with self.assertRaises(ValueError): self.journal.read()
        path.write_bytes(b'x' * 4097)
        with self.assertRaises(RuntimeError): self.journal.read()
        path.write_bytes(original); other = self.directory / 'copy'; os.link(path, other)
        with self.assertRaises(RuntimeError): self.journal.read()
        path.unlink(); path.symlink_to(other)
        with self.assertRaises(OSError): self.journal.read()

    def test_permissions_and_nonregular_entries_fail_closed(self):
        self.journal.write(self.state); path = self.directory / self.journal.name
        path.chmod(0o644)
        with self.assertRaises(RuntimeError): self.journal.read()
        path.unlink(); os.mkfifo(path, 0o600)
        with self.assertRaises(RuntimeError): self.journal.read()
        self.directory.chmod(0o755)
        with self.assertRaises(RuntimeError): self.journal.read()

    def test_failure_before_atomic_publication_retains_previous_state(self):
        self.journal.write(self.state)
        with patch.object(bluez.os, 'replace', side_effect=OSError('fail')):
            with self.assertRaises(OSError): self.journal.write({**self.state, 'hci_index': 4})
        self.assertEqual(self.journal.read(), self.state)
        self.assertEqual([p.name for p in self.directory.iterdir()], [self.journal.name])

    def test_invalid_schema_never_published(self):
        for extra in ({'hci_index': True}, {'original_power': 1}, {'version': 2}, {'hci_index': -1}):
            with self.subTest(extra=extra), self.assertRaises(RuntimeError):
                self.journal.write({**self.state, **extra})
        self.assertFalse(self.directory.exists())
