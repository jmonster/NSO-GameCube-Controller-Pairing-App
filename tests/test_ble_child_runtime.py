import asyncio
import base64
import io
import queue
import threading
import unittest
from unittest.mock import AsyncMock

from _support import load_module

output_module = load_module('ble/output.py')
runtime = load_module('ble/child_runtime.py')


class WriterTests(unittest.TestCase):
    def test_partial_and_interrupted_writes_preserve_json_input_order(self):
        result = bytearray()
        interrupted = [False]
        def write(fd, view):
            if not interrupted[0]:
                interrupted[0] = True
                raise InterruptedError
            count = min(3, len(view))
            result.extend(view[:count])
            return count
        errors = []
        writer = output_module.OutputWriter(123, errors.append, write=write)
        report = bytearray(64)
        writer.event({'e': 'ready', 'protocol': 2})
        writer.data(2, report, 1)
        report[0] = 9
        writer.event({'e': 'disconnected', 's': 2, 'g': 1})
        self.assertTrue(writer.close(2))
        events, data = [], []
        ipc = load_module('ble/ipc.py')
        ipc.read_event_stream(io.BytesIO(result), lambda s, g, d: data.append((s, g, d)), events.append)
        self.assertEqual(events, [{'e': 'ready', 'protocol': 2}, {'e': 'disconnected', 's': 2, 'g': 1}])
        self.assertEqual(data, [(2, 1, bytes(64))])
        self.assertEqual(errors, [])

    def test_stalled_writer_does_not_block_producers_or_close(self):
        entered, release = threading.Event(), threading.Event()
        def write(fd, view):
            entered.set()
            release.wait(2)
            return len(view)
        errors = []
        writer = output_module.OutputWriter(123, errors.append, max_frames=1, write=write)
        try:
            writer.put(b'first')
            self.assertTrue(entered.wait(1))
            writer.put(b'second')
            with self.assertRaises(ConnectionError):
                writer.put(b'overflow')
            self.assertFalse(writer.close(0))
            self.assertEqual(len(errors), 1)
            with self.assertRaises(ConnectionError):
                writer.put(b'after failure')
        finally:
            release.set()
            self.assertTrue(writer.close(2))

    def test_byte_limit_is_enforced_before_enqueue(self):
        errors = []
        writer = output_module.OutputWriter(123, errors.append, max_bytes=2,
                                            write=lambda fd, view: len(view))
        with self.assertRaises(ConnectionError):
            writer.put(b'abc')
        writer.close()
        self.assertEqual(len(errors), 1)

    def test_zero_write_and_broken_pipe_signal_failure(self):
        for error in (None, BrokenPipeError('parent exited')):
            with self.subTest(error=error):
                failed = threading.Event()
                def write(fd, view):
                    if error:
                        raise error
                    return 0
                writer = output_module.OutputWriter(123, lambda e: failed.set(), write=write)
                writer.put(b'hello')
                self.assertTrue(failed.wait(1))
                self.assertTrue(writer.close())

    def test_oversized_event_is_fatal(self):
        errors = []
        writer = output_module.OutputWriter(123, errors.append, write=lambda fd, view: len(view))
        with self.assertRaises(ValueError):
            writer.event({'e': 'status', 'msg': 'x' * 65536})
        writer.close()
        self.assertEqual(len(errors), 1)

    def test_invalid_input_is_not_silently_padded_or_truncated(self):
        writer = output_module.OutputWriter(123, lambda e: None, write=lambda fd, view: len(view))
        try:
            for slot, report in ((-1, bytes(64)), (4, bytes(64)), (True, bytes(64)),
                                 (0, bytes(63)), (0, bytes(65))):
                with self.assertRaises(ValueError):
                    writer.data(slot, report, 1)
        finally:
            writer.close()


class FakeOutput:
    def __init__(self):
        self.messages = []

    def event(self, event):
        self.messages.append(event)

    def data(self, slot, report, generation):
        self.messages.append({'data': bytes(report), 's': slot, 'g': generation})


class FakeBackend:
    def __init__(self):
        self.connects = []
        self.cleanup = []
        self.gate = None
        self.disconnected_during_init = False
        self.callbacks = []
        self.close = AsyncMock()
        self.stop_scan = AsyncMock()
        self.open = AsyncMock()
        self.send_rumble = AsyncMock()
        self.set_led = AsyncMock()
        self.scan_only = AsyncMock(return_value=[])

    async def connect_device(self, address, **kwargs):
        self.connects.append(address)
        self.callbacks.append(kwargs)
        try:
            kwargs['data_queue'].put_nowait(bytes(64))
            if self.disconnected_during_init:
                kwargs['on_disconnect']()
            if self.gate:
                await self.gate.wait()
            return address
        finally:
            self.cleanup.append(address)

    async def disconnect(self, identifier):
        self.cleanup.append('disconnect:' + identifier)
        await asyncio.sleep(0)


class ChildSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend, self.output = FakeBackend(), FakeOutput()
        self.runner = runtime.ChildRunner(self.backend, self.output)

    async def command(self, cmd):
        """Generate valid v2 commands for the pre-existing lifecycle scenarios."""
        cmd = dict(cmd)
        if cmd['cmd'] in {'connect_device', 'scan_connect', 'scan_start', 'scan_devices'}:
            cmd.setdefault('g', self.runner._last_generation + 1)
        elif cmd['cmd'] in {'disconnect', 'rumble', 'set_led'}:
            wanted = runtime._address(cmd.get('address'))
            session = next((s for s in self.runner.sessions.values()
                            if (runtime._address(s.identifier) == wanted if wanted
                                else s.slot == cmd.get('slot_index'))), None)
            cmd.setdefault('g', session.generation if session else 1)
        return await self.runner.command(cmd)

    async def asyncTearDown(self):
        await self.runner._stop_scan()
        for slot in list(self.runner.sessions):
            await self.runner._retire(slot)

    async def test_failed_bluez_takeover_cannot_publish_success(self):
        for result in (False, None):
            with self.subTest(result=result):
                self.output.messages.clear()
                self.runner.stop_bluez = lambda: result
                await self.command({'cmd': 'stop_bluez'})
                self.assertEqual([m['e'] for m in self.output.messages], ['error'])
                self.assertEqual(self.output.messages[0]['ctx'], 'stop_bluez')
        def fail():
            raise RuntimeError('permission denied')
        self.output.messages.clear()
        self.runner.stop_bluez = fail
        await self.command({'cmd': 'stop_bluez'})
        self.assertIn('permission denied', self.output.messages[0]['msg'])
        self.runner.stop_bluez = lambda: True
        await self.command({'cmd': 'stop_bluez'})
        self.assertEqual(self.output.messages[-1], {'e': 'bluez_stopped'})

    async def connect(self, address='first', slot=0):
        await self.command({'cmd': 'connect_device', 'slot_index': slot, 'address': address})
        session = self.runner.sessions[slot]
        await session.task
        return session

    async def test_connected_event_precedes_initial_data(self):
        await self.connect()
        self.assertEqual(self.output.messages[0]['e'], 'connected')
        self.assertEqual(self.output.messages[1], {'s': 0, 'g': 1, 'data': bytes(64)})

    async def test_stale_disconnect_status_and_data_cannot_affect_replacement(self):
        await self.connect('first')
        old = self.backend.callbacks[-1]
        await self.connect('second')
        count = len(self.output.messages)
        old['on_disconnect']()
        old['on_status']('stale')
        old['data_queue'].put_nowait(bytes(64))
        self.assertEqual(len(self.output.messages), count)
        self.assertEqual(self.runner.sessions[0].identifier, 'second')

    async def test_disconnect_during_init_reports_connection_failure_not_connected(self):
        self.backend.disconnected_during_init = True
        await self.connect()
        self.assertEqual([m.get('e') for m in self.output.messages], ['connect_error'])
        self.assertFalse(self.runner.sessions)

    async def test_cancellation_finishes_before_replacement_starts(self):
        self.backend.gate = asyncio.Event()
        await self.command({'cmd': 'connect_device', 'slot_index': 0, 'address': 'first'})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.backend.gate = None
        await self.connect('second')
        self.assertIn('first', self.backend.cleanup)
        self.assertIn('disconnect:first', self.backend.cleanup)
        self.assertNotIn('disconnected', [m.get('e') for m in self.output.messages])

    async def test_duplicate_target_does_not_disconnect_other_slot(self):
        first = await self.connect('AA:BB:CC:DD:EE:FF/P')
        await self.command({'cmd': 'connect_device', 'slot_index': 1,
                                   'address': 'aa:bb:cc:dd:ee:ff'})
        self.assertIs(self.runner.sessions[0], first)
        self.assertNotIn(1, self.runner.sessions)
        self.assertFalse(any(v.startswith('disconnect:') for v in self.backend.cleanup))

    async def test_cancel_all_scans_preserves_ready_connections(self):
        first = await self.connect()
        await self.command({'cmd': 'cancel_all_scans'})
        self.assertIs(self.runner.sessions[0], first)

    async def test_eof_and_invalid_commands_always_close_backend(self):
        for command in (None, {'cmd': 'bogus'}, {'cmd': 'connect_device', 'slot_index': -1}):
            with self.subTest(command=command):
                commands = queue.Queue()
                commands.put(command)
                if command is None:
                    await self.runner.run(commands)
                else:
                    with self.assertRaises(ValueError):
                        await self.runner.run(commands)
                self.backend.close.assert_awaited()

    async def test_cancelling_reader_wakes_executor_and_closes_backend(self):
        commands = queue.Queue()
        task = asyncio.create_task(self.runner.run(commands))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.backend.close.assert_awaited_once()

    async def test_feedback_targets_owning_slot(self):
        await self.connect('first', 0)
        await self.connect('second', 1)
        await self.command({'cmd': 'rumble', 'slot_index': 1,
                                   'data': base64.b64encode(b'xyz').decode()})
        await asyncio.sleep(0)
        self.backend.send_rumble.assert_awaited_once_with('second', b'xyz')

    async def test_shutdown_cleans_connected_sessions(self):
        await self.connect()
        commands = queue.Queue()
        commands.put({'cmd': 'shutdown'})
        await self.runner.run(commands)
        self.assertFalse(self.runner.sessions)
        self.backend.close.assert_awaited_once()

    async def test_reassigned_ui_slot_feedback_uses_address_not_other_controller(self):
        await self.connect('first', 0)
        await self.connect('second', 1)
        await self.command({'cmd': 'rumble', 'slot_index': 1, 'address': 'FIRST',
                                  'data': base64.b64encode(b'xyz').decode()})
        await asyncio.sleep(0)
        self.backend.send_rumble.assert_awaited_once_with('first', b'xyz')

    async def test_reassigned_ui_slot_disconnect_retires_only_address_owner(self):
        first = await self.connect('first', 0)
        second = await self.connect('second', 1)
        await self.command({'cmd': 'disconnect', 'slot_index': 1, 'address': 'first'})
        self.assertNotIn(0, self.runner.sessions)
        self.assertIs(self.runner.sessions[1], second)
        self.assertNotIn('disconnect:second', self.backend.cleanup)
        await self.command({'cmd': 'disconnect', 'slot_index': 1, 'address': 'first'})
        self.assertIs(self.runner.sessions[1], second)

    async def test_cancel_then_start_scan_awaits_pending_connection_but_keeps_ready_controller(self):
        ready = await self.connect('ready', 1)
        self.backend.gate = asyncio.Event()
        self.backend.start_scan = AsyncMock()
        await self.command({'cmd': 'connect_device', 'slot_index': 0, 'address': 'pending'})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await self.command({'cmd': 'cancel_all_scans'})
        await self.command({'cmd': 'scan_start', 'slot_index': 0})
        await self.runner.scan_task
        self.assertNotIn(0, self.runner.sessions)
        self.assertIs(self.runner.sessions[1], ready)
        self.assertIn('pending', self.backend.cleanup)
