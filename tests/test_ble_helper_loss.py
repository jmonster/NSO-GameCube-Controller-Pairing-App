"""Failure handling on the existing wire protocol, without a radio or Tk display."""
import ast
import io
import json
import queue
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock

from _support import SRC, fake_module, load_definitions, load_module

reader = load_module('ble/reader.py')


def production_class(name):
    tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef)]
    namespace = dict(threading=threading, time=time, queue=queue, json=json,
                     subprocess=subprocess, logger=Mock(), t=lambda key: key,
                     read_events=reader.read_events, reap_helper=Mock())
    definitions = load_definitions('app.py', names, namespace, class_name=name)
    return type(name, (), {n: definitions[n] for n in names}), definitions


def nested_handler(name, values):
    """Bind a real run_headless closure without executing hardware startup."""
    code = compile((SRC / 'app.py').read_text(encoding='utf-8'), 'app.py', 'exec')
    outer = next(c for c in code.co_consts if isinstance(c, types.CodeType) and c.co_name == 'run_headless')
    inner = next(c for c in outer.co_consts if isinstance(c, types.CodeType) and c.co_name == name)
    def cell(value):
        return (lambda: value).__closure__[0]
    closure = tuple(cell(values.get(n)) for n in inner.co_freevars)
    return types.FunctionType(inner, dict(logger=Mock(), **values), closure=closure)


def helper(payload=b''):
    return Mock(stdout=io.BytesIO(payload), stdin=Mock(), poll=Mock(return_value=None))


def gui(proc, initialized=True):
    cls, definitions = production_class('GCControllerEnabler')
    app = object.__new__(cls)
    app._ble_subprocess, app._ble_lock = proc, threading.RLock()
    app._ble_initialized, app._ble_init_in_progress = initialized, not initialized
    app._ble_events = queue.Queue(maxsize=256)
    app._ble_init_event, app._ble_init_result = threading.Event(), None
    app._ble_loss, app._ble_service_failed, app._closing = None, False, False
    app._ble_init_attempt = object()
    app._ble_slot_remap, app._ble_pair_mode = {}, {}
    app._diff_scan_callback, app._scan_stream_callback = {}, {}
    app._auto_scan_pending, app._auto_scan_slot = True, 0
    app._latest_ui_data = [None] * 4
    app.root, app.ui = Mock(), Mock(slots=[Mock() for _ in range(4)])
    app._stop_auto_scan, app._stop_rumble_pwm = Mock(), Mock()
    app._handle_ble_event, app._start_auto_scan = Mock(), Mock()
    app.slots = []
    for mode in ('ble', 'usb', 'ble', 'usb'):
        app.slots.append(types.SimpleNamespace(
            connection_mode=mode, ble_connected=mode == 'ble',
            device_identity='connected', reconnect_was_emulating=True,
            rumble_desired=1.0, rumble_state=True, _pipe_cancel=threading.Event(),
            input_proc=Mock(stop_event=threading.Event()),
            emu_mgr=Mock(), conn_mgr=Mock(), ble_data_queue=queue.Queue(maxsize=1)))
    return app, definitions


class GuiLossTests(unittest.TestCase):
    def test_eof_wakes_initialization(self):
        app, _ = gui(helper(), initialized=False)
        app._ble_event_reader()
        self.assertTrue(app._ble_init_event.is_set())
        self.assertEqual(app._wait_ble_init(.05)['e'], 'error')
        self.assertFalse(app._ble_initialized)

    def test_full_queue_does_not_block_reader_or_drop_silently(self):
        proc = helper(b'\xff\x00' + bytes(64))
        app, _ = gui(proc)
        app.slots[0].ble_data_queue.put(b'pending')
        thread = threading.Thread(target=app._ble_event_reader, daemon=True)
        thread.start()
        thread.join(1)
        try:
            self.assertFalse(thread.is_alive(), 'reader blocked on a full queue')
            self.assertIn('queue full', app._ble_loss[1])
        finally:
            # Also release the deliberately blocked baseline implementation.
            app.slots[0].ble_data_queue.get_nowait()
            thread.join(1)

    def test_loss_neutralizes_only_ble_once_and_drops_queued_connected(self):
        proc = helper(b'{"e":"connected","s":0,"mac":"test"}\n')
        app, definitions = gui(proc)
        app._ble_event_reader()
        app._poll_ble_events()
        app._poll_ble_events()
        for i in (0, 2):
            slot = app.slots[i]
            slot.emu_mgr.stop.assert_called_once()
            slot.input_proc.stop.assert_called_once()
            self.assertFalse(slot.ble_connected)
            self.assertTrue(slot.input_proc.stop_event.is_set())
        for i in (1, 3):
            app.slots[i].emu_mgr.stop.assert_not_called()
            app.slots[i].input_proc.stop.assert_not_called()
            app.slots[i].conn_mgr.disconnect.assert_not_called()
        app._handle_ble_event.assert_not_called()
        definitions['reap_helper'].assert_called_once_with(proc)
        app._start_auto_scan.assert_not_called()
        app.root.after.assert_not_called()  # The reader never calls Tk.

    def test_backend_and_ui_failures_do_not_skip_other_slots_or_reaping(self):
        app, definitions = gui(helper())
        app._stop_auto_scan.side_effect = RuntimeError('Tk failed')
        app.ui.update_status.side_effect = RuntimeError('Tk failed')
        app.slots[0].emu_mgr.stop.side_effect = OSError('backend failed')
        app._ble_event_reader()
        app._poll_ble_events()
        app.slots[0].input_proc.stop.assert_called_once()
        app.slots[2].emu_mgr.stop.assert_called_once()
        definitions['reap_helper'].assert_called_once()

    def test_retired_reader_cannot_enqueue_input_or_poison_replacement_init(self):
        old = helper(b'\xff\x00' + bytes(64) + b'{"e":"open_ok"}\n')
        replacement = helper()
        app, _ = gui(replacement, initialized=False)
        app._ble_event_reader(old)
        self.assertTrue(app.slots[0].ble_data_queue.empty())
        self.assertIsNone(app._ble_loss)
        self.assertFalse(app._ble_init_event.is_set())
        self.assertIs(app._ble_subprocess, replacement)

    def test_delayed_events_and_cleanup_recheck_owner(self):
        old, replacement = helper(), helper()
        app, definitions = gui(replacement)
        app._ble_events.put((old, {'e': 'disconnected', 's': 0}))
        app._poll_ble_events()
        app._on_ble_service_loss(old, 'late EOF')
        app._cleanup_ble(old)
        app._handle_ble_event.assert_not_called()
        definitions['reap_helper'].assert_not_called()
        self.assertIs(app._ble_subprocess, replacement)

    def test_normal_event_is_dispatched_on_poll(self):
        proc = helper()
        app, _ = gui(proc)
        event = {'e': 'status', 's': 0, 'msg': 'ready'}
        app._ble_events.put((proc, event))
        app._poll_ble_events()
        app._handle_ble_event.assert_called_once_with(event)

    def test_event_queue_saturation_uses_out_of_band_failure(self):
        app, _ = gui(helper(b'{"e":"status","s":0}\n'))
        app._ble_events = queue.Queue(maxsize=1)
        app._ble_events.put((app._ble_subprocess, {'e': 'connected', 's': 0}))
        app._ble_event_reader()
        self.assertIsNotNone(app._ble_loss)
        app._poll_ble_events()
        app._handle_ble_event.assert_not_called()
        self.assertIsNone(app._ble_subprocess)

    def test_expected_shutdown_and_late_async_completion_do_not_report_loss(self):
        proc = helper()
        app, definitions = gui(proc)
        old_attempt = app._ble_init_attempt
        app._cleanup_ble()
        app._ble_event_reader(proc)
        app._ble_init_attempt = object()
        app._on_ble_init_complete(True, old_attempt)
        self.assertIsNone(app._ble_loss)
        self.assertFalse(app._ble_service_failed)
        app._start_auto_scan.assert_not_called()
        definitions['reap_helper'].assert_called_once_with(proc)

    def test_quit_discards_reader_loss(self):
        app, _ = gui(helper())
        app._closing = True
        app._ble_event_reader()
        self.assertIsNone(app._ble_loss)

    def test_failed_event_dispatch_still_retires_the_helper(self):
        proc = helper()
        app, _ = gui(proc)
        app._handle_ble_event.side_effect = RuntimeError('callback failed')
        app._ble_events.put((proc, {'e': 'connected', 's': 0}))
        app._poll_ble_events()
        app.slots[0].emu_mgr.stop.assert_called_once()
        self.assertIsNone(app._ble_subprocess)

    def test_loss_restores_pending_pair_ui_but_not_a_live_usb_slot(self):
        app, _ = gui(helper())
        app._ble_pair_mode = {1: 'pair', 3: 'pair'}
        app.slots[1].conn_mgr.device = None
        app._ble_event_reader()
        app._poll_ble_events()
        app.ui.slots[1].pair_btn.configure.assert_called_once()
        app.slots[3].input_proc.stop.assert_not_called()
        app.ui.slots[3].pair_btn.configure.assert_not_called()

    def test_old_initialization_cannot_send_to_replacement_helper(self):
        old, replacement = helper(), helper()
        app, _ = gui(replacement)
        app._send_ble_cmd({'cmd': 'open'}, proc=old)
        replacement.stdin.write.assert_not_called()
        old.stdin.write.assert_not_called()

    def test_real_child_termination_retires_non_neutral_output(self):
        proc = subprocess.Popen([sys.executable, '-u', '-c',
            "import sys,time; sys.stdout.buffer.write(b'\\xff\\x00'+bytes(64)); "
            "sys.stdout.buffer.flush(); time.sleep(30)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        app, _ = gui(proc)
        thread = threading.Thread(target=app._ble_event_reader, args=(proc,), daemon=True)
        try:
            thread.start()
            self.assertEqual(app.slots[0].ble_data_queue.get(timeout=3), bytes(64))
            proc.kill()
            proc.wait(timeout=3)
            thread.join(3)
            self.assertFalse(thread.is_alive())
            app._poll_ble_events()
            app.slots[0].emu_mgr.stop.assert_called_once()
            self.assertFalse(app.slots[0].ble_connected)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
            proc.stdin.close()
            thread.join(3)


class HeadlessLossTests(unittest.TestCase):
    def manager(self, proc=None):
        cls, _ = production_class('_BleHeadlessManager')
        manager = cls()
        manager._subprocess = proc or helper()
        return manager

    def test_eof_wakes_initialization(self):
        manager = self.manager()
        manager._event_reader(Mock(), Mock())
        self.assertTrue(manager._init_event.is_set())
        self.assertEqual(manager._wait_init(.05)['e'], 'error')

    def test_input_overflow_and_event_callback_error_are_not_swallowed(self):
        for payload, data_error, event_error in (
                (b'\xff\x00' + bytes(64), queue.Full(), None),
                (b'{"e":"connected","s":0}\n', None, RuntimeError('callback'))):
            with self.subTest(payload=payload[:5]):
                manager = self.manager(helper(payload))
                manager._initialized = True
                manager._event_reader(Mock(side_effect=data_error), Mock(side_effect=event_error))
                self.assertFalse(manager.is_alive)
                self.assertIsNotNone(manager._failure)

    def test_retired_callbacks_and_queued_events_cannot_touch_new_helper(self):
        old = helper(b'\xff\x00' + bytes(64))
        manager = self.manager()
        callback = Mock()
        manager._event_reader(callback, callback, old)
        callback.assert_not_called()
        self.assertIsNone(manager._failure)
        self.assertFalse(manager.accepts_event({'e': 'service_lost', '_owner': old}))

    def test_loss_handler_cleans_owned_ble_and_preserves_usb_and_other_owner(self):
        proc, other = helper(), helper()
        manager = self.manager(proc)
        slots = [dict(index=i, type=mode, ble_process=owner,
                      emu_mgr=Mock(), input_proc=Mock(stop_event=threading.Event()))
                 for i, mode, owner in ((0, 'ble', proc), (1, 'usb', None), (2, 'ble', other))]
        affected = slots[0]
        affected['emu_mgr'].stop.side_effect = RuntimeError('cleanup failed')
        queues, reconnects, rumble = {0: queue.Queue(), 2: queue.Queue()}, {0: 'device'}, [True]*4
        handle = nested_handler('_handle_headless_ble_event', dict(
            ble_mgr=manager, active_slots=slots, ble_data_queues=queues,
            ble_pending_reconnects=reconnects, rumble_states=rumble,
            ble_scanning_slot=0))
        manager._event_reader(Mock(), Mock())
        loss = manager._failure
        handle(loss)
        handle(loss)
        affected['emu_mgr'].stop.assert_called_once()
        affected['input_proc'].stop.assert_called_once()
        self.assertEqual([s['index'] for s in slots], [1, 2])
        for slot in slots:
            slot['emu_mgr'].stop.assert_not_called()
        self.assertNotIn(0, queues)
        self.assertIn(2, queues)
        self.assertFalse(rumble[0])
        self.assertEqual(reconnects, {})
        proc.stdin.write.assert_not_called()  # Do not send on the failed transport.

    def test_actual_data_callback_propagates_overflow(self):
        q = queue.Queue(maxsize=1)
        q.put(b'press')
        callback = nested_handler('_on_ble_data', {'ble_data_queues': {0: q}, '_queue': queue})
        with self.assertRaises(queue.Full):
            callback(0, b'release')
        self.assertEqual(q.get_nowait(), b'press')

    def test_old_waiter_shutdown_and_command_do_not_mutate_replacement(self):
        old, replacement = helper(), helper()
        manager = self.manager(replacement)
        manager._init_result = {'e': 'open_ok'}
        manager._init_event.set()
        self.assertIsNone(manager._wait_init(.05, proc=old))
        manager.shutdown(old)
        manager.send_cmd({'cmd': 'open'}, proc=old)
        replacement.stdin.write.assert_not_called()
        self.assertIs(manager._subprocess, replacement)
        self.assertEqual(manager._wait_init(.05), {'e': 'open_ok'})
        self.assertFalse(manager._stopping)

    def test_normal_shutdown_preserves_shutdown_command_without_loss(self):
        proc = helper()
        manager = self.manager(proc)
        manager.shutdown()
        manager._event_reader(Mock(), Mock(), proc)
        self.assertIsNone(manager._failure)
        self.assertIn(b'shutdown', proc.stdin.write.call_args.args[0])
        self.assertFalse(manager.accepts_event({'e': 'connected', '_owner': proc}))


class FramingTests(unittest.TestCase):
    def test_fragmented_reports_and_json_retain_order(self):
        class Fragments(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))
        events = []
        stream = Fragments(b'{"e":"status","s":1}\n\xff\x01' + bytes(range(64)))
        with self.assertRaises(EOFError):
            reader.read_events(stream, lambda si, data: events.append((si, data)), events.append)
        self.assertEqual(events, [{'e': 'status', 's': 1}, (1, bytes(range(64)))])

    def test_every_truncated_binary_report_is_rejected(self):
        for length in range(65):
            with self.subTest(length=length), self.assertRaises(EOFError):
                reader.read_events(io.BytesIO(b'\xff' + bytes(length)), Mock(), Mock())

    def test_invalid_json_slot_type_and_oversize_are_rejected(self):
        for data in (b'[]\n', b'{"e":"status","s":true}\n', b'{"e":"status","s":4}\n',
                     b'{bad}\n', b'{"e":"status"}', b'x'*65537 + b'\n', b'\xff\x04'+bytes(64)):
            with self.subTest(data=data[:40]), self.assertRaises((ValueError, EOFError)):
                reader.read_events(io.BytesIO(data), Mock(), Mock())


class ReapingTests(unittest.TestCase):
    def test_real_helper_observes_eof_and_is_reaped(self):
        proc = subprocess.Popen([sys.executable, '-c',
                                 "import sys; sys.stdin.buffer.read()"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            reader.reap_helper(proc)
            self.assertIsNotNone(proc.poll())
            self.assertTrue(proc.stdin.closed)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=3)
            proc.stdout.close()

    def test_wait_timeout_escalates_to_terminate_then_kill(self):
        proc = helper()
        proc.wait.side_effect = [subprocess.TimeoutExpired('helper', 1),
                                 subprocess.TimeoutExpired('helper', 3), 0]
        reader.reap_helper(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 3)
