import socket
import struct
import time
import unittest
from unittest.mock import Mock, patch
from _support import fake_module, load_module

vg = load_module('virtual_gamepad.py')
module = load_module('dsu_server.py', {'virtual_gamepad': vg})


def request(kind, payload=b''):
    packet = bytearray(b'DSUC') + bytearray(struct.pack('<HHII', 1001, 4 + len(payload), 0, 123))
    packet += struct.pack('<I', kind) + payload
    module._finalize_crc(packet)
    return bytes(packet)


class DSUStabilityTests(unittest.TestCase):
    def setUp(self):
        self.server = module.DSUServer()
        self.addCleanup(self.server.stop)

    def test_all_truncated_headers_bad_lengths_and_crc_are_rejected(self):
        good = request(module.MSG_TYPE_REQ_VERSION)
        for n in range(len(good)):
            self.assertIsNone(module._parse_request(good[:n]))
        bad = bytearray(good)
        bad[-1] ^= 1
        self.assertIsNone(module._parse_request(bad))
        self.assertEqual(module._parse_request(good + b'trailing'),
                         (module.MSG_TYPE_REQ_VERSION, good))
        for size in (0, 3, 1000):
            bad = bytearray(good)
            struct.pack_into('<H', bad, 6, size)
            module._finalize_crc(bad)
            self.assertIsNone(module._parse_request(bad))

    def test_invalid_port_count_cannot_read_past_packet_or_reply(self):
        self.server._reply = Mock()
        for count in (-1, 5, 0x7fffffff):
            self.server._handle_request(request(module.MSG_TYPE_REQ_PORTS,
                                        struct.pack('<i', count)), ('127.0.0.1', 1))
        self.server._reply.assert_not_called()

    def test_subscriptions_filter_slots_and_macs_and_bound_storage(self):
        self.server._slot_connected = [True] * 4
        self.server.MAX_SUBSCRIPTIONS = 3
        for number, payload in enumerate((bytes([1, 2]) + bytes(6),
                                         bytes([2, 0]) + bytes(5) + b'\x03',
                                         bytes(8), bytes(8))):
            self.server._handle_request(request(module.MSG_TYPE_REQ_DATA, payload),
                                        ('127.0.0.1', number))
        self.assertIn(('127.0.0.1', 0), self.server._subscribers_snapshot[2])
        self.assertNotIn(('127.0.0.1', 0), self.server._subscribers_snapshot[0])
        self.assertIn(('127.0.0.1', 1), self.server._subscribers_snapshot[3])
        self.assertEqual(len(self.server._subscribers), 3)
        with patch.object(module.time, 'monotonic', return_value=time.monotonic() + 6):
            self.server._prune_subscribers()
        self.assertFalse(self.server._subscribers)
        self.assertEqual(self.server._subscribers_snapshot, ((), (), (), ()))

    def test_busy_listener_prunes_without_waiting_for_receive_timeout(self):
        sock = Mock()
        self.server._sock = sock
        self.server._running = True
        calls = []
        def receive(size):
            calls.append(size)
            if len(calls) == 3:
                self.server._running = False
            return request(module.MSG_TYPE_REQ_VERSION), ('127.0.0.1', 1)
        sock.recvfrom.side_effect = receive
        self.server._prune_subscribers = Mock()
        with patch.object(module.select, 'select', return_value=([sock], [], [])), \
             patch.object(module.time, 'monotonic', side_effect=[0, 2, 4, 6]):
            self.server._listen_loop()
        self.assertEqual(self.server._prune_subscribers.call_count, 3)

    def test_backpressure_reply_and_data_send_do_not_raise(self):
        sock = Mock()
        sock.sendto.side_effect = BlockingIOError()
        self.server._sock = sock
        self.server._subscribers_snapshot = ((('127.0.0.1', 1),), (), (), ())
        self.server._handle_request(request(module.MSG_TYPE_REQ_VERSION), ('127.0.0.1', 1))
        self.server.update_slot(0, self.server._make_empty_state())
        self.assertEqual(sock.sendto.call_count, 2)
        self.server._slot_packet_counter[0] = 0xffffffff
        self.server.update_slot(0, self.server._make_empty_state())
        self.assertEqual(self.server._slot_packet_counter[0], 0)

    def test_bind_failure_closes_socket_and_does_not_cache_singleton(self):
        sock = Mock()
        sock.bind.side_effect = OSError('address in use')
        with patch.object(module.socket, 'socket', return_value=sock), \
             patch.object(module, '_server_instance', None), \
             patch.object(module, '_server_refcount', 0):
            with self.assertRaises(RuntimeError):
                module._acquire_server()
            self.assertIsNone(module._server_instance)
            self.assertEqual(module._server_refcount, 0)
        sock.close.assert_called_once()

    def test_real_loopback_socket_survives_malformed_traffic_and_streams_correct_slot(self):
        self.server.BASE_PORT = 0
        self.server.start()
        self.assertEqual(self.server._sock.gettimeout(), 0.0)
        self.assertEqual(self.server._sock.getsockname()[0], '127.0.0.1')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            target = ('127.0.0.1', self.server.port)
            for n in range(20):
                client.sendto((b'DSUC' + bytes(20))[:n], target)
            client.sendto(request(module.MSG_TYPE_REQ_VERSION), target)
            response, _ = client.recvfrom(1024)
            self.assertEqual(response[:4], b'DSUS')
            self.assertEqual(struct.unpack_from('<H', response, 20)[0], 1001)
            self.server.set_slot_connected(2, True)
            client.sendto(request(module.MSG_TYPE_REQ_DATA, bytes([1, 2]) + bytes(6)), target)
            deadline = time.monotonic() + 2
            while not self.server._subscribers_snapshot[2] and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertTrue(self.server._subscribers_snapshot[2])
            self.server.update_slot(2, self.server._make_empty_state())
            packet, _ = client.recvfrom(1024)
            self.assertEqual(len(packet), 100)
            self.assertEqual(packet[20], 2)
            thread = self.server._thread
            self.server.stop()
            self.assertFalse(thread.is_alive())

    def test_udp_client_reset_does_not_terminate_listener(self):
        sock = Mock()
        self.server._sock = sock
        self.server._running = True
        sock.recvfrom.side_effect = [ConnectionResetError(), ConnectionRefusedError(),
            (request(module.MSG_TYPE_REQ_VERSION), ('127.0.0.1', 1))]
        def reply(*args):
            self.server._running = False
        sock.sendto.side_effect = reply
        with patch.object(module.select, 'select', return_value=([sock], [], [])):
            self.server._listen_loop()
        self.assertEqual(sock.recvfrom.call_count, 3)
        sock.sendto.assert_called_once()
