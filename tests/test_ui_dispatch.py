import threading
import unittest
from unittest.mock import Mock
from _support import load_module


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module('ui_dispatch.py')
        self.owner = threading.get_ident()
        self.scheduled = []
        def after(delay, callback):
            self.assertEqual(threading.get_ident(), self.owner)
            self.scheduled.append(callback)
            return len(self.scheduled)
        self.root = Mock(after=Mock(side_effect=after))
        self.dispatcher = self.module.MainThreadDispatcher(self.root, batch_size=2)

    def test_worker_post_never_calls_tk(self):
        seen = []
        worker = threading.Thread(target=lambda: self.dispatcher.post(seen.append, 'value'))
        worker.start(); worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(seen, [])
        self.assertEqual(self.root.after.call_count, 1)
        self.scheduled.pop(0)()
        self.assertEqual(seen, ['value'])

    def test_bounded_batches_yield_to_tk(self):
        seen = []
        for value in range(5):
            self.dispatcher.post(seen.append, value)
        self.scheduled.pop(0)()
        self.assertEqual(seen, [0, 1])
        self.scheduled.pop(0)()
        self.assertEqual(seen, [0, 1, 2, 3])

    def test_failed_callback_does_not_drop_following_callback(self):
        callback = Mock()
        self.dispatcher.post(lambda: 1 / 0)
        self.dispatcher.post(callback)
        with self.assertLogs(self.module.logger, level='ERROR'):
            self.scheduled.pop(0)()
        callback.assert_called_once()

    def test_shutdown_discards_work_and_rejects_future_posts(self):
        callback = Mock()
        self.dispatcher.post(callback)
        self.dispatcher.close()
        self.assertFalse(self.dispatcher.post(callback))
        self.scheduled.pop(0)()
        callback.assert_not_called()
        self.root.after_cancel.assert_called_once()

    def test_shutdown_inside_callback_does_not_reschedule(self):
        callback = Mock()
        self.dispatcher.post(self.dispatcher.close)
        self.dispatcher.post(callback)
        self.scheduled.pop(0)()
        callback.assert_not_called()
        self.assertEqual(self.root.after.call_count, 1)

    def test_overflow_is_bounded_latched_and_handled_on_owner_thread(self):
        self.dispatcher.close()
        calls = []
        def overflow():
            self.assertEqual(threading.get_ident(), self.owner)
            calls.append('overflow')
        dispatcher = self.module.MainThreadDispatcher(self.root, max_pending=2, on_overflow=overflow)
        scheduled = self.scheduled[-1]
        callback = Mock()
        self.assertTrue(dispatcher.post(callback))
        self.assertTrue(dispatcher.post(callback))
        results = []
        worker = threading.Thread(target=lambda: results.append(dispatcher.post(callback)))
        worker.start(); worker.join(2)
        self.assertEqual(results, [False])
        self.assertEqual(dispatcher._queue.qsize(), 2)
        self.assertEqual(calls, [])
        self.assertFalse(dispatcher.post(callback))
        with self.assertLogs(self.module.logger, 'ERROR'):
            scheduled()
        self.assertEqual(calls, ['overflow'])
        self.assertEqual(dispatcher._queue.qsize(), 0)
        callback.assert_not_called()
        scheduled()
        self.assertEqual(calls, ['overflow'])
        self.assertFalse(dispatcher.post(callback))

    def test_explicit_shutdown_cancels_pending_overflow_notification(self):
        callback = Mock()
        dispatcher = self.module.MainThreadDispatcher(self.root, max_pending=1, on_overflow=callback)
        scheduled = self.scheduled[-1]
        dispatcher.post(Mock()); dispatcher.post(Mock())
        dispatcher.close(); scheduled()
        callback.assert_not_called()

    def test_invalid_queue_limits_fail_before_scheduling(self):
        for limit in (0, -1, True, '10', 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.module.MainThreadDispatcher(self.root, max_pending=limit)
