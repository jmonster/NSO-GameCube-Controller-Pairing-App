"""Read the existing 66-byte report / JSON-line helper protocol.

This module has no Bluetooth dependencies. Callback errors (including a full
input queue) propagate to the owner, which must retire the affected helper.
"""
import json
import logging
import subprocess

from ..controller_constants import MAX_SLOTS


def read_events(stream, on_data, on_event):
    while True:
        header = stream.read(1)
        if not header:
            raise EOFError('BLE helper closed stdout')
        if header == b'\xff':
            packet = bytearray()
            while len(packet) < 65:
                part = stream.read(65 - len(packet))
                if not part:
                    raise EOFError('Truncated BLE input report')
                packet.extend(part)
            if packet[0] >= MAX_SLOTS:
                raise ValueError('Invalid BLE input slot')
            on_data(packet[0], bytes(packet[1:]))
        else:
            line = header + stream.readline(65536)
            if not line.endswith(b'\n') or len(line) > 65536:
                raise ValueError('Truncated or oversized BLE event')
            if not line.strip():
                continue
            event = json.loads(line.decode('utf-8'))
            if not isinstance(event, dict) or not isinstance(event.get('e'), str):
                raise ValueError('Invalid BLE event')
            if 's' in event and (type(event['s']) is not int or not 0 <= event['s'] < MAX_SLOTS):
                raise ValueError('Invalid BLE event slot')
            # _owner is local metadata, never accepted from the wire.
            event.pop('_owner', None)
            on_event(event)


def reap_helper(proc):
    """Give an idle helper EOF, then escalate to termination if it stays alive."""
    try:
        if proc.poll() is None:
            # Existing helpers clean up on stdin EOF, not SIGTERM. Preserve that
            # opportunity on ordinary idle command pipes before escalation.
            proc.stdin.close()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            logging.getLogger(__name__).exception('Could not reap BLE helper')
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
