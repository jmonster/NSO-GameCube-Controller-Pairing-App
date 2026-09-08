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
