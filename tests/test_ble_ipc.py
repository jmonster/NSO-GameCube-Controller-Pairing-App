"""Legacy IPC framing: fragmentation is valid; corrupted input is terminal."""
import io
from unittest import TestCase

from _support import load_module

ipc = load_module('ble/ipc.py')


class ShortReads(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3))

    def readline(self, size=-1):
        return super().readline(min(size, 3))


class IpcTests(TestCase):
    def test_fragmented_binary_and_json_keep_order_and_payload_boundaries(self):
        payload = bytes(range(64))
        stream = ShortReads(b'{"e":"ready"}\n\xff\x00' + payload
                            + b'{"e":"disconnected","s":0}\n')
        events = []
        ipc.read_event_stream(stream,
                              lambda si, data: events.append((si, data)), events.append)
        self.assertEqual(events, [{'e': 'ready'}, (0, payload),
                                  {'e': 'disconnected', 's': 0}])

    def test_invalid_frames_fail_instead_of_skipping_transitions(self):
        for frame in (b'\xff\x00short', b'{"e":"ready"}', b'{bad}\n',
                      b'[]\n', b'{"e":1}\n', b'{"e":"status","s":true}\n',
                      b'{"e":"status","s":4}\n', b'\xff\x04' + bytes(64),
                      b'{"e":"\xff"}\n', b'x' * (ipc.MAX_JSON_BYTES + 1)):
            with self.subTest(frame=frame[:30]):
                with self.assertRaises((ipc.ProtocolError, EOFError)):
                    ipc.read_event_stream(io.BytesIO(frame), self.fail, self.fail)

    def test_initialization_mailbox_preserves_order_and_terminal_state(self):
        session = ipc.HelperSession(None)
        session.init_event({'e': 'ready'})
        session.init_event({'e': 'bluez_stopped'})
        self.assertEqual(session.wait_init(0), {'e': 'ready'})
        self.assertEqual(session.wait_init(0), {'e': 'bluez_stopped'})
        self.assertTrue(session.end('EOF'))
        self.assertFalse(session.end())
        session.init_event({'e': 'open_ok'})
        self.assertIsNone(session.wait_init(0))
        self.assertTrue(session.claim_loss())
        self.assertFalse(session.claim_loss())
