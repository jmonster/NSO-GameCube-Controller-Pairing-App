import collections
import queue
import threading
import time
import types
import unittest
from unittest.mock import Mock

from _support import fake_module, load_module


def load_input():
    button = types.SimpleNamespace(name='A', byte_index=3, mask=0x02)
    return load_module('input_processor.py', {
        'controller_constants': fake_module(
            BUTTONS=[button], normalize=lambda value, center, span: (value - center) / span,
            apply_deadzone=lambda value, dz: value),
        'calibration': fake_module(CalibrationManager=object),
        'emulation_manager': fake_module(EmulationManager=object),
    })


def report(button=0, trigger=0):
    result = [0] * 64
    result[3] = button
    result[6:12] = [0, 8, 128, 0, 8, 128]
    result[13] = trigger
    return result


class InputStabilityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_input()
        self.queue = queue.Queue()
        self.errors = []
        self.disconnect = Mock()
        self.cal = types.SimpleNamespace(stick_calibrating=False,
                                         update_trigger_raw=Mock())
        self.emu = types.SimpleNamespace(is_emulating=True, gamepad=object(), update=Mock())
        calibration = {}
        for stick in ('left', 'right'):
            for axis in ('x', 'y'):
                calibration[f'stick_{stick}_center_{axis}'] = 2048
                calibration[f'stick_{stick}_range_{axis}'] = 2047
        self.processor = self.module.InputProcessor(
            device_getter=lambda: None, calibration=calibration,
            cal_mgr=self.cal, emu_mgr=self.emu, on_ui_update=Mock(),
            on_error=self.errors.append, on_disconnect=self.disconnect,
            ble_queue=self.queue)

    def run_ble_reports(self, reports, stop_after):
        for item in reports:
            self.queue.put(item)
        original = self.processor._process_data
        seen = []

        def consume(data, **kwargs):
            original(data, **kwargs)
            seen.append(list(data))
            if len(seen) >= stop_after:
                self.processor.stop()

        self.processor._process_data = consume
        self.processor.start('ble')
        thread = self.processor._read_thread
        thread.join(0.5)
        self.processor.stop()
        self.assertFalse(thread.is_alive())
        return seen

    def test_ble_preserves_entire_queued_press_release(self):
        reports = [report(), report(2), report()]
        self.assertEqual(self.run_ble_reports(reports, 3), reports)
        states = [call.args[-1]['A'] for call in self.emu.update.call_args_list]
        self.assertEqual(states, [False, True, False])
        self.disconnect.assert_not_called()
        self.assertEqual(self.errors, [])

    def test_ble_preserves_analog_trigger_changes(self):
        reports = [report(trigger=v) for v in (0, 128, 255, 0)]
        self.assertEqual(self.run_ble_reports(reports, 4), reports)
        self.assertEqual([c.args[4] for c in self.emu.update.call_args_list], [0, 128, 255, 0])

    def test_ble_saturated_queue_does_not_starve_stop(self):
        reports = [report() for _ in range(10000)]
        seen = self.run_ble_reports(reports, 5)
        self.assertEqual(len(seen), 5)
        self.assertEqual(self.queue.qsize(), len(reports) - 5)

    def test_stop_can_be_called_from_reader_callback(self):
        self.assertEqual(len(self.run_ble_reports([report()], 1)), 1)
        self.assertEqual(self.errors, [])
        self.assertTrue(self.processor.stop_event.is_set())

    def test_stop_waits_for_thread_even_after_is_reading_cleared(self):
        thread = Mock()
        thread.is_alive.return_value = True
        self.processor._read_thread = thread
        self.processor.is_reading = False
        self.processor.stop()
        thread.join.assert_called_once_with(timeout=1.0)

    def test_no_restart_while_previous_reader_alive(self):
        self.processor._read_thread = Mock(is_alive=Mock(return_value=True))
        self.processor.stop_event.set()
        with self.assertRaises(RuntimeError):
            self.processor.start()
        self.assertTrue(self.processor.stop_event.is_set())

    def test_invalid_transport_rejected_before_start(self):
        with self.assertRaises(ValueError):
            self.processor.start('bluetooth-typo')
        self.assertFalse(self.processor.is_reading)

    def test_missing_ble_queue_rejected_before_start(self):
        self.processor._ble_queue = None
        with self.assertRaises(ValueError):
            self.processor.start('ble')
        self.assertFalse(self.processor.is_reading)

    def run_usb_reports(self, reports, windows=False):
        pending = collections.deque(reports)
        device = Mock()
        self.processor._device_getter = lambda: device
        self.module.IS_WINDOWS = windows
        seen = []
        original = self.processor._process_data

        def read(*args, **kwargs):
            if pending:
                return pending.popleft()
            self.processor.stop()
            return []

        def consume(data, **kwargs):
            original(data, **kwargs)
            seen.append(list(data))

        device.read.side_effect = read
        self.processor._process_data = consume
        self.processor.is_reading = True
        self.processor._warmup_passed = True
        self.processor._read_loop()
        return seen, device

    def test_usb_preserves_press_release_and_never_toggles_read_mode(self):
        reports = [report(), report(2), report()]
        seen, device = self.run_usb_reports(reports)
        self.assertEqual(seen, reports)
        device.set_nonblocking.assert_not_called()
        self.assertEqual([c.args[-1]['A'] for c in self.emu.update.call_args_list],
                         [False, True, False])

    def test_truncated_windows_report_does_not_disconnect(self):
        good = [0] * 64
        good[0] = 0x05
        good[5] = 0x08
        seen, _ = self.run_usb_reports([[0x05, 0], good], windows=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][3], 0x02)
        self.assertEqual(self.processor.malformed_report_count, 1)
        self.disconnect.assert_not_called()

    def test_short_native_frame_ignored_and_counted(self):
        self.processor._process_data([0] * 14)
        self.assertEqual(self.processor.malformed_report_count, 1)
        self.emu.update.assert_not_called()

    def test_warmup_failsafe_still_accepts_held_button(self):
        # Keep the existing BLE->USB phantom-input mitigation in this patch.
        self.processor._warmup_start_t = time.perf_counter() - 1.0
        self.processor._process_data(report(2))
        self.assertTrue(self.emu.update.call_args.args[-1]['A'])

    def test_all_truncated_0x05_reports_rejected(self):
        for length in range(63):
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.module._translate_report_0x05(([0x05] + [0] * 64)[:length])

    def test_all_truncated_0x0a_reports_rejected(self):
        for length in range(15):
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.module._translate_report_0x0A(([0x0A] + [0] * 64)[:length])

    def test_translators_reject_mismatched_id(self):
        for translator in (self.module._translate_report_0x05,
                           self.module._translate_report_0x0A):
            with self.subTest(translator=translator.__name__), self.assertRaises(ValueError):
                translator([0] * 64)

    def test_0x05_buttons_sticks_and_trigger_bounds(self):
        for length in (63, 64):
            with self.subTest(length=length):
                data = [0] * length
                data[0], data[5], data[6], data[7] = 0x05, 0xcf, 0x72, 0xcf
                data[11:17] = [0, 0xf0, 255, 255, 15, 0]
                data[61:63] = [0, 255]
                translated = self.module._translate_report_0x05(data)
                self.assertEqual(translated[3:6], [0x7f, 0x3f, 0x13])
                self.assertEqual(translated[6:12], data[11:17])
                self.assertEqual(translated[13:15], [0, 255])

    def test_0x0a_buttons_sticks_and_trigger_bounds(self):
        data = [0] * 15
        data[0], data[3], data[4], data[5] = 0x0A, 0x3f, 0x32, 0xcf
        data[6:12] = [255, 255, 255, 0, 0, 0]
        data[13:15] = [255, 0]
        translated = self.module._translate_report_0x0A(data)
        self.assertEqual(translated[3:6], [0x7f, 0x3f, 0x03])
        self.assertEqual(translated[6:12], data[6:12])
        self.assertEqual(translated[13:15], [255, 0])


if __name__ == '__main__':
    unittest.main()
