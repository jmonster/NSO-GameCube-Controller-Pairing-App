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

    def __init__(self, root, interval_ms=10, batch_size=64, max_pending=1024, on_overflow=None):
        if any(type(value) is not int or value < 1 for value in (interval_ms, batch_size, max_pending)):
            raise ValueError('Dispatch interval, batch size and backlog limit must be positive integers')
        self._root = root
        self._interval_ms = interval_ms
        self._batch_size = batch_size
        self._queue = queue.Queue(maxsize=max_pending)
        self._overflow = False
        self._on_overflow = on_overflow
        self._lock = threading.Lock()
        self._closed = False
        self._timer = root.after(interval_ms, self._drain)

    def post(self, callback, *args, **kwargs):
        """Queue without Tcl calls or blocking; overload latches a fatal state."""
        with self._lock:
            if self._closed or self._overflow:
                return False
            try:
                self._queue.put_nowait((callback, args, kwargs))
            except queue.Full:
                self._overflow = True
                return False
        return True

    def _drain(self):
        self._timer = None
        if self._closed:
            return
        if self._overflow:
            # Never replay an incomplete control-event history. The application
            # must neutralize outputs/cancel pending factories on the Tk owner.
            self.close()
            logger.error('UI event backlog exceeded its limit; callbacks retired')
            if self._on_overflow is not None:
                try:
                    self._on_overflow()
                except Exception:
                    logger.exception('UI overload shutdown failed')
            return
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
