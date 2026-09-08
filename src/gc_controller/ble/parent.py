"""Owned, non-blocking parent commands and bounded child-process teardown."""
import logging
import threading

from .output import OutputWriter
from .sessions import SessionRouter

logger = logging.getLogger(__name__)
_live = set()
_live_lock = threading.Lock()


class CommandTransport:
    """A transport never follows a mutable reference to a replacement process.

    Only the writer touches/closes stdin. A separate reaper escalates graceful
    EOF to termination/kill without holding up Tk or a rumble callback. A write
    deadline detects a stuck child even when the command queue is not full.
    """

    def __init__(self, process, on_failure, *, write_timeout=2.0,
                 grace_timeout=5.0, terminate_timeout=25.0, write=None,
                 max_frames=128, max_bytes=64 * 1024):
        self.process = process
        self._on_failure = on_failure
        self._grace_timeout = grace_timeout
        self._terminate_timeout = terminate_timeout
        self._lock = threading.Lock()
        self._send_lock = threading.RLock()
        self.router = SessionRouter()
        self._closing = False
        self._failed = False
        self._done = threading.Event()
        self._writer = OutputWriter(
            process.stdin.fileno(), self._failed_write, write=write,
            max_frames=max_frames, max_bytes=max_bytes,
            write_timeout=write_timeout, on_close=process.stdin.close)
        with _live_lock:
            _live.add(self)

    def _failed_write(self, error):
        with self._lock:
            if self._failed or self._closing:
                return
            self._failed = True
        try:
            self._on_failure(self.process, str(error))
        except Exception:
            logger.exception('BLE command failure handler failed')
        finally:
            # EOF alone cannot unblock a stuck write. Reap this exact child,
            # even if the UI is shutting down and no longer accepts callbacks.
            self.close()

    def send(self, command):
        # Keep generation allocation and frame publication in the same order
        # even when UI actions and rumble callbacks come from different threads.
        with self._send_lock:
            with self._lock:
                if self._closing or self._failed:
                    return False
            try:
                if self.process.poll() is not None:
                    raise ConnectionError('BLE subprocess has exited')
                prepared = self.router.prepare(command)
                if prepared is None:
                    return False
                self._writer.event(prepared)
                return True
            except Exception as error:
                self._failed_write(error)
                return False

    def close(self, *, wait=False):
        with self._lock:
            start = not self._closing
            self._closing = True
        if start:
            self.router.clear()
            # Zero join: producers/main thread must never wait on a full pipe.
            self._writer.close(0)
            threading.Thread(target=self._reap, name='ble-process-reaper',
                             daemon=True).start()
        if wait:
            self._done.wait(self._grace_timeout + self._terminate_timeout + 4)
        return self._done.is_set()

    @staticmethod
    def close_all():
        """Final application exit must also await previously retired helpers."""
        with _live_lock:
            transports = list(_live)
        for transport in transports:
            transport.close()
        for transport in transports:
            transport.close(wait=True)

    def _reap(self):
        try:
            try:
                self.process.wait(timeout=self._grace_timeout)
            except Exception:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=self._terminate_timeout)
                except Exception:
                    self.process.kill()
                    self.process.wait(timeout=3)
        except Exception:
            logger.warning('BLE subprocess did not exit after termination', exc_info=True)
        finally:
            self._writer.close(0.5)
            self._done.set()
            with _live_lock:
                _live.discard(self)
