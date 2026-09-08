"""Version 2 framing: generation-tagged input and JSON-line events."""
import json
import struct

MAX_JSON_BYTES = 64 * 1024
MAX_SLOTS = 4
PROTOCOL_VERSION = 2
INPUT_HEADER = struct.Struct('>BQ')  # wire slot + unsigned 64-bit generation


def valid_generation(value):
    return type(value) is int and 0 < value < 2 ** 64


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
            raise ProtocolError('Legacy BLE protocol is not supported; restart both processes')
        if header == b'\xfe':
            slot, generation = INPUT_HEADER.unpack(_read_exact(stream, INPUT_HEADER.size))
            if slot >= MAX_SLOTS or not valid_generation(generation):
                raise ProtocolError('Invalid BLE input slot/generation')
            report = _read_exact(stream, 64)
            on_data(slot, generation, report)
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
        if event['e'].startswith('_') or any(key.startswith('_') for key in event):
            raise ProtocolError('Reserved internal metadata in BLE event')
        slot = event.get('s')
        if event['e'] in {'status', 'connected', 'disconnected', 'connect_error',
                           'devices_found', 'device_detected'} and slot is None:
            raise ProtocolError('Missing slot in session event')
        if slot is not None and (type(slot) is not int or not 0 <= slot < MAX_SLOTS):
            raise ProtocolError('Invalid BLE event slot')
        if slot is not None and not valid_generation(event.get('g')):
            raise ProtocolError('Missing or invalid BLE event generation')
        if event['e'] == 'ready' and (type(event.get('protocol')) is not int or event['protocol'] != PROTOCOL_VERSION):
            raise ProtocolError('Incompatible BLE subprocess protocol')
        on_event(event)
