"""Ordered, bounded subprocess output without blocking the Bluetooth event loop."""
from collections import deque
import json
import logging
import os
import threading
import time

from .ipc import MAX_JSON_BYTES, MAX_SLOTS

_logger = logging.getLogger(__name__)


class OutputWriter:
    """One daemon writer owns stdout; producers enqueue immutable frames.

    Overflow is a transport failure, never permission to drop button edges.
    The failure callback must stop the child so the parent observes EOF and
    neutralizes its outputs. A blocked OS write cannot be cancelled portably;
    close() therefore has a bounded join and the child must exit afterwards.
    """

    def __init__(self, fd, on_failure, *, max_bytes=128 * 1024,
                 max_frames=512, write=None, write_timeout=None, on_close=None):
        if max_bytes <= 0 or max_frames <= 0:
            raise ValueError('Output limits must be positive')
        if write_timeout is not None and write_timeout <= 0:
            raise ValueError('write_timeout must be positive')
        self._write_timeout = write_timeout
        self._on_close = on_close
        self._writing_since = None
        self._finished = threading.Event()
        self._fd = fd
        self._write = os.write if write is None else write
        self._on_failure = on_failure
        self._max_bytes = max_bytes
        self._max_frames = max_frames
        self._frames = deque()
        self._bytes = 0
        self._condition = threading.Condition()
        self._closed = False
        self._failure = None
        self._thread = threading.Thread(target=self._run, name='ble-output', daemon=True)
        self._thread.start()
        if write_timeout is not None:
            threading.Thread(target=self._watchdog, name='ble-write-watchdog',
                             daemon=True).start()

    def _fail(self, error):
        with self._condition:
            if self._failure is not None:
                return
            self._failure = error
            self._closed = True
            self._frames.clear()
            self._bytes = 0
            self._condition.notify_all()
        try:
            self._on_failure(error)
        except Exception:
            _logger.exception('BLE output failure handler failed')

    def put(self, frame):
        frame = bytes(frame)
        with self._condition:
            if self._closed:
                raise ConnectionError('BLE output is closed') from self._failure
            overflow = (len(self._frames) >= self._max_frames or
                        self._bytes + len(frame) > self._max_bytes)
            if not overflow:
                self._frames.append(frame)
                self._bytes += len(frame)
                self._condition.notify()
                return
        error = BufferError('BLE output backlog exceeded its limit')
        self._fail(error)
        raise ConnectionError(str(error)) from error

    def event(self, event):
        frame = (json.dumps(event, separators=(',', ':')) + '\n').encode('utf-8')
        if len(frame) > MAX_JSON_BYTES:
            error = ValueError('BLE event exceeds size limit')
            self._fail(error)
            raise error
        self.put(frame)

    def data(self, slot, report):
        if type(slot) is not int or not 0 <= slot < MAX_SLOTS or len(report) != 64:
            raise ValueError('Expected a valid slot and a 64-byte translated input report')
        self.put(bytes((0xff, slot)) + bytes(report))

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._frames or self._closed)
                    if not self._frames:
                        return
                    frame = self._frames.popleft()
                    self._bytes -= len(frame)
                    self._writing_since = time.monotonic()
                # Single writer prevents JSON/input interleaving even when the
                # kernel accepts only part of a frame.
                view = memoryview(frame)
                while view:
                    try:
                        written = self._write(self._fd, view)
                    except InterruptedError:
                        continue
                    if written <= 0 or written > len(view):
                        raise OSError('Invalid/zero-length BLE pipe write')
                    view = view[written:]
                    with self._condition:
                        self._writing_since = time.monotonic() if view else None
        except Exception as error:
            self._fail(error)
        finally:
            # The writer owns closure: never close a descriptor while another
            # thread can still be blocked writing to it (descriptor reuse).
            try:
                if self._on_close is not None:
                    self._on_close()
            except Exception:
                _logger.debug('BLE pipe close failed', exc_info=True)
            self._finished.set()

    def _watchdog(self):
        while not self._finished.wait(min(0.1, self._write_timeout / 4)):
            with self._condition:
                since, failed = self._writing_since, self._failure is not None
            if failed:
                return
            if since is not None and time.monotonic() - since >= self._write_timeout:
                self._fail(TimeoutError('BLE pipe write made no progress before its deadline'))
                return

    def close(self, timeout=0.5):
        """Drain healthy output, without indefinitely waiting for a stalled parent."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)
        return not self._thread.is_alive()
