"""Parent BLE failure policy, using production readers and event handlers.

Bluetooth, Tk and virtual devices are faked. The subprocess test uses real OS
pipes; no controller, adapter, privileges or display server is required.
"""
import ast
import base64
import io
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from types import MethodType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from _support import SRC, fake_module, load_definitions, load_module

ipc = load_module('ble/ipc.py')


def load_emulation(factory):
    return load_module('emulation_manager.py', {
        'virtual_gamepad': fake_module(VirtualGamepad=object, create_gamepad=factory),
        'calibration': fake_module(CalibrationManager=object),
        'controller_constants': fake_module(BUTTON_MAPPING={'A': 'A', 'B': 'B'}),
    }).EmulationManager


class Pad:
    """Observable output sink, including failure injection at each cleanup step."""
    def __init__(self):
        self.buttons = set()
        self.axes = {}
        self.calls = []
        self.fail = set()

    def press_button(self, button):
        self.buttons.add(button)

    def release_button(self, button):
        self.buttons.discard(button)

    def left_joystick(self, **values):
        self.axes.update(values)

    right_joystick = left_joystick

    def left_trigger(self, value):
        self.axes['left_trigger'] = value

    def right_trigger(self, value):
        self.axes['right_trigger'] = value

    def _record(self, operation):
        self.calls.append(operation)
        if operation in self.fail:
            raise OSError(operation)

    def reset(self):
        self._record('reset')
        self.buttons.clear()
        self.axes.clear()

    def update(self):
        self._record('update')

    def stop_rumble_listener(self):
        self._record('stop_listener')

    def close(self):
        self._record('close')


def nonneutral_output():
    pad = Pad()
    manager = load_emulation(lambda *args, **kwargs: pad)(SimpleNamespace(
        calibrate_trigger_fast=lambda value, side: value, _calibration={}))
    manager.start()
    manager.update(1, 0, 0, -1, 128, 64, {'A': True})
    assert pad.buttons and pad.axes
    pad.calls.clear()
    return manager, pad


def process(stream=b''):
    proc = Mock(stdout=io.BytesIO(stream), stdin=Mock(), stderr=io.BytesIO())
    proc.poll.return_value = None
    return proc


class Root:
    def __init__(self):
        self.callbacks = []

    def after(self, delay, callback):
        self.callbacks.append(callback)
        return len(self.callbacks)

    def after_cancel(self, timer):
        pass

    def drain(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def production_namespace():
    return dict(queue=queue, json=json, threading=threading, time=time,
                subprocess=subprocess, sys=sys, shutil=Mock(), os=Mock(),
                logger=logging.getLogger(__name__), HelperSession=ipc.HelperSession,
                read_event_stream=ipc.read_event_stream, close_helper=ipc.close_helper,
                retire_input=ipc.retire_input, MAX_SLOTS=4, t=lambda key, **kw: key)


def gui(proc=None):
    proc = proc or process()
    session = ipc.HelperSession(proc)
    session.initialized = True
    manager, pad = nonneutral_output()
    slots = [SimpleNamespace(
        ble_session=session if i == 0 else None, connection_mode='ble' if i == 0 else 'usb',
        ble_connected=i == 0, ble_address='test', device_identity='test',
        rumble_desired=1.0, rumble_state=True, is_connected=True,
        ble_data_queue=queue.Queue(1), input_proc=Mock(),
        emu_mgr=manager if i == 0 else Mock(), conn_mgr=Mock(device=None))
        for i in range(4)]
    app = SimpleNamespace(
        _ble_session=session, _ble_subprocess=proc, _ble_initialized=True,
        _ble_init_event=threading.Event(), _ble_init_result=None,
        _ble_closing=False, _ble_init_in_progress=False, _ble_init_attempt=object(),
        _ble_events=queue.SimpleQueue(), _ble_pair_mode={}, _ble_slot_remap={},
        _diff_scan_callback={}, _scan_stream_callback={}, _ble_known_scan_slot=None,
        _auto_scan_pending=False, _auto_scan_slot=None,
        _latest_ui_data=[None] * 4, _rumble_pwm_timers={},
        slots=slots, root=Root(), ui=Mock(slots=[Mock() for _ in slots]),
        _stop_auto_scan=Mock(), _start_auto_scan=Mock(), _stop_rumble_pwm=Mock(),
        _handle_ble_event=Mock(), _apply_ui_update=Mock(),
        _sync_rumble_output=Mock(), slot_calibrations=[{}] * 4,
        _attempt_ble_reconnect=Mock(), _on_pipe_connected=Mock(), _on_pipe_failed=Mock())
    names = {'_ble_event_reader', '_wait_ble_init', '_cleanup_ble', '_ui_poll',
             '_dispatch_ble_event', '_ble_service_lost', '_owned_ble_callback',
             '_make_rumble_callback', '_retry_ble_reconnect', '_on_ble_init_complete',
             '_init_ble_background', '_start_dolphin_pipe_emulation'}
    tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == 'GCControllerEnabler')
    # Using the intersection also permits behavioral reproduction on the old
    # reader, whose missing loss handler is itself part of the defect.
    names &= {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    funcs = load_definitions('app.py', names, production_namespace(), cls.name)
    for name in names:
        setattr(app, name, MethodType(funcs[name], app))
    return app, session, pad


def headless_manager(proc):
    tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == '_BleHeadlessManager')
    ns = production_namespace()
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SRC / 'app.py'), 'exec'), ns)
    manager = ns[cls.name]()
    manager._subprocess = proc
    manager._session = ipc.HelperSession(proc)
    manager._session.initialized = manager._initialized = True
    return manager


def headless_handlers(manager, active_slots, data_queues, **overrides):
    """Execute unchanged nested production definitions in their closure context."""
    tree = ast.parse((SRC / 'app.py').read_text(encoding='utf-8'))
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_headless')
    names = {'_on_ble_data', '_on_ble_event', '_handle_headless_ble_event',
             '_make_headless_rumble_cb'}
    nodes = [n for n in run.body if isinstance(n, ast.FunctionDef) and n.name in names]
    context = dict(ble_mgr=manager, active_slots=active_slots, ble_data_queues=data_queues,
                   ble_event_queue=queue.Queue(), ble_scanning_slot=0,
                   ble_pending_reconnects={0: 'test'}, rumble_states=[True] * 4,
                   rumble_tids=[0] * 4, stop_event=threading.Event(),
                   _open_ble_slots=Mock(return_value=[0]), _start_ble_scan=Mock(),
                   _headless_resolve_slot=Mock(return_value=None),
                   claimed_slot_indices=set(), slot_assignments={},
                   slot_calibrations=[{} for _ in range(4)],
                   mode_override=None, mode='dolphin_pipe',
                   disconnect_events=[threading.Event() for _ in range(4)])
    context.update(overrides)
    wrapper = ast.parse('def capture(' + ','.join(context) + '):\n    return locals()').body[0]
    wrapper.body[0:0] = nodes
    ns = production_namespace() | {'_queue': queue, 'base64': base64,
                                  'build_rumble_packet': Mock(return_value=b'rumble'),
                                  'make_ble_device_identity': lambda mac: mac,
                                  'CalibrationManager': Mock()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                 str(SRC / 'app.py'), 'exec'), ns)
    return ns['capture'](**context)


class GuiLossTests(TestCase):
    def test_eof_neutralizes_owned_output_once_and_preserves_usb(self):
        app, session, pad = gui()
        app._ble_event_reader()
        app._ui_poll()
        app._ui_poll()
        self.assertFalse(pad.buttons)
        self.assertFalse(pad.axes)
        self.assertEqual(pad.calls, ['reset', 'update', 'stop_listener', 'close'])
        self.assertFalse(app.slots[0].emu_mgr.is_emulating)
        self.assertIsNone(app.slots[0].ble_session)
        for slot in app.slots[1:]:
            slot.input_proc.stop.assert_not_called()
            slot.emu_mgr.stop.assert_not_called()
        app._start_auto_scan.assert_not_called()

    def test_full_queue_is_terminal_not_a_blocked_or_silent_release_drop(self):
        app, session, pad = gui(process(b'\xff\x00' + bytes(64)))
        app.slots[0].ble_data_queue.put(b'older pressed report')
        reader = threading.Thread(target=app._ble_event_reader, daemon=True)
        reader.start()
        try:
            reader.join(0.5)
            self.assertFalse(reader.is_alive(), 'Full input queue permanently blocked reader')
            app._ui_poll()
            self.assertIn('queue exhausted', session.failure)
            self.assertFalse(pad.buttons)
        finally:
            # Always release the old blocking implementation during reproduction.
            try:
                app.slots[0].ble_data_queue.get_nowait()
            except queue.Empty:
                pass
            reader.join(1)

    def test_malformed_or_failed_read_is_an_explicit_loss(self):
        streams = [io.BytesIO(b'{broken}\n'), io.BytesIO(b'\xff\x00short'),
                   Mock(read=Mock(side_effect=OSError('pipe read failed')))]
        for stream in streams:
            with self.subTest(stream=type(stream).__name__):
                proc = process()
                proc.stdout = stream
                app, session, pad = gui(proc)
                app._ble_event_reader()
                app._ui_poll()
                self.assertTrue(session.failure)
                self.assertFalse(pad.buttons)
                self.assertIn('close', pad.calls)

    def test_reader_that_resumes_after_replacement_cannot_deliver_events(self):
        app, old, pad = gui(process(b'{"e":"connected","s":0,"mac":"old"}\n'))
        old_stream = old.process.stdout
        replacement = ipc.HelperSession(process())

        def read(size):
            app._ble_session = replacement
            app._ble_subprocess = replacement.process
            app.slots[0].ble_session = replacement
            return old_stream.read(size)

        old.process.stdout = SimpleNamespace(read=read, readline=old_stream.readline)
        app._ble_event_reader()
        app.root.drain()
        app._ui_poll()
        app._handle_ble_event.assert_not_called()
        self.assertTrue(pad.buttons)
        replacement.process.stdin.close.assert_not_called()

    def test_queued_event_and_timer_recheck_ownership(self):
        app, old, pad = gui()
        callback = Mock()
        guarded = app._owned_ble_callback(callback)
        app._ble_events.put((old, {'e': 'connected', 's': 0}))
        replacement = ipc.HelperSession(process())
        app._ble_session = replacement
        guarded()
        app._ui_poll()
        app._handle_ble_event.assert_not_called()
        callback.assert_not_called()

    def test_delayed_loss_only_retires_slots_still_owned_by_old_helper(self):
        app, old, pad = gui()
        replacement = ipc.HelperSession(process())
        app._ble_session = replacement
        app._ble_subprocess = replacement.process
        app.slots[1].connection_mode = 'ble'
        app.slots[1].ble_session = replacement
        old.end('lost')
        app._ble_service_lost(old)
        self.assertFalse(pad.buttons)
        app.slots[1].emu_mgr.stop.assert_not_called()
        self.assertIs(app._ble_subprocess, replacement.process)
        self.assertTrue(app._ble_initialized)
        replacement.process.stdin.close.assert_not_called()

    def test_initialization_waiter_wakes_on_eof_instead_of_waiting_for_timeout(self):
        app, owner, pad = gui()
        app._ble_initialized = owner.initialized = False
        results = []
        waiter = threading.Thread(target=lambda: results.append(app._wait_ble_init(5)), daemon=True)
        waiter.start()
        try:
            app._ble_event_reader()
            waiter.join(0.5)
            self.assertFalse(waiter.is_alive(), 'EOF did not release initialization waiter')
            self.assertEqual(results, [None])
        finally:
            app._ble_init_event.set()  # Release baseline waiters on a failed assertion.
            waiter.join(1)

    def test_input_and_ui_cleanup_errors_do_not_skip_other_slots_or_process(self):
        app, owner, pad = gui()
        other, other_pad = nonneutral_output()
        app.slots[1].connection_mode = 'ble'
        app.slots[1].ble_session = owner
        app.slots[1].emu_mgr = other
        app.slots[0].input_proc.stop.side_effect = OSError('reader cleanup')
        app.ui.reset_slot_ui.side_effect = OSError('UI cleanup')
        app._stop_auto_scan.side_effect = OSError('timer cleanup')
        app._ble_event_reader()
        app._ui_poll()
        self.assertFalse(pad.buttons)
        self.assertFalse(other_pad.buttons)
        owner.process.stdin.close.assert_called_once()

    def test_normal_shutdown_does_not_report_loss_and_is_idempotent(self):
        app, owner, pad = gui()
        app._cleanup_ble()
        app._ble_event_reader()
        app._cleanup_ble()
        app._ui_poll()
        self.assertIsNone(owner.failure)
        self.assertFalse(owner.loss_handled)
        owner.process.stdin.close.assert_called_once()
        app.ui.update_ble_status.assert_not_called()

    def test_rumble_queued_before_replacement_does_not_touch_replacement(self):
        app, old, pad = gui()
        app.slots[0].rumble_desired = 0
        callback = app._make_rumble_callback(0)
        callback(255, 0)
        old.end('lost')
        app._ble_session = app.slots[0].ble_session = ipc.HelperSession(process())
        app.root.drain()
        self.assertEqual(app.slots[0].rumble_desired, 0)
        app._sync_rumble_output.assert_not_called()

    def test_init_completion_cannot_start_scanning_a_replacement(self):
        app, old, pad = gui()
        attempt = app._ble_init_attempt
        app._ble_session = ipc.HelperSession(process())
        app._on_ble_init_complete(True, attempt, old)
        app._start_auto_scan.assert_not_called()

    def test_loss_is_not_delayed_behind_status_event_backlog(self):
        app, owner, pad = gui()
        for _ in range(1000):
            app._ble_events.put((owner, {'e': 'status', 's': 0}))
        app._ble_event_reader()
        app._ui_poll()
        self.assertFalse(pad.buttons)
        app._handle_ble_event.assert_not_called()

    def test_connection_handoff_keeps_reports_before_output_is_attached(self):
        app, owner, pad = gui(process(b'\xff\x00' + bytes(64)))
        slot = app.slots[0]
        slot.ble_session = None
        slot.is_connected = False
        slot.connection_mode = 'usb'  # Empty slot default while UI applies connected.
        app._ble_event_reader()
        self.assertEqual(slot.ble_data_queue.get_nowait(), bytes(64))

    def test_pipe_completion_or_error_cannot_change_a_replacement_slot(self):
        for failure in (None, OSError('old pipe failed')):
            with self.subTest(failure=failure):
                app, owner, pad = gui()
                app.slots[0].emu_mgr = Mock()
                app.slots[0].emu_mgr.start.side_effect = failure
                synchronous = SimpleNamespace(Event=threading.Event,
                    Thread=lambda target, daemon: SimpleNamespace(start=target))
                with patch.dict(app._start_dolphin_pipe_emulation.__func__.__globals__,
                                threading=synchronous):
                    app._start_dolphin_pipe_emulation(0)
                owner.end('lost')
                app._ble_session = app.slots[0].ble_session = ipc.HelperSession(process())
                app.root.drain()
                app._on_pipe_connected.assert_not_called()
                app._on_pipe_failed.assert_not_called()

    def test_background_init_returns_its_actual_owner_after_replacement(self):
        app, owner, pad = gui()
        app._ble_initialized = owner.initialized = False
        app._start_ble_subprocess = Mock(return_value=owner)
        app._send_ble_cmd = Mock()
        replacement = ipc.HelperSession(process())
        replies = iter([{'e': 'ready'}, {'e': 'bluez_stopped'}, {'e': 'open_ok'}])

        def reply(*args, **kwargs):
            result = next(replies)
            if result['e'] == 'open_ok':
                app._ble_session = replacement
                app._ble_subprocess = replacement.process
            return result

        app._wait_ble_init = reply
        result = app._init_ble_background()
        success = result[1] if isinstance(result, tuple) else result
        self.assertFalse(success)
        self.assertFalse(app._ble_initialized)
        self.assertIs(app._ble_session, replacement)

    def test_real_child_exit_releases_init_waiter_and_reports_loss(self):
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import sys; print(\'{"e":"ready"}\', flush=True); sys.stdin.buffer.read(1)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        app, owner, pad = gui(child)
        app._ble_initialized = owner.initialized = False
        reader = threading.Thread(target=app._ble_event_reader, daemon=True)
        reader.start()
        try:
            self.assertEqual(app._wait_ble_init(3), {'e': 'ready'})
            child.stdin.write(b'x')
            child.stdin.flush()
            self.assertIsNone(app._wait_ble_init(3))
            reader.join(3)
            self.assertFalse(reader.is_alive())
            app._ui_poll()
            self.assertFalse(pad.buttons)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()
            reader.join(3)


class HeadlessLossTests(TestCase):
    def setup_headless(self, data=b''):
        mgr = headless_manager(process(data))
        emu, pad = nonneutral_output()
        slots = [dict(index=0, type='ble', ble_session=mgr._session,
                      input_proc=Mock(), emu_mgr=emu)]
        queues = {0: queue.Queue(1)}
        return mgr, slots, pad, headless_handlers(mgr, slots, queues)

    def drain(self, handlers):
        while not handlers['ble_event_queue'].empty():
            handlers['_handle_headless_ble_event'](handlers['ble_event_queue'].get_nowait())

    def test_eof_neutralizes_headless_output_once(self):
        mgr, slots, pad, handlers = self.setup_headless()
        mgr._event_reader(handlers['_on_ble_data'], handlers['_on_ble_event'])
        self.drain(handlers)
        self.drain(handlers)
        self.assertFalse(pad.buttons)
        self.assertEqual(slots, [])
        self.assertEqual(pad.calls, ['reset', 'update', 'stop_listener', 'close'])

    def test_full_headless_queue_is_not_silently_dropped(self):
        mgr, slots, pad, handlers = self.setup_headless(b'\xff\x00' + bytes(64))
        handlers['ble_data_queues'][0].put(b'pressed')
        mgr._event_reader(handlers['_on_ble_data'], handlers['_on_ble_event'])
        self.drain(handlers)
        self.assertIsNotNone(mgr._session.failure)
        self.assertIn('queue exhausted', mgr._session.failure)
        self.assertFalse(pad.buttons)

    def test_stale_loss_preserves_usb_and_replacement_and_continues_cleanup(self):
        mgr, slots, pad, handlers = self.setup_headless()
        old = mgr._session
        old.end('lost')
        mgr._session = replacement = ipc.HelperSession(process())
        mgr._subprocess = replacement.process
        for si, transport in [(1, 'usb'), (2, 'ble')]:
            slots.append(dict(index=si, type=transport, ble_session=replacement,
                              input_proc=Mock(), emu_mgr=Mock()))
        slots[0]['input_proc'].stop.side_effect = OSError('cleanup')
        handlers['_handle_headless_ble_event']({'e': '_service_lost', '_session': old})
        self.assertFalse(pad.buttons)
        self.assertEqual([info['index'] for info in slots], [1, 2])
        for info in slots:
            info['input_proc'].stop.assert_not_called()
            info['emu_mgr'].stop.assert_not_called()
        self.assertIs(mgr._subprocess, replacement.process)
        replacement.process.stdin.close.assert_not_called()

    def test_delayed_retry_and_rumble_are_rejected_after_replacement(self):
        mgr, slots, pad, handlers = self.setup_headless()
        old = mgr._session
        callback = handlers['_make_headless_rumble_cb'](0, owner=old)
        mgr._session = ipc.HelperSession(process())
        handlers['_handle_headless_ble_event']({'e': '_retry_scan', '_session': old})
        handlers['_start_ble_scan'].assert_not_called()
        before = list(handlers['rumble_states'])
        callback(0, 0)
        self.assertEqual(handlers['rumble_states'], before)

    def test_old_headless_reader_cannot_deliver_data_to_a_replacement(self):
        mgr, slots, pad, handlers = self.setup_headless(b'\xff\x00' + bytes(64))
        old = mgr._session
        stream = old.process.stdout
        replacement = ipc.HelperSession(process())

        def read(size):
            mgr._session = replacement
            mgr._subprocess = replacement.process
            return stream.read(size)

        old.process.stdout = SimpleNamespace(read=read, readline=stream.readline)
        data, event = Mock(), Mock()
        mgr._event_reader(data, event)
        data.assert_not_called()
        replacement.process.stdin.close.assert_not_called()

    def test_helper_exit_cancels_blocking_headless_pipe_creation(self):
        mgr = headless_manager(process())
        owner = mgr._session
        entered = threading.Event()
        pad = Pad()

        def factory(*args, cancel_event=None, **kwargs):
            self.assertIs(cancel_event, owner.stop_event)
            entered.set()
            self.assertTrue(cancel_event.wait(3))
            return pad

        emulation = load_emulation(factory)
        handlers = headless_handlers(mgr, [], {})
        handle = handlers['_handle_headless_ble_event']
        creator = threading.Thread(target=lambda: handle(
            {'e': 'connected', 's': 0, 'mac': 'test', '_session': owner}), daemon=True)
        with patch.dict(handle.__globals__, EmulationManager=emulation):
            creator.start()
            try:
                self.assertTrue(entered.wait(3))
                mgr._event_reader(handlers['_on_ble_data'], handlers['_on_ble_event'])
                creator.join(3)
                self.assertFalse(creator.is_alive())
                self.drain(handlers)
                self.assertEqual(handlers['active_slots'], [])
                self.assertIn('close', pad.calls)
            finally:
                owner.end('test teardown')
                creator.join(3)

    def test_init_waiter_and_normal_shutdown(self):
        mgr, slots, pad, handlers = self.setup_headless()
        mgr._initialized = mgr._session.initialized = False
        results = []
        waiter = threading.Thread(target=lambda: results.append(mgr._wait_init(5)), daemon=True)
        waiter.start()
        try:
            mgr.shutdown()
            mgr._event_reader(handlers['_on_ble_data'], handlers['_on_ble_event'])
            waiter.join(0.5)
            self.assertFalse(waiter.is_alive())
            self.assertEqual(results, [None])
            self.assertTrue(handlers['ble_event_queue'].empty())
        finally:
            if hasattr(mgr, '_init_event'):
                mgr._init_event.set()
            waiter.join(1)


class CleanupTests(TestCase):
    def test_every_output_cleanup_operation_is_attempted_after_errors(self):
        for operation in ('reset', 'update', 'stop_listener', 'close'):
            with self.subTest(operation=operation):
                emu, pad = nonneutral_output()
                pad.fail.add(operation)
                emu.stop()
                emu.stop()
                self.assertEqual(pad.calls, ['reset', 'update', 'stop_listener', 'close'])
                self.assertIsNone(emu.gamepad)
                self.assertFalse(emu.is_emulating)

    def test_process_cleanup_escalates_and_reaps_even_when_steps_raise(self):
        proc = process()
        proc.stdin.close.side_effect = OSError('close failed')
        proc.wait.side_effect = [subprocess.TimeoutExpired('helper', 3), 0]
        proc.terminate.side_effect = OSError('terminate failed')
        session = ipc.HelperSession(proc)
        ipc.close_helper(session)
        ipc.close_helper(session)
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)
        proc.stdin.close.assert_called_once()

    def test_creation_completing_after_stop_cannot_replace_new_output(self):
        entered, release = threading.Event(), threading.Event()
        old, new = Pad(), Pad()

        def factory(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return old

        cls = load_emulation(factory)
        manager = cls(Mock())
        errors = []

        def start():
            try:
                manager.start()
            except OSError as error:
                errors.append(error)

        creator = threading.Thread(target=start, daemon=True)
        creator.start()
        try:
            self.assertTrue(entered.wait(3))
            manager.stop()
            # Publish a newer output through the same production start method.
            with patch.dict(cls.start.__globals__, create_gamepad=lambda *a, **kw: new):
                manager.start()
            release.set()
            creator.join(3)
            self.assertFalse(creator.is_alive())
            self.assertIs(manager.gamepad, new)
            self.assertEqual(len(errors), 1)
            self.assertIn('close', old.calls)
            self.assertNotIn('close', new.calls)
        finally:
            release.set()
            creator.join(3)
            manager.stop()

    def test_inflight_input_cannot_reassert_buttons_after_neutralization(self):
        manager, pad = nonneutral_output()
        entered, release = threading.Event(), threading.Event()
        original = pad.left_joystick

        def blocked(**values):
            entered.set()
            self.assertTrue(release.wait(3))
            original(**values)

        pad.left_joystick = blocked
        updater = threading.Thread(target=lambda: manager.update(1, 0, 0, 0, 0, 0, {'B': True}), daemon=True)
        stopper = threading.Thread(target=manager.stop, daemon=True)
        updater.start()
        try:
            self.assertTrue(entered.wait(3))
            stopper.start()
            release.set()
            updater.join(3)
            stopper.join(3)
            self.assertFalse(updater.is_alive() or stopper.is_alive())
            self.assertFalse(pad.buttons)
            self.assertFalse(pad.axes)
            self.assertEqual(pad.calls[-4:], ['reset', 'update', 'stop_listener', 'close'])
        finally:
            release.set()
            updater.join(3)
            if stopper.ident:
                stopper.join(3)
