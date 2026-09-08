import io
import json
import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import Mock

from _support import load_module, load_definitions

parent = load_module('ble/parent.py')


class CommandTransportTests(unittest.TestCase):
    def fake_process(self):
        return Mock(stdin=Mock(fileno=Mock(return_value=123)), poll=Mock(return_value=None))

    def test_partial_interrupted_writes_preserve_commands_and_snapshot_arguments(self):
        data = bytearray(); first = [True]
        def write(fd, view):
            if first[0]:
                first[0] = False
                raise InterruptedError
            data.extend(view[:2])
            return min(2, len(view))
        proc = self.fake_process(); errors = []
        transport = parent.CommandTransport(proc, lambda p, e: errors.append((p, e)), write=write)
        cmd = {'cmd': 'open'}
        self.assertTrue(transport.send(cmd))
        cmd['cmd'] = 'changed'
        self.assertTrue(transport.send({'cmd': 'shutdown'}))
        self.assertTrue(transport.close(wait=True))
        self.assertTrue(transport._writer.close(2))
        self.assertEqual([json.loads(line) for line in data.splitlines()],
                         [{'cmd': 'open'}, {'cmd': 'shutdown'}])
        self.assertEqual(errors, [])
        proc.stdin.close.assert_called_once()

    def test_broken_pipe_reports_original_owner_exactly_once(self):
        proc = self.fake_process(); failed = threading.Event(); errors = []
        def on_failure(owner, error):
            errors.append((owner, error)); failed.set()
        def write(fd, view): raise BrokenPipeError('closed')
        transport = parent.CommandTransport(proc, on_failure, write=write)
        transport.send({'cmd': 'open'})
        self.assertTrue(failed.wait(2))
        transport.close(wait=True)
        self.assertFalse(transport.send({'cmd': 'open'}))
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0][0], proc)
        self.assertIn('closed', errors[0][1])
        proc.stdin.close.assert_called_once()

    def test_queue_full_never_blocks_caller_or_closes_inflight_descriptor(self):
        entered, release = threading.Event(), threading.Event()
        def write(fd, view):
            entered.set(); release.wait(2); return len(view)
        proc = self.fake_process(); errors = []
        transport = parent.CommandTransport(proc, lambda p, e: errors.append(e),
                                             write=write, max_frames=1)
        try:
            transport.send({'cmd': 'first'})
            self.assertTrue(entered.wait(1))
            self.assertTrue(transport.send({'cmd': 'second'}))
            self.assertFalse(transport.send({'cmd': 'overflow'}))
            proc.stdin.close.assert_not_called()
            self.assertEqual(len(errors), 1)
        finally:
            release.set(); transport.close(wait=True)
            self.assertTrue(transport._writer.close(2))
        proc.stdin.close.assert_called_once()

    def test_close_escalates_only_for_owned_process(self):
        proc = self.fake_process()
        proc.wait.side_effect = [subprocess.TimeoutExpired('child', 1),
                                 subprocess.TimeoutExpired('child', 1), 0]
        transport = parent.CommandTransport(proc, Mock(), write=lambda fd, view: len(view))
        self.assertTrue(transport.close(wait=True))
        proc.terminate.assert_called_once(); proc.kill.assert_called_once()
        self.assertFalse(transport.send({'cmd': 'open'}))
        transport.close(wait=True)
        self.assertEqual(proc.wait.call_count, 3)

    def test_exited_child_wakes_error_handler(self):
        proc = self.fake_process(); proc.poll.return_value = 1
        errors = []
        transport = parent.CommandTransport(proc, lambda p, e: errors.append((p, e)))
        self.assertFalse(transport.send({'cmd': 'open'}))
        transport.close(wait=True)
        self.assertIs(errors[0][0], proc)
        self.assertIn('exited', errors[0][1])

    def test_real_stalled_child_is_reaped_after_command_write_deadline(self):
        # No radio, display, network or privileged command. This fills an actual
        # OS pipe on every CI host and verifies that shutdown releases its writer.
        proc = subprocess.Popen([sys.executable, '-u', '-c',
                                 "import sys,time; sys.stdout.buffer.write(b'ready\\n'); "
                                 "sys.stdout.buffer.flush(); time.sleep(30)"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        transport = None
        try:
            self.assertEqual(proc.stdout.readline(), b'ready\n')
            failed = threading.Event(); errors = []
            def on_failure(owner, error):
                errors.append((owner, error)); failed.set()
            transport = parent.CommandTransport(proc, on_failure, write_timeout=0.1,
                         grace_timeout=0.1, terminate_timeout=0.2,
                         max_bytes=1024 * 1024)
            for _ in range(8):
                transport.send({'cmd': 'blocked', 'payload': 'x' * 50000})
            self.assertTrue(failed.wait(4), 'The blocked command writer did not time out')
            self.assertIn('deadline', errors[0][1])
            self.assertIs(errors[0][0], proc)
            self.assertTrue(transport.close(wait=True))
            self.assertIsNotNone(proc.poll())
            self.assertTrue(transport._writer.close(2))
            self.assertTrue(proc.stdin.closed)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=3)
            if transport is not None:
                transport.close(wait=True)
                transport._writer.close(2)
            else:
                # An early readiness failure happens before a writer owns stdin.
                proc.stdin.close()
            proc.stdout.close()

    def test_headless_failure_wakes_initialization_and_emits_owned_loss(self):
        method = load_definitions('app.py', {'_command_failed'},
                                  class_name='_BleHeadlessManager')['_command_failed']
        proc = object(); callback = Mock()
        obj = types.SimpleNamespace(_subprocess=proc, _initialized=True,
                                    _init_event=threading.Event(), _on_event=callback)
        method(obj, proc, 'broken command pipe')
        self.assertFalse(obj._initialized)
        self.assertTrue(obj._init_event.is_set())
        self.assertIs(callback.call_args.args[0]['_process'], proc)
        callback.reset_mock()
        obj._subprocess = object()
        method(obj, proc, 'stale error')
        callback.assert_not_called()

    def test_observer_failure_does_not_prevent_reaping(self):
        proc = self.fake_process(); proc.poll.return_value = 1
        transport = parent.CommandTransport(proc, Mock(side_effect=RuntimeError('observer')))
        with self.assertLogs(parent.logger, level='ERROR'):
            self.assertFalse(transport.send({'cmd': 'open'}))
        self.assertTrue(transport.close(wait=True))
        self.assertNotIn(transport, parent._live)

    def test_final_exit_awaits_previously_retired_helpers(self):
        entered, release = threading.Event(), threading.Event()
        proc = self.fake_process()
        def wait(**kwargs):
            entered.set()
            release.wait(2)
            return 0
        proc.wait.side_effect = wait
        transport = parent.CommandTransport(proc, Mock(), write=lambda fd, view: len(view))
        transport.close()
        self.assertTrue(entered.wait(1))
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (parent.CommandTransport.close_all(), finished.set()))
        thread.start()
        try:
            self.assertFalse(finished.wait(0.02))
        finally:
            release.set(); thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertNotIn(transport, parent._live)
