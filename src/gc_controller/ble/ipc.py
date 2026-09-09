"""Framing for the existing 66-byte BLE data / JSON-line subprocess protocol."""
import json

MAX_JSON_BYTES = 64 * 1024
MAX_SLOTS = 4


class ProtocolError(ValueError):
    """The stream cannot safely be interpreted as controller input."""


def _read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError('BLE subprocess closed during an input report')
        chunks.extend(chunk)
    return bytes(chunks)


def read_event_stream(stream, on_data, on_event):
    """Read until EOF; malformed/truncated traffic and callback failures propagate.

    A caller must treat termination as transport loss unless it intentionally
    retired this process. Silently skipping corrupted input can leave buttons
    pressed indefinitely. JSON has a size bound; payload bytes are never parsed
    as delimiters. Reads need not fill the requested buffer in one operation.
    """
    while True:
        header = stream.read(1)
        if not header:
            return
        if header == b'\xff':
            packet = _read_exact(stream, 65)
            if packet[0] >= MAX_SLOTS:
                raise ProtocolError('Invalid BLE input slot')
            on_data(packet[0], packet[1:])
            continue
        line = bytearray(header)
        while not line.endswith(b'\n'):
            if len(line) >= MAX_JSON_BYTES:
                raise ProtocolError('BLE event exceeds size limit')
            chunk = stream.readline(MAX_JSON_BYTES - len(line))
            if not chunk:
                raise EOFError('BLE subprocess closed during a JSON event')
            line.extend(chunk)
        try:
            event = json.loads(line.decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError('Malformed BLE event') from exc
        if not isinstance(event, dict) or not isinstance(event.get('e'), str):
            raise ProtocolError('BLE event must be an object with an event name')
        slot = event.get('s')
        if slot is not None and (type(slot) is not int or not 0 <= slot < MAX_SLOTS):
            raise ProtocolError('Invalid BLE event slot')
        on_event(event)


class HelperSession:
    """One helper's initialization mailbox and terminal state (parent-only)."""

    def __init__(self, process):
        import collections
        import threading
        self.process = process
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.init_events = collections.deque()
        self.ended = False
        self.initialized = False
        self.failure = None
        self.loss_handled = False
        self.cleanup_started = False

    def init_event(self, event):
        with self.condition:
            if not self.ended:
                self.init_events.append(event)
                self.condition.notify_all()

    def wait_init(self, timeout):
        with self.condition:
            self.condition.wait_for(lambda: self.ended or self.init_events, timeout)
            if not self.ended and self.init_events:
                return self.init_events.popleft()
        return None

    def end(self, reason=None):
        """Wake all waiters once. None means deliberate retirement, not failure."""
        with self.condition:
            if self.ended:
                return False
            self.ended = True
            self.stop_event.set()
            self.initialized = False
            self.failure = reason
            self.condition.notify_all()
            return True

    def claim_loss(self):
        with self.condition:
            if self.failure is None or self.loss_handled:
                return False
            self.loss_handled = True
            return True


def close_helper(session):
    """Reap only this helper; try each escalation even after cleanup errors."""
    import logging
    with session.condition:
        if session.cleanup_started:
            return
        session.cleanup_started = True
        session.end()
    proc = session.process
    try:
        proc.stdin.close()  # Existing child EOF path performs backend cleanup.
    except Exception:
        logging.getLogger(__name__).debug('BLE stdin close failed', exc_info=True)
    for action in (None, proc.terminate, proc.kill):
        try:
            if action is not None:
                action()
            proc.wait(timeout=3)
            return
        except Exception:
            logging.getLogger(__name__).debug('BLE helper cleanup failed', exc_info=True)
    logging.getLogger(__name__).warning('BLE helper could not be reaped')


def retire_input(input_proc, emu_mgr):
    """A broken reader must not prevent the independent output cleanup."""
    import logging
    for operation in (input_proc.stop, emu_mgr.stop):
        try:
            operation()
        except Exception:
            logging.getLogger(__name__).warning('BLE slot cleanup failed', exc_info=True)
