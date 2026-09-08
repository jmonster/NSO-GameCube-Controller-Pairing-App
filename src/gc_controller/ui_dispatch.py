"""Worker-to-Tk dispatch without invoking Tcl from a worker thread."""
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class MainThreadDispatcher:
    """Drain bounded batches on Tk's owner thread; posting never calls Tk.

    The caller creates/closes this dispatcher on the thread that owns root.
    Input rendering retains its separate fixed-rate poll, not this queue.
    """

    def __init__(self, root, interval_ms=10, batch_size=64):
        if interval_ms < 1 or batch_size < 1:
            raise ValueError('interval_ms and batch_size must be positive')
        self._root = root
        self._interval_ms = interval_ms
        self._batch_size = batch_size
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._timer = root.after(interval_ms, self._drain)

    def post(self, callback, *args, **kwargs):
        """Queue a callback; return False once shutdown starts."""
        with self._lock:
            if self._closed:
                return False
            self._queue.put_nowait((callback, args, kwargs))
        return True

    def _drain(self):
        self._timer = None
        for _ in range(self._batch_size):
            if self._closed:
                return
            try:
                callback, args, kwargs = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args, **kwargs)
            except Exception:
                logger.exception('UI callback failed')
        if not self._closed:
            self._timer = self._root.after(self._interval_ms, self._drain)

    def close(self):
        """Stop accepting callbacks and discard queued work before root.destroy()."""
        with self._lock:
            self._closed = True
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
        if self._timer is not None:
            self._root.after_cancel(self._timer)
            self._timer = None
