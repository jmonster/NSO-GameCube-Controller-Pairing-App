"""Never run systemctl, btmgmt or hciconfig in tests."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

from _support import SRC, load_module

bluez = load_module('ble/bluez.py')


class BlueZLeaseTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.service = 'active'
        self.up = True
        self.failure = None
        self.locked = False
        self.journal = Mock(read=Mock(return_value=None))

    @contextmanager
    def lock(self):
        self.assertFalse(self.locked)
        self.locked = True
        try: yield
        finally: self.locked = False

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(kwargs['timeout'], 5)
        self.assertEqual(kwargs['env']['LC_ALL'], 'C')
        if self.failure and self.failure(command):
            raise subprocess.TimeoutExpired(command, 5)
        if command[:2] == ['systemctl', 'show']:
            output = self.service + '\n'
        elif command[-1] == 'info':
            output = 'hci2: Primary controller\n current settings: ' + ('powered ' if self.up else '') + 'le\n'
        elif command == ['hciconfig', 'hci2']:
            output = 'hci2: Type: Primary\n BD Address: 00:11:22:33:44:55\n ' + ('UP RUNNING' if self.up else 'DOWN') + '\n'
        else:
            output = ''
        return types.SimpleNamespace(returncode=0, stdout=output, stderr='')

    def lease(self, tool='btmgmt', run=None):
        return bluez.BlueZLease(2, run=self.run_command if run is None else run,
                               lock_factory=self.lock, adapter_tool=tool, resolve_tool=lambda name: name,
                               journal=self.journal)

    def test_active_service_and_selected_adapter_are_restored(self):
        lease = self.lease()
        self.assertTrue(lease.acquire()); self.assertTrue(lease.is_acquired)
        self.assertTrue(self.locked)
        before = len(self.calls); lease.acquire(); self.assertEqual(len(self.calls), before)
        lease.release(); self.assertFalse(self.locked)
        self.assertFalse(lease.is_acquired)
        self.assertEqual(self.calls[-2:], [['systemctl', 'start', 'bluetooth.service'],
                                          ['btmgmt', '--index', '2', 'power', 'on']])
        self.assertFalse(any('hci0' in c or 'hci1' in c for c in self.calls))
        before = len(self.calls); lease.release(); self.assertEqual(len(self.calls), before)

    def test_previously_inactive_service_is_never_started(self):
        for state in ('inactive', 'failed'):
            with self.subTest(state=state):
                self.service = state; self.calls.clear()
                lease = self.lease(); lease.acquire(); lease.release()
                self.assertFalse(any(c[:2] in (['systemctl', 'start'], ['systemctl', 'stop']) for c in self.calls))

    def test_previously_off_adapter_is_restored_off_after_service_restart(self):
        self.up = False
        lease = self.lease(); lease.acquire(); lease.release()
        self.assertEqual(self.calls[-1], ['btmgmt', '--index', '2', 'power', 'off'])

    def test_unstable_or_unknown_service_state_has_no_side_effects(self):
        for state in ('activating', 'deactivating', 'reloading', ''):
            with self.subTest(state=state):
                self.service = state; self.calls.clear()
                with self.assertRaises(RuntimeError): self.lease().acquire()
                self.assertEqual(len(self.calls), 1)
                self.assertFalse(self.locked)

    def test_failed_query_never_stops_service(self):
        fake = Mock(return_value=types.SimpleNamespace(returncode=1, stdout='', stderr='denied'))
        with self.assertRaisesRegex(RuntimeError, 'denied'): self.lease(run=fake).acquire()
        self.assertEqual(fake.call_count, 1)
        self.assertFalse(self.locked)

    def test_partial_stop_timeout_still_attempts_full_rollback(self):
        self.failure = lambda cmd: cmd[:2] == ['systemctl', 'stop']
        lease = self.lease()
        with self.assertRaises(subprocess.TimeoutExpired): lease.acquire()
        self.assertIn(['systemctl', 'start', 'bluetooth.service'], self.calls)
        self.assertIn(['btmgmt', '--index', '2', 'power', 'on'], self.calls)
        self.assertFalse(lease.is_acquired); self.assertFalse(self.locked)

    def test_adapter_down_failure_rolls_back_service(self):
        self.failure = lambda cmd: cmd[-2:] == ['power', 'off']
        with self.assertRaises(subprocess.TimeoutExpired): self.lease().acquire()
        self.assertIn(['systemctl', 'start', 'bluetooth.service'], self.calls)
        self.assertFalse(self.locked)

    def test_failed_restore_keeps_lock_and_reapplies_power_after_service_retry(self):
        lease = self.lease(); lease.acquire()
        self.failure = lambda cmd: cmd[:2] == ['systemctl', 'start']
        with self.assertRaisesRegex(RuntimeError, 'restore'): lease.release()
        self.assertTrue(self.locked)
        self.assertIn(['btmgmt', '--index', '2', 'power', 'on'], self.calls)
        self.failure = None; before = len(self.calls)
        lease.release()
        self.assertEqual(self.calls[before:], [['systemctl', 'start', 'bluetooth.service'],
                         ['btmgmt', '--index', '2', 'power', 'on']])
        self.assertFalse(self.locked)

    def test_hciconfig_fallback_is_scoped_to_selected_adapter(self):
        lease = self.lease('hciconfig'); lease.acquire(); lease.release()
        self.assertIn(['hciconfig', 'hci2', 'down'], self.calls)
        self.assertEqual(self.calls[-1], ['hciconfig', 'hci2', 'up'])

    def test_bad_adapter_output_does_not_trigger_takeover(self):
        def run(cmd, **kwargs):
            result = self.run_command(cmd, **kwargs)
            if cmd[-1] == 'info': result.stdout = 'unrecognized response'
            return result
        with self.assertRaisesRegex(RuntimeError, 'power state'): self.lease(run=run).acquire()
        self.assertFalse(any(c[:2] == ['systemctl', 'stop'] for c in self.calls))

    def test_second_helper_fails_before_changing_system_state(self):
        @contextmanager
        def busy():
            raise RuntimeError('already owned')
            yield
        lease = bluez.BlueZLease(2, run=self.run_command, lock_factory=busy, adapter_tool='btmgmt',
                               resolve_tool=lambda name: name, journal=self.journal)
        with self.assertRaisesRegex(RuntimeError, 'already owned'): lease.acquire()
        self.assertEqual(self.calls, [])

    def test_invalid_index_rejected_without_side_effects(self):
        for index in (None, True, -1, 65536, '2'):
            with self.subTest(index=index), self.assertRaises(ValueError):
                bluez.BlueZLease(index)


@unittest.skipIf(sys.platform == 'win32', 'Windows termination does not deliver POSIX SIGTERM')
class ChildTerminationTests(unittest.TestCase):
    def test_sigterm_runs_backend_cleanup_and_wakes_command_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'closed'
            code = f'''import sys
sys.path.insert(0, {str(SRC.parent)!r})
from gc_controller.ble.child_runtime import run_subprocess
from pathlib import Path
class Backend:
    async def close(self): Path({str(marker)!r}).write_text('closed')
run_subprocess(Backend())
'''
            proc = subprocess.Popen([sys.executable, '-u', '-c', code],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertIn(b'"ready"', proc.stdout.readline())
                proc.terminate()
                proc.wait(timeout=5)
                self.assertEqual(marker.read_text(), 'closed')
                self.assertEqual(proc.returncode, 0)
            finally:
                if proc.poll() is None: proc.kill(); proc.wait(timeout=3)
                proc.stdin.close(); proc.stdout.close(); proc.stderr.close()
