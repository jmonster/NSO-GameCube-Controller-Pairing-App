"""Real worker threads, faked USB calls, and owner-thread callback delivery."""
import ast
import logging
import queue
import threading
import time
from types import SimpleNamespace, MethodType
from unittest import TestCase
from unittest.mock import Mock

from _support import SRC, load_module, load_definitions

usb = load_module('usb_worker.py')


class Delivery:
    def __init__(self):
        self.queue = queue.Queue()
        self.owner = threading.get_ident()
        self.accept = True

    def post(self, fn, *args):
        if not self.accept:
            return False
        self.queue.put((fn, args))
        return True

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError('Worker/callback did not complete')
            try:
                fn, args = self.queue.get(timeout=0.02)
            except queue.Empty:
                continue
            assert threading.get_ident() == self.owner
            fn(*args)

    def drain(self):
        while not self.queue.empty():
            fn, args = self.queue.get_nowait()
            fn(*args)


class USBWorkerTests(TestCase):
    def setUp(self):
        self.delivery = Delivery()
        self.created = []
        self.connect_entered = threading.Event()
        self.connect_gate = threading.Event(); self.connect_gate.set()
        self.feedback_gate = threading.Event(); self.feedback_gate.set()
        self.feedback_entered = threading.Event()
        self.calls = []
        case = self
        class Manager:
            @staticmethod
            def enumerate_devices():
                case.calls.append(('scan', threading.get_ident()))
                return [{'path': b'A'}, {'path': b'B'}]
            def __init__(self, on_status, on_progress):
                self.device = None
                self.on_status = on_status
                case.created.append(self)
            def connect_hid(self, device_path):
                case.calls.append(('open', device_path, threading.get_ident()))
                case.connect_entered.set()
                if not case.connect_gate.wait(4): raise RuntimeError('test gate')
                self.device = object()
                self.path = device_path
                self.on_status('connected')
                return True
            def set_player_led(self, n):
                case.feedback_entered.set()
                if not case.feedback_gate.wait(4): raise RuntimeError('test gate')
                case.calls.append(('led', self.path, n, threading.get_ident()))
            def send_rumble(self, on):
                case.calls.append(('rumble', getattr(self, 'path', None), on, threading.get_ident()))
            def disconnect(self):
                case.calls.append(('close', getattr(self, 'path', None), threading.get_ident()))
                self.device = None
        self.factory = Manager
        self.service = usb.USBService(Manager, self.delivery.post)
        self.status = Mock()
        self.connection = self.service.connection(self.status, Mock())

    def tearDown(self):
        self.connect_gate.set(); self.feedback_gate.set()
        self.service.close(4)
        self.delivery.drain()

    def connect(self, path=b'A', connection=None):
        connection = connection or self.connection
        ready = []
        self.assertTrue(connection.connect_async(path, ready.append))
        self.delivery.until(lambda: ready)
        self.assertEqual(ready, [True])
        return connection

    def test_open_and_cleanup_use_worker_and_publish_only_on_owner(self):
        ready = []
        self.assertTrue(self.connection.connect_async(b'A', ready.append))
        self.assertTrue(self.connect_entered.wait(2))
        self.assertIsNone(self.connection.device)
        self.delivery.until(lambda: ready)
        self.assertIsNotNone(self.connection.device)
        self.connection.disconnect()
        self.assertIsNone(self.connection.device)
        self.assertTrue(self.service.close(3))
        self.assertTrue(all(row[-1] != threading.get_ident() for row in self.calls))

    def test_cancel_blocked_open_disposes_late_result_and_suppresses_callbacks(self):
        self.connect_gate.clear(); ready = Mock()
        self.connection.connect_async(b'A', ready)
        self.assertTrue(self.connect_entered.wait(2))
        self.connection.disconnect()
        self.connect_gate.set()
        self.service.close(3)
        self.delivery.drain()
        ready.assert_not_called(); self.status.assert_not_called()
        self.assertIsNone(self.connection.device)
        self.assertIsNone(self.created[0].device)

    def test_cancel_after_completion_is_queued_cannot_install_late_device(self):
        ready = Mock(); self.connection.connect_async(b'A', ready)
        deadline = time.monotonic() + 3
        while self.delivery.queue.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.connection.disconnect()
        self.delivery.drain()
        self.assertIsNone(self.connection.device)
        ready.assert_not_called()

    def test_same_path_stays_reserved_during_retired_open_cleanup(self):
        self.connect_gate.clear()
        self.connection.connect_async(b'A', Mock())
        self.assertTrue(self.connect_entered.wait(2))
        self.connection.disconnect()
        other = self.service.connection(Mock(), Mock())
        self.assertFalse(other.connect_async(b'A', Mock()))
        self.connect_gate.set()
        self.delivery.until(lambda: not self.service._sessions)
        self.connect(b'A', other)

    def test_worker_budget_cannot_be_bypassed_by_cancel_and_retry(self):
        self.connect_gate.clear()
        peers = [self.service.connection(Mock(), Mock()) for _ in range(5)]
        for i, peer in enumerate(peers[:4]):
            self.assertTrue(peer.connect_async(str(i).encode(), Mock()))
            peer.disconnect()
        # Cancellation before start may finish quickly, so force the first four
        # actors into blocking opens before testing the resource limit below.
        self.connect_gate.set(); self.service.close(3)
        self.service = usb.USBService(self.factory, self.delivery.post, max_sessions=1)
        peer = self.service.connection(Mock(), Mock())
        self.connect_gate.clear(); self.connect_entered.clear()
        peer.connect_async(b'A', Mock())
        self.assertTrue(self.connect_entered.wait(2)); peer.disconnect()
        replacement = self.service.connection(Mock(), Mock())
        self.assertFalse(replacement.connect_async(b'B', Mock()))
        self.assertEqual(len(self.service._sessions), 1)
        self.assertFalse(self.service.close(0))

    def test_feedback_is_bounded_latest_state_and_disconnect_never_waits(self):
        self.connect()
        self.feedback_gate.clear()
        self.connection.set_player_led(1)
        self.assertTrue(self.feedback_entered.wait(2))
        actor = self.connection._actor
        for _ in range(1000):
            self.connection.send_rumble(True)
            self.connection.send_rumble(False)
            self.connection.set_player_led(2)
        self.assertLessEqual(len(actor.pending), 2)
        self.connection.disconnect()
        self.assertTrue(actor.stopped)
        self.assertFalse(self.connection.send_rumble(True))
        self.feedback_gate.set(); actor.thread.join(3)
        self.assertFalse(actor.thread.is_alive())
        self.assertNotIn(True, [c[2] for c in self.calls if c[0] == 'rumble'])
        self.assertEqual([c[2] for c in self.calls if c[0] == 'led'], [1])

    def test_transfer_during_feedback_preserves_actor_without_taking_hardware_lock(self):
        self.connect()
        self.feedback_gate.clear(); self.connection.set_player_led(1)
        self.assertTrue(self.feedback_entered.wait(2))
        dest = self.service.connection(Mock(), Mock())
        device = self.connection.device
        self.assertTrue(self.connection.transfer_to(dest))
        self.assertIsNone(self.connection.device)
        self.assertIs(dest.device, device)
        self.assertEqual(dest.device_path, b'A')
        self.assertFalse(self.connection.send_rumble(True))
        dest.set_player_led(4)
        self.feedback_gate.set()
        self.delivery.until(lambda: any(c[:3] == ('led', b'A', 4) for c in self.calls))
        self.assertEqual(len(self.created), 1)

    def test_rejected_completion_dispatch_closes_hardware(self):
        self.delivery.accept = False
        self.connection.connect_async(b'A', Mock())
        self.assertTrue(self.connect_entered.wait(2))
        self.connection._actor.thread.join(3)
        self.assertIsNone(self.created[0].device)
        self.assertFalse(self.service._sessions)

    def test_callback_failure_disposes_published_device(self):
        callback = Mock(side_effect=RuntimeError('UI failed'))
        self.connection.connect_async(b'A', callback)
        with self.assertRaisesRegex(RuntimeError, 'UI failed'):
            self.delivery.until(lambda: callback.called)
        self.assertIsNone(self.connection.device)
        self.assertTrue(self.service.close(3))

    def test_scan_uses_worker_and_cancellation_suppresses_queued_snapshot(self):
        callback = Mock()
        self.service.scan('test', callback)
        deadline = time.monotonic() + 2
        while self.delivery.queue.empty() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.service.cancel_scan('test'); self.delivery.drain()
        callback.assert_not_called()
        self.service.scan('test', callback)
        self.delivery.until(lambda: callback.called)
        self.assertEqual(callback.call_args.args[0][0]['path'], b'A')
        self.assertIsNone(callback.call_args.args[1])
        self.assertTrue(all(c[1] != threading.get_ident() for c in self.calls if c[0] == 'scan'))

    def test_scan_error_is_distinct_from_empty_success(self):
        self.factory.enumerate_devices = Mock(side_effect=OSError('discovery failed'))
        result = Mock(); self.service.scan('error', result)
        self.delivery.until(lambda: result.called)
        self.assertEqual(result.call_args.args, ([], 'discovery failed'))

    def test_scan_backlog_and_shutdown_are_bounded(self):
        self.service.close(3)
        gate = threading.Event(); entered = threading.Event()
        self.factory.enumerate_devices = lambda: (entered.set(), gate.wait(3), [])[2]
        self.service = usb.USBService(self.factory, self.delivery.post, max_scans=2)
        callback = Mock()
        self.service.scan('a', callback); self.assertTrue(entered.wait(2))
        for _ in range(100): self.assertTrue(self.service.scan('a', callback))
        self.assertTrue(self.service.scan('b', callback))
        self.assertFalse(self.service.scan('c', callback))
        self.assertFalse(self.service.close(0))
        gate.set(); self.service.close(3); self.delivery.drain()
        callback.assert_not_called()


class USBGuiSchedulingTests(TestCase):
    def make_app(self, methods):
        definitions = load_definitions('app.py', methods,
            {'MAX_SLOTS': 2, 't': lambda s: s, 'time': time,
             'make_usb_device_identity': lambda d: 'usb:' + d.get('serial_number', ''),
             'logger': logging.getLogger(__name__)}, class_name='GCControllerEnabler')
        slots = [SimpleNamespace(is_connected=False, usb_pending=None, usb_pending_path=None,
                                 conn_mgr=Mock(device_path=None), input_proc=Mock(),
                                 device_identity='usb:A', reconnect_was_emulating=True) for _ in range(2)]
        ui = Mock(); ui.slots = [Mock(), Mock()]
        app = SimpleNamespace(slots=slots, ui=ui, _usb_service=Mock(), _ble_pair_mode={},
                              root=Mock(), slot_calibrations=[{}, {}], _recent_usb_hotplug={},
                              _resolve_slot_for_device=lambda i: None)
        for n in methods: setattr(app, n, MethodType(definitions[n], app))
        return app

    def test_gui_class_has_no_blocking_hid_usb_open_close_or_enumeration(self):
        tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'GCControllerEnabler')
        forbidden = {'enumerate_devices', 'enumerate_usb_devices', 'connect_hid', 'initialize_via_usb'}
        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden)
                self.assertNotEqual(ast.unparse(node.func), 'hid.enumerate')
                self.assertFalse(ast.unparse(node.func).endswith('conn_mgr.device.close'))

    def test_manual_scan_completion_cannot_open_after_cancellation(self):
        app = self.make_app({'connect_controller', '_cancel_usb_pending', '_usb_claimed_paths'})
        app._queue_usb_open = Mock()
        app.connect_controller(0)
        callback = app._usb_service.scan.call_args.args[1]
        app._cancel_usb_pending(0)
        callback([{'path': b'A'}], None)
        app._queue_usb_open.assert_not_called()

    def test_two_manual_scan_callbacks_do_not_claim_same_hid_path(self):
        app = self.make_app({'connect_controller', '_queue_usb_open', '_usb_claimed_paths'})
        app._usb_connect_done = Mock()
        app.connect_controller(0); first = app._usb_service.scan.call_args.args[1]
        app.connect_controller(1); second = app._usb_service.scan.call_args.args[1]
        devices = [{'path': b'A'}, {'path': b'B'}]
        first(devices, None); second(devices, None)
        self.assertEqual(app.slots[0].conn_mgr.connect_async.call_args.args[0], b'A')
        self.assertEqual(app.slots[1].conn_mgr.connect_async.call_args.args[0], b'B')

    def test_pending_usb_slot_is_not_reassigned_to_ble(self):
        app = self.make_app({'_find_slot_for_device', '_resolve_ble_target_slot', '_pick_auto_scan_slot'})
        app.slots[0].usb_pending = object()
        app._resolve_slot_for_device = lambda identity: 0
        self.assertEqual(app._find_slot_for_device('usb:A'), 1)
        self.assertEqual(app._resolve_ble_target_slot(0, 'ble:A'), 1)
        self.assertEqual(app._pick_auto_scan_slot(), 1)
        app.slots[1].usb_pending = object()
        self.assertIsNone(app._resolve_ble_target_slot(0, 'ble:A'))

    def test_reconnect_never_falls_through_to_other_controller(self):
        app = self.make_app({'_attempt_reconnect', '_usb_claimed_paths'})
        app.slots[0].input_proc.stop_event.is_set.return_value = False
        app._queue_usb_open = Mock()
        app._attempt_reconnect(0)
        callback = app._usb_service.scan.call_args.args[1]
        callback([{'path': b'B', 'serial_number': 'B'}], None)
        app._queue_usb_open.assert_not_called()
        self.assertIsNone(app.slots[0].usb_pending)
        app.root.after.assert_called_once()

    def test_scan_failure_does_not_fabricate_unplugs(self):
        app = self.make_app({'_usb_hotplug_tick'})
        app._usb_hotplug_active = True; app._usb_hotplug_epoch = object()
        app._last_seen_usb_paths = {b'A'}; app._connect_usb_snapshot = Mock()
        app._usb_hotplug_tick()
        callback = app._usb_service.scan.call_args.args[1]
        callback([], 'permission error')
        self.assertEqual(app._last_seen_usb_paths, {b'A'})
        app._connect_usb_snapshot.assert_not_called()

    def test_retired_reader_disconnect_cannot_close_current_session(self):
        app = self.make_app({'_on_unexpected_disconnect'})
        app._cancel_usb_pending = Mock()
        app.slots[0].input_proc.reader_token = object()
        app._on_unexpected_disconnect(0, object())
        app.slots[0].conn_mgr.disconnect.assert_not_called()
        app._cancel_usb_pending.assert_not_called()
