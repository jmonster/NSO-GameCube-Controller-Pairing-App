"""Host-side delivery tests: preserve the legacy controller/IPC byte contract."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from _support import SRC, load_module

output = load_module('ble/pipe_output.py')
HELPERS = ('bleak_subprocess', 'ble_subprocess')


class PipeWriterTests(unittest.TestCase):
    def writer(self, **limits):
        read, write = os.pipe()
        writer = output.PipeWriter(write, **limits)
        os.close(write)
        self.addCleanup(os.close, read)
        self.addCleanup(writer.close)
        return writer, read

    def test_helper_callbacks_queue_output_instead_of_writing_synchronously(self):
        for name in HELPERS:
            with self.subTest(helper=name):
                helper = load_module('ble/' + name + '.py')
                helper._output = Mock()
                with patch.object(helper.os, 'write', side_effect=AssertionError('Callback performed OS write')):
                    helper.send({'e': 'ready'})
                    helper.PipeQueue(1).put_nowait(bytes(range(63)))
                self.assertEqual(helper._output.submit.call_count, 2)
                self.assertEqual(helper._output.submit.call_args_list[0].args[0], b'{"e":"ready"}\n')
                self.assertEqual(helper._output.submit.call_args_list[1].args[0],
                                 b'\xff\x01' + bytes(range(63)) + b'\0')
                helper._output.fail.assert_not_called()

    def test_legacy_reports_and_json_are_byte_identical_for_both_helpers(self):
        for name in HELPERS:
            with self.subTest(helper=name):
                writer, read = self.writer()
                helper = load_module('ble/' + name + '.py')
                helper._output = writer
                event = {'e': 'status', 's': 2, 'msg': 'héllo\n\r\xff'}
                helper.send(event)
                expected = (json.dumps(event, separators=(',', ':')) + '\n').encode()
                # Include all byte values, every slot and the existing 63-byte
                # GATT -> 64-byte host padding, without adding shape restrictions.
                for slot in range(4):
                    adapter = helper.PipeQueue(slot)
                    for size in (0, 1, 63, 64, 65, 256):
                        data = bytearray((i + slot * 64) % 256 for i in range(size))
                        adapter.put_nowait(data)
                        expected += bytes([255, slot]) + bytes(data[:64]).ljust(64, b'\0')
                        data[:] = b'\x77' * size
                self.assertTrue(writer.close())
                with os.fdopen(os.dup(read), 'rb') as stream:
                    self.assertEqual(stream.read(), expected)

    def test_partial_and_interrupted_writes_finish_before_the_next_message(self):
        original_write = os.write
        calls = 0
        def fragmented(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError()
            return original_write(fd, data[:3])
        with patch.object(output.os, 'write', fragmented):
            writer, read = self.writer()
            expected = b'first\n' + bytes(range(256)) + b'last\n'
            for packet in (b'first\n', bytes(range(256)), b'last\n'):
                self.assertTrue(writer.submit(packet))
            self.assertTrue(writer.close())
        self.assertEqual(os.read(read, 4096), expected)

    def test_inflight_message_counts_towards_message_and_byte_limits(self):
        for limits, first, second in (({'max_messages': 1}, b'a', b'b'),
                                       ({'max_bytes': 4}, b'1234', b'5')):
            with self.subTest(limits=limits):
                entered, release = threading.Event(), threading.Event()
                def blocked(fd, data):
                    entered.set()
                    release.wait(3)
                    return len(data)
                with patch.object(output.os, 'write', blocked):
                    writer, _ = self.writer(**limits)
                    failure = Mock()
                    writer.on_failure = failure
                    try:
                        self.assertTrue(writer.submit(first))
                        self.assertTrue(entered.wait(1))
                        self.assertFalse(writer.submit(second))
                        self.assertIsInstance(writer.error, BufferError)
                        self.assertFalse(writer.submit(b'late'))
                        failure.assert_called_once()
                    finally:
                        release.set()
                        writer.close()

    def test_queued_mutable_buffers_are_snapshots_not_reused_storage(self):
        entered, release = threading.Event(), threading.Event()
        actual = []
        def blocked(fd, data):
            entered.set()
            release.wait(3)
            actual.append(bytes(data))
            return len(data)
        with patch.object(output.os, 'write', blocked):
            writer, _ = self.writer()
            try:
                packet = bytearray(b'pressed')
                writer.submit(packet)
                self.assertTrue(entered.wait(1))
                packet[:] = b'release'
                writer.submit(packet)
                packet[:] = b'garbage'
            finally:
                release.set()
                writer.close()
        self.assertEqual(actual, [b'pressed', b'release'])

    def test_broken_and_zero_writes_notify_once(self):
        for result in (BrokenPipeError(), 0):
            with self.subTest(result=result):
                failure = threading.Event()
                fn = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                with patch.object(output.os, 'write', fn):
                    writer, _ = self.writer()
                    callback = Mock(side_effect=lambda error: failure.set())
                    writer.on_failure = callback
                    writer.submit(b'input')
                    self.assertTrue(failure.wait(1))
                    self.assertFalse(writer.close())
                    self.assertFalse(writer.submit(b'late'))
                    callback.assert_called_once()

    def test_stall_fails_without_blocking_producer_event_loop_or_close(self):
        entered, release, failed = (threading.Event() for _ in range(3))
        async def produce(writer):
            writer.submit(b'input')
            for _ in range(10):
                await asyncio.sleep(0)
            return 'loop is responsive'
        def blocked(fd, data):
            entered.set()
            release.wait(3)
            return len(data)
        with patch.object(output.os, 'write', blocked):
            writer, _ = self.writer(stall_timeout=0.05)
            writer.on_failure = lambda error: failed.set()
            try:
                self.assertEqual(asyncio.run(produce(writer)), 'loop is responsive')
                self.assertTrue(entered.wait(1))
                self.assertTrue(failed.wait(1))
                self.assertIsInstance(writer.error, TimeoutError)
                self.assertFalse(writer.close(timeout=0.01))
                # Only the writing thread can close/reuse this descriptor.
                os.fstat(writer._fd)
            finally:
                release.set()
                writer._writer.join(1)
        self.assertFalse(writer._writer.is_alive())
        with self.assertRaises(OSError):
            os.fstat(writer._fd)

    def test_idle_writer_has_no_inactivity_disconnect_and_close_drains(self):
        writer, read = self.writer(stall_timeout=0.05)
        time.sleep(0.1)  # No outstanding bytes: this is not a controller timeout.
        self.assertIsNone(writer.error)
        writer.submit(b'final')
        self.assertTrue(writer.close())
        self.assertFalse(writer._watchdog.is_alive())
        self.assertEqual(os.read(read, 20), b'final')
        self.assertFalse(writer.submit(b'late'))

    def test_existing_parent_parser_reads_writer_output(self):
        ipc = load_module('ble/ipc.py')
        writer, read = self.writer()
        reports, events = [], []
        expected = []
        for slot in range(4):
            for state in (0, 1, 0):
                payload = bytes([state]) + bytes(range(1, 64))
                expected.append((slot, payload))
                writer.submit(bytes([255, slot]) + payload)
        writer.submit(b'{"e":"connected","s":0,"mac":"unchanged"}\n')
        self.assertTrue(writer.close())
        with os.fdopen(os.dup(read), 'rb') as stream:
            ipc.read_event_stream(stream, lambda *args: reports.append(args), events.append)
        self.assertEqual(reports, expected)
        self.assertEqual(events, [{'e': 'connected', 's': 0, 'mac': 'unchanged'}])


# Actual helper main functions, with fake radio APIs only. No controller, BlueZ,
# adapter or privilege changes. EOF and output-failure cleanup are observable.
HELPER_FIXTURE = r'''
import asyncio, importlib, json, os, pathlib, sys, types
from gc_controller.ble import pipe_output
helper_name, marker, mode = sys.argv[1:]
sys.argv = [helper_name]
record = []
class Backend:
    async def open(self, *args): record.append(['open', list(args)])
    async def close(self):
        record.append(['close'])
        pathlib.Path(marker).write_text(json.dumps(record))
    async def scan_and_connect(self, **kwargs):
        record.append(['connect', kwargs['target_address'], kwargs['slot_index']])
        # Oversized producer burst exhausts a deliberately tiny test limit.
        for i in range(40 if mode == 'overflow' else 1):
            kwargs['data_queue'].put_nowait(bytes([i]) * 63)
        return kwargs['target_address']
for name, cls in [('bleak_backend', 'BleakBackend'), ('bumble_backend', 'BumbleBackend')]:
    module = types.ModuleType('gc_controller.ble.' + name)
    setattr(module, cls, Backend)
    sys.modules[module.__name__] = module
import gc_controller.ble
# Never run the real privileged Linux operations.
gc_controller.ble.stop_bluez = lambda: record.append(['stop_bluez'])
gc_controller.ble.find_hci_adapter = lambda: 0
if mode == 'overflow':
    original = pipe_output.PipeWriter
    pipe_output.PipeWriter = lambda fd: original(fd, max_messages=1)
helper = importlib.import_module('gc_controller.ble.' + helper_name)
helper.main()
'''


class HelperProcessTests(unittest.TestCase):
    def launch(self, name, marker, mode='normal'):
        env = dict(os.environ, PYTHONPATH=str(SRC.parent))
        proc = subprocess.Popen([sys.executable, '-u', '-c', HELPER_FIXTURE,
                                 name, str(marker), mode], env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream: stream.close()
        self.addCleanup(cleanup)
        return proc

    def test_normal_open_shutdown_and_eof_preserve_command_contract(self):
        for name in HELPERS:
            for ending in (b'', b'{"cmd":"shutdown"}\n'):
                with self.subTest(helper=name, ending=ending), tempfile.TemporaryDirectory() as directory:
                    marker = Path(directory) / 'cleanup.json'
                    proc = self.launch(name, marker)
                    data, error = proc.communicate(b'{"cmd":"stop_bluez"}\n{"cmd":"open"}\n' + ending, timeout=6)
                    self.assertEqual(proc.returncode, 0, error)
                    self.assertEqual([json.loads(line)['e'] for line in data.splitlines()],
                                     ['ready', 'bluez_stopped', 'open_ok'])
                    expected = ([['stop_bluez'], ['open', [0]], ['close']]
                                if name == 'ble_subprocess' else [['open', []], ['close']])
                    self.assertEqual(json.loads(marker.read_text()), expected)

    def test_broken_stdout_exits_nonzero_and_closes_backend(self):
        for name in HELPERS:
            with self.subTest(helper=name), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / 'cleanup.json'
                proc = self.launch(name, marker)
                # Close before any output: even the ready event may fail.
                proc.stdout.close()
                proc.stdout = None
                _, error = proc.communicate(b'{"cmd":"open"}\n', timeout=6)
                self.assertNotEqual(proc.returncode, 0, error)
                self.assertEqual(json.loads(marker.read_text()).count(['close']), 1)

    def test_output_overflow_exits_without_waiting_for_parent_stdin_eof(self):
        for name in HELPERS:
            with self.subTest(helper=name), tempfile.TemporaryDirectory() as directory:
                marker = Path(directory) / 'cleanup.json'
                proc = self.launch(name, marker, 'overflow')
                # One command; keep stdin OPEN to prove output failure wakes it.
                proc.stdin.write(b'{"cmd":"scan_connect","slot_index":2,"target_address":"target"}\n')
                proc.stdin.flush()
                proc.wait(timeout=6)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(json.loads(marker.read_text()).count(['close']), 1)

    def test_real_full_os_pipe_has_bounded_stall_shutdown(self):
        code = '''
import sys, threading
from gc_controller.ble.pipe_output import PipeWriter
failed = threading.Event()
writer = PipeWriter(sys.stdout.buffer.fileno(), stall_timeout=0.1)
writer.on_failure = lambda error: failed.set()
# A single large message fills the actual OS pipe; the parent does not read.
writer.submit(b'x' * (128 * 1024))
if not failed.wait(3): raise SystemExit(3)
if not isinstance(writer.error, TimeoutError): raise SystemExit(4)
writer.close(timeout=0.01)
raise SystemExit(17)
'''
        proc = subprocess.Popen([sys.executable, '-c', code],
                                env=dict(os.environ, PYTHONPATH=str(SRC.parent)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(proc.wait(timeout=5), 17)
        finally:
            if proc.poll() is None: proc.kill()
            proc.wait(timeout=5)
            proc.stdout.close()
            proc.stderr.close()
