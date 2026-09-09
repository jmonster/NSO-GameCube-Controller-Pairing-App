"""Ordered, bounded stdout delivery; no controller protocol interpretation."""
from collections import deque
import os
import threading
import time


class PipeWriter:
    """One raw-fd writer; a stalled write fails the helper, not its event loop.

    The duplicate descriptor belongs only to the writer. On terminal failure a
    blocked OS write may outlive close(); the helper must exit after cleanup.
    The daemon writer cannot hold Python's buffered-stdout lock at process exit.
    """

    def __init__(self, fd, *, max_messages=512, max_bytes=128 * 1024,
                 stall_timeout=5.0):
        if max_messages <= 0 or max_bytes <= 0 or stall_timeout <= 0:
            raise ValueError('Output limits must be positive')
        self._fd = os.dup(fd)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.setmode(self._fd, os.O_BINARY)
        except Exception:
            os.close(self._fd)
            raise
        self._condition = threading.Condition()
        self._pending = deque()
        self._messages = self._bytes = 0  # Include the in-flight message.
        self._max_messages, self._max_bytes = max_messages, max_bytes
        self._stall_timeout = stall_timeout
        self._progress = time.monotonic()
        self._closing = False
        self.error = None
        self.on_failure = None
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._watchdog = threading.Thread(target=self._watch, daemon=True)
        self._writer.start()
        self._watchdog.start()

    def submit(self, packet):
        """Snapshot a complete message; never wait for pipe space or drop-oldest."""
        packet = bytes(packet)
        if not packet:
            return True
        with self._condition:
            if self._closing or self.error is not None:
                return False
            if (self._messages >= self._max_messages
                    or self._bytes + len(packet) > self._max_bytes):
                error = BufferError('BLE stdout queue exhausted')
            else:
                if not self._messages:
                    self._progress = time.monotonic()
                self._messages += 1
                self._bytes += len(packet)
                self._pending.append(packet)
                self._condition.notify_all()
                return True
        self.fail(error)
        return False

    def fail(self, error):
        with self._condition:
            if self.error is not None:
                return
            self.error = error
            self._pending.clear()
            self._condition.notify_all()
        # Wake the command task out-of-band: stdout itself is no longer usable.
        callback = self.on_failure
        if callback is not None:
            callback(error)

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._pending or self._closing or self.error is not None)
                    if self.error is not None or not self._pending:
                        return
                    packet = self._pending.popleft()
                remaining = memoryview(packet)
                while remaining:
                    with self._condition:
                        if self.error is not None:
                            return
                    try:
                        written = os.write(self._fd, remaining)
                    except InterruptedError:
                        continue
                    if written <= 0:
                        raise BrokenPipeError('BLE stdout write made no progress')
                    remaining = remaining[written:]
                    with self._condition:
                        self._progress = time.monotonic()
                        self._condition.notify_all()
                with self._condition:
                    self._messages -= 1
                    self._bytes -= len(packet)
                    self._condition.notify_all()
        except Exception as exc:
            self.fail(exc)
        finally:
            os.close(self._fd)

    def _watch(self):
        with self._condition:
            while self.error is None:
                if not self._messages:
                    if self._closing:
                        return
                    self._condition.wait()
                    continue
                left = self._stall_timeout - (time.monotonic() - self._progress)
                if left <= 0:
                    break
                self._condition.wait(left)
            else:
                return
        self.fail(TimeoutError('BLE stdout stopped making progress'))

    def close(self, timeout=1.0):
        """Drain healthy output; never join a stalled OS write indefinitely."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._writer.join(timeout)
        if self._writer.is_alive():
            self.fail(TimeoutError('BLE stdout did not drain during shutdown'))
        self._watchdog.join(timeout)
        return self.error is None
