import io
import logging
import queue
import threading
import types
import unittest
from unittest.mock import Mock
from _support import load_definitions, load_module

ipc = load_module('ble/ipc.py')
sessions = load_module('ble/sessions.py')


def packet(slot=0, payload=None, generation=1):
    return b'\xfe' + ipc.INPUT_HEADER.pack(slot, generation) + (payload or bytes(range(64)))


class Fragmented(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3) if size >= 0 else 3)


class FramingTests(unittest.TestCase):
    def parse(self, raw, stream=io.BytesIO):
        data, events = [], []
        ipc.read_event_stream(stream(raw), lambda si, generation, value: data.append((si, generation, value)), events.append)
        return data, events

    def test_interleaved_events_and_fragmented_binary_payloads(self):
        raw = b'{"e":"connected","s":0,"g":1}\n' + packet() + b'{"e":"disconnected","s":0,"g":1}\n'
        data, events = self.parse(raw, Fragmented)
        self.assertEqual(data, [(0, 1, bytes(range(64)))])
        self.assertEqual([e['e'] for e in events], ['connected', 'disconnected'])

    def test_payload_delimiters_have_no_special_meaning(self):
        payload = (b'\xff\n{}' * 16)
        self.assertEqual(self.parse(packet(3, payload))[0], [(3, 1, payload)])

    def test_every_truncated_binary_packet_is_rejected(self):
        raw = packet()
        for length in range(1, len(raw)):
            with self.subTest(length=length), self.assertRaises(EOFError):
                self.parse(raw[:length])

    def test_malformed_json_and_invalid_slots_fail_closed(self):
        for raw in (b'[]\n', b'null\n', b'{}\n', b'{bad}\n', b'\xff\n',
                    b'{"e":"status","s":-1}\n', b'{"e":"status","s":true}\n',
                    b'{"e":"status","s":4}\n', packet(4)):
            with self.subTest(raw=raw[:30]), self.assertRaises(ipc.ProtocolError):
                self.parse(raw)

    def test_unterminated_and_overlong_json_are_rejected(self):
        with self.assertRaises(EOFError): self.parse(b'{"e":"ready"}')
        with self.assertRaises(ipc.ProtocolError): self.parse(b' ' * (ipc.MAX_JSON_BYTES + 1))

    def test_callback_failure_propagates_instead_of_dropping_input(self):
        callback = Mock(side_effect=queue.Full)
        with self.assertRaises(queue.Full):
            ipc.read_event_stream(io.BytesIO(packet()), callback, Mock())


class ParentOwnershipTests(unittest.TestCase):
    def gui(self, raw):
        names = {'_ble_event_reader', '_dispatch_ble_event', '_ble_service_lost'}
        methods = load_definitions('app.py', names,
                                  {'read_event_stream': ipc.read_event_stream,
                                   'logger': logging.getLogger('test'),
                                   'normalize_ble_address': sessions.address},
                                  class_name='GCControllerEnabler')
        obj = types.SimpleNamespace(_ble_subprocess=types.SimpleNamespace(stdout=io.BytesIO(raw)),
                                    _ble_initialized=True, _ble_slot_remap={},
                                    _ble_init_event=threading.Event(),
                                    slots=[types.SimpleNamespace(ble_data_queue=queue.Queue(maxsize=1))],
                                    _handle_ble_event=Mock(), calls=[])
        router = sessions.SessionRouter()
        router.prepare({'cmd': 'connect_device', 'slot_index': 0, 'address': 'first'})
        ready = router.event({'e': 'connected', 's': 0, 'g': 1, 'mac': 'first'})
        router.bind(ready, 0, obj.slots[0].ble_data_queue.put_nowait)
        obj._ble_commands = types.SimpleNamespace(router=router)
        for name in names: setattr(obj, name, types.MethodType(methods[name], obj))
        obj._call_on_ui_thread = lambda fn, *args: obj.calls.append((fn, args))
        return obj

    def test_gui_eof_wakes_init_and_schedules_service_loss(self):
        obj = self.gui(b'')
        obj._ble_event_reader(obj._ble_subprocess)
        self.assertTrue(obj._ble_init_event.is_set())
        self.assertEqual(obj._ble_init_result['ctx'], 'ipc')
        self.assertEqual(obj.calls[-1][0].__name__, '_ble_service_lost')

    def test_old_gui_callbacks_cannot_mutate_replacement_process(self):
        obj = self.gui(b'{"e":"disconnected","s":0,"g":1}\n')
        old = obj._ble_subprocess
        obj._ble_event_reader(old)
        obj._ble_subprocess = object()
        for fn, args in obj.calls: fn(*args)
        obj._handle_ble_event.assert_not_called()

    def test_full_gui_queue_reports_loss_without_blocking(self):
        obj = self.gui(packet() + packet())
        with self.assertLogs('test', level='WARNING'):
            obj._ble_event_reader(obj._ble_subprocess)
        self.assertIn('Full', obj._ble_init_result['msg'])
        self.assertEqual(obj.slots[0].ble_data_queue.qsize(), 1)

    def test_headless_eof_and_callback_failure_report_loss(self):
        method = load_definitions('app.py', {'_event_reader'},
                                 {'read_event_stream': ipc.read_event_stream,
                                  'logger': logging.getLogger('test')},
                                 class_name='_BleHeadlessManager')['_event_reader']
        for raw in (b'', packet()):
            obj = types.SimpleNamespace(_subprocess=types.SimpleNamespace(stdout=io.BytesIO(raw)),
                                        _initialized=True, _init_event=threading.Event())
            router = sessions.SessionRouter()
            router.prepare({'cmd': 'connect_device', 'slot_index': 0, 'address': 'first'})
            ready = router.event({'e': 'connected', 's': 0, 'g': 1, 'mac': 'first'})
            router.bind(ready, 0, Mock(side_effect=BufferError))
            obj._commands = types.SimpleNamespace(router=router)
            events = []
            with unittest.mock.patch.object(logging.getLogger('test'), 'warning'):
                method(obj, Mock(side_effect=BufferError), events.append, obj._subprocess)
            self.assertEqual(events[-1]['e'], 'service_lost')
            self.assertIs(events[-1]['_process'], obj._subprocess)
            self.assertFalse(obj._initialized)
            self.assertTrue(obj._init_event.is_set())

    def test_retired_headless_reader_cannot_emit_loss_for_new_process(self):
        method = load_definitions('app.py', {'_event_reader'},
                                 {'read_event_stream': ipc.read_event_stream,
                                  'logger': logging.getLogger('test')},
                                 class_name='_BleHeadlessManager')['_event_reader']
        obj = types.SimpleNamespace(_subprocess=object(), _initialized=True, _commands=None)
        data, events = Mock(), Mock()
        method(obj, data, events, types.SimpleNamespace(stdout=io.BytesIO(packet())))
        data.assert_not_called(); events.assert_not_called()

    def test_service_loss_retires_ble_but_leaves_usb_untouched(self):
        obj = self.gui(b'')
        ble = types.SimpleNamespace(ble_connected=True, connection_mode='ble',
                                    input_proc=Mock(), emu_mgr=Mock(), ble_data_queue=queue.Queue())
        ble.stop_emulation = ble.emu_mgr.stop
        ble.ble_data_queue.put(b'stale')
        usb = types.SimpleNamespace(ble_connected=False, connection_mode='usb',
                                    input_proc=Mock(), emu_mgr=Mock())
        obj.slots = [ble, usb]
        obj.ui = Mock(slots=[Mock(), Mock()])
        obj._ble_pair_mode = {0: 'autoscan'}
        obj._cleanup_ble = Mock()
        obj._stop_auto_scan = Mock()
        obj._reset_rumble = Mock()
        with self.assertLogs('test', level='WARNING'):
            obj._ble_service_lost(obj._ble_subprocess, 'lost')
        ble.input_proc.stop.assert_called_once()
        ble.emu_mgr.stop.assert_called_once()
        self.assertFalse(ble.ble_connected)
        self.assertTrue(ble.ble_data_queue.empty())
        usb.input_proc.stop.assert_not_called()
        usb.emu_mgr.stop.assert_not_called()
        self.assertFalse(obj._auto_scan_pending)
        self.assertEqual(obj._ble_pair_mode, {})
