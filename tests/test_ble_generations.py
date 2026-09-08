"""Wire epochs survive buffered traffic and UI/transport slot reassignment."""
import asyncio
import base64
import io
import json
import queue
import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import Mock

from _support import SRC, load_module, load_definitions
from test_ble_child_runtime import FakeBackend, FakeOutput

ipc = load_module('ble/ipc.py')
sessions = load_module('ble/sessions.py')
runtime = load_module('ble/child_runtime.py')
parent = load_module('ble/parent.py')


def connected(command, mac='first'):
    return {'e': 'connected', 's': command['slot_index'], 'g': command['g'], 'mac': mac}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.router = sessions.SessionRouter(max_pending=2)

    def connect(self, ui=0, mac='first'):
        cmd = self.router.prepare({'cmd': 'connect_device', 'slot_index': ui, 'address': mac})
        event = self.router.event(connected(cmd, mac))
        return cmd, event

    def test_initial_reports_wait_for_exact_consumer_and_preserve_press_release(self):
        command, event = self.connect()
        self.router.data(command['slot_index'], command['g'], b'press')
        self.router.data(command['slot_index'], command['g'], b'release')
        received = []
        self.assertTrue(self.router.bind(event, 3, received.append))
        self.assertEqual(received, [b'press', b'release'])
        self.router.data(command['slot_index'], command['g'], b'next')
        self.assertEqual(received[-1], b'next')

    def test_reused_slot_rejects_already_buffered_old_events_and_reports(self):
        old, event = self.connect()
        self.router.data(old['slot_index'], old['g'], b'old')
        new, new_event = self.connect(mac='second')
        self.assertEqual(new['slot_index'], old['slot_index'])
        self.assertGreater(new['g'], old['g'])
        received = []
        self.router.bind(new_event, 0, received.append)
        for kind in ('status', 'connected', 'connect_error', 'disconnected'):
            self.assertIsNone(self.router.event({**event, 'e': kind}))
        self.router.data(old['slot_index'], old['g'], b'stale release')
        self.router.data(new['slot_index'], new['g'], b'current')
        self.assertEqual(received, [b'current'])

    def test_moved_player_slot_does_not_free_its_still_owned_wire_slot(self):
        first, event = self.connect()
        a, b = [], []
        self.router.bind(event, 3, a.append)
        second, second_event = self.connect(ui=0, mac='second')
        self.assertNotEqual(first['slot_index'], second['slot_index'])
        self.assertEqual(second_event['s'], 0)
        self.router.bind(second_event, 0, b.append)
        self.router.data(first['slot_index'], first['g'], b'first')
        self.router.data(second['slot_index'], second['g'], b'second')
        self.assertEqual(a, [b'first']); self.assertEqual(b, [b'second'])
        loss = self.router.event({**connected(first), 'e': 'disconnected'})
        self.assertEqual(loss['s'], 3)
        rumble = self.router.prepare({'cmd': 'rumble', 'slot_index': 3, 'address': 'FIRST/P'})
        self.assertEqual((rumble['slot_index'], rumble['g']), (first['slot_index'], first['g']))

    def test_feedback_follows_installed_player_slot_without_address(self):
        command, event = self.connect()
        self.router.bind(event, 3, Mock())
        feedback = self.router.prepare({'cmd': 'set_led', 'slot_index': 3, 'new_slot_index': 3})
        self.assertEqual(feedback['slot_index'], command['slot_index'])
        self.assertEqual(feedback['g'], command['g'])

    def test_disconnect_invalidates_callbacks_before_command_is_written(self):
        command, event = self.connect()
        self.router.bind(event, 3, Mock())
        self.assertEqual(self.router.prepare({'cmd': 'disconnect', 'slot_index': 3})['g'], command['g'])
        self.assertIsNone(self.router.event(event))
        self.assertIsNone(self.router.prepare({'cmd': 'rumble', 'slot_index': 3}))

    def test_cancel_includes_connected_but_not_installed_generation(self):
        pending, event = self.connect()
        live, live_event = self.connect(1, 'second')
        self.router.bind(live_event, 1, Mock())
        cancel = self.router.prepare({'cmd': 'cancel_all_scans'})
        self.assertEqual(cancel['cancel_generations'], [pending['g']])
        self.assertIsNone(self.router.event(event))
        self.assertIsNotNone(self.router.event(live_event))

    def test_initial_backlog_overflow_is_not_silent_loss(self):
        command, _ = self.connect()
        for _ in range(2): self.router.data(0, command['g'], bytes(64))
        with self.assertRaises(BufferError): self.router.data(0, command['g'], bytes(64))

    def test_bound_queue_overflow_propagates(self):
        command, event = self.connect()
        self.router.bind(event, 0, Mock(side_effect=queue.Full))
        with self.assertRaises(queue.Full): self.router.data(0, command['g'], bytes(64))

    def test_input_before_connected_is_invalid(self):
        command = self.router.prepare({'cmd': 'scan_connect', 'slot_index': 0})
        with self.assertRaisesRegex(ValueError, 'before Connected'):
            self.router.data(0, command['g'], bytes(64))

    def test_terminal_old_event_cannot_retire_replacement(self):
        old, event = self.connect()
        new, new_event = self.connect(mac='second')
        self.router.finish({**event, 'e': 'connect_error'})
        self.assertIsNotNone(self.router.event(new_event))

    def test_scan_epochs_do_not_steal_ready_controller(self):
        connection, event = self.connect()
        self.router.bind(event, 0, Mock())
        scan = self.router.prepare({'cmd': 'scan_start', 'slot_index': 0})
        self.assertIsNotNone(self.router.event(event))
        found = {'e': 'device_detected', 's': 0, 'g': scan['g'], 'device': {}}
        self.assertIsNotNone(self.router.event(found))
        self.router.prepare({'cmd': 'scan_stop'})
        self.assertIsNone(self.router.event(found))
        self.assertIsNotNone(self.router.event(event))

    def test_all_four_wire_slots_can_remain_owned_after_reassignment(self):
        for ui in range(4):
            command, event = self.connect(ui, str(ui))
            self.router.bind(event, (ui + 1) % 4, Mock())
        with self.assertRaises(BufferError): self.connect(0, 'fifth')

    def test_gui_rechecks_epoch_at_actual_callback_delivery(self):
        old, event = self.connect()
        self.connect(mac='second')
        method = load_definitions('app.py', {'_dispatch_ble_event'},
                                  {'normalize_ble_address': sessions.address},
                                  class_name='GCControllerEnabler')['_dispatch_ble_event']
        proc = object()
        app = types.SimpleNamespace(_ble_subprocess=proc,
                                    _ble_commands=types.SimpleNamespace(router=self.router),
                                    _handle_ble_event=Mock(), _ble_service_lost=Mock())
        method(app, proc, event)
        app._handle_ble_event.assert_not_called()
        app._ble_service_lost.assert_not_called()


class EpochFramingTests(unittest.TestCase):
    def test_rejects_missing_zero_negative_boolean_and_oversized_generations(self):
        for generation in (None, 0, -1, True, 2**64, '1'):
            raw = json.dumps({'e': 'status', 's': 0, 'g': generation}).encode() + b'\n'
            with self.subTest(generation=generation), self.assertRaises(ipc.ProtocolError):
                ipc.read_event_stream(io.BytesIO(raw), Mock(), Mock())
        with self.assertRaises(ipc.ProtocolError):
            ipc.read_event_stream(io.BytesIO(b'\xfe' + ipc.INPUT_HEADER.pack(0, 0) + bytes(64)), Mock(), Mock())

    def test_legacy_frames_and_wrong_ready_version_fail_immediately(self):
        for raw in (b'\xff' + bytes(65), b'{"e":"ready"}\n', b'{"e":"ready","protocol":1}\n'):
            with self.subTest(raw=raw), self.assertRaises(ipc.ProtocolError):
                ipc.read_event_stream(io.BytesIO(raw), Mock(), Mock())

    def test_all_64_generation_bits_round_trip_without_json_float_conversion(self):
        received = []
        generation = 2**64 - 1
        raw = b'\xfe' + ipc.INPUT_HEADER.pack(3, generation) + bytes(64)
        ipc.read_event_stream(io.BytesIO(raw), lambda *args: received.append(args), Mock())
        self.assertEqual(received, [(3, generation, bytes(64))])

    def test_wire_events_cannot_spoof_internal_routing_metadata(self):
        with self.assertRaises(ipc.ProtocolError):
            ipc.read_event_stream(io.BytesIO(b'{"e":"status","s":0,"g":1,"_wire":1}\n'), Mock(), Mock())


class ChildEpochTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend, self.output = FakeBackend(), FakeOutput()
        self.runner = runtime.ChildRunner(self.backend, self.output)

    async def asyncTearDown(self):
        await self.runner._stop_scan()
        for slot in list(self.runner.sessions): await self.runner._retire(slot)

    async def connect(self, generation):
        await self.runner.command({'cmd': 'connect_device', 'slot_index': 0, 'g': generation, 'address': 'first'})
        await self.runner.sessions[0].task

    async def test_replayed_connect_cannot_replace_current_session(self):
        await self.connect(7)
        old = self.runner.sessions[0]
        for generation in (1, 7):
            with self.assertRaisesRegex(ValueError, 'Replayed'): await self.connect(generation)
            self.assertIs(self.runner.sessions[0], old)

    async def test_old_feedback_and_disconnect_cannot_affect_reconnected_same_address(self):
        await self.connect(1)
        await self.connect(2)
        current = self.runner.sessions[0]
        for action in ('rumble', 'disconnect'):
            await self.runner.command({'cmd': action, 'slot_index': 0, 'g': 1, 'address': 'first',
                                       'data': base64.b64encode(b'xyz').decode()})
        await asyncio.sleep(0)
        self.assertIs(self.runner.sessions[0], current)
        self.backend.send_rumble.assert_not_awaited()

    async def test_cancel_can_retire_a_child_ready_session_not_installed_by_parent(self):
        await self.connect(1)
        self.assertTrue(self.runner.sessions[0].ready)
        await self.runner.command({'cmd': 'cancel_all_scans', 'cancel_generations': [1]})
        self.assertFalse(self.runner.sessions)
        self.assertIn('disconnect:first', self.backend.cleanup)

    async def test_missing_generation_and_unbounded_cancel_list_fail_closed(self):
        with self.assertRaises(ValueError):
            await self.runner.command({'cmd': 'connect_device', 'slot_index': 0, 'address': 'first'})
        with self.assertRaises(ValueError):
            await self.runner.command({'cmd': 'cancel_all_scans', 'cancel_generations': [1] * 5})


class RealChildEpochTests(unittest.TestCase):
    def test_real_child_roundtrip_reuses_wire_slot_without_delivering_old_buffered_input(self):
        code = f'''import sys
sys.path.insert(0, {str(SRC.parent)!r})
from gc_controller.ble.child_runtime import run_subprocess
class Backend:
    async def connect_device(self, address, **kwargs):
        kwargs['data_queue'].put_nowait(bytes([1 if address == 'first' else 2]) * 64)
        return address
    async def disconnect(self, identifier): pass
    async def close(self): pass
run_subprocess(Backend())
'''
        proc = subprocess.Popen([sys.executable, '-u', '-c', code], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        errors, events = [], queue.Queue()
        transport = parent.CommandTransport(proc, lambda p, e: errors.append(e), grace_timeout=2)
        def read():
            try:
                def event(value):
                    value = transport.router.event(value)
                    if value is not None: events.put(value)
                ipc.read_event_stream(proc.stdout, transport.router.data, event)
            except Exception as exc:
                errors.append(str(exc))
        reader = threading.Thread(target=read, daemon=True); reader.start()
        try:
            self.assertEqual(events.get(timeout=4), {'e': 'ready', 'protocol': 2})
            transport.send({'cmd': 'connect_device', 'slot_index': 0, 'address': 'first'})
            old = events.get(timeout=4)
            # Do not install the first consumer: its initial report remains buffered.
            transport.send({'cmd': 'connect_device', 'slot_index': 0, 'address': 'second'})
            new = events.get(timeout=4)
            self.assertGreater(new['g'], old['g'])
            self.assertIsNone(transport.router.event(old))
            received = queue.Queue()
            transport.router.bind(new, 3, received.put_nowait)
            self.assertEqual(received.get(timeout=4), bytes([2]) * 64)
            self.assertTrue(received.empty())
            transport.send({'cmd': 'shutdown'})
            self.assertTrue(transport.close(wait=True))
            reader.join(3)
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
        finally:
            if proc.poll() is None: proc.kill(); proc.wait(timeout=3)
            transport.close(wait=True); reader.join(3)
            proc.stdout.close(); proc.stderr.close()
