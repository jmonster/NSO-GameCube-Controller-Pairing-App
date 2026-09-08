"""Exercise the real Bleak state machine with deterministic GATT/OS fakes."""
import asyncio
import queue
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from _support import fake_module, load_module

SW2 = 'ab7de9be-89fe-49ad-828f-118f09df7fd0'
CONTROL = '00c5af5d-1964-4e30-8f51-1956f96bd280'


def characteristic(handle, properties):
    # Identical UUIDs force the backend to use actual discovered objects.
    return types.SimpleNamespace(uuid='duplicate-uuid', handle=handle, properties=properties)


class BleakSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients, self.scanners = [], []
        self.reports = [bytes(63)]
        self.services = [types.SimpleNamespace(uuid=CONTROL, characteristics=[characteristic(5, ['write'])]),
                         types.SimpleNamespace(uuid=SW2, characteristics=[
                             characteristic(10, ['read', 'notify']),
                             characteristic(14, ['read', 'notify']),
                             characteristic(18, ['write-without-response']),
                             characteristic(20, ['write-without-response']),
                             characteristic(22, ['write-without-response']),
                             characteristic(26, ['notify'])])]
        self.fail_connect = False
        self.fail_notify = False
        self.connect_gate = None
        self.advertisements = []
        test = self

        class Client:
            def __init__(self, target, *, timeout, disconnected_callback):
                self.callback = disconnected_callback
                self.is_connected = False
                self.services = test.services
                self.mtu_size = 185
                self.disconnect_count = 0
                self.writes = []
                self.subscribed = asyncio.Event()
                self.input_callback = None
                self.input_char = None
                test.clients.append(self)

            async def connect(self):
                self.is_connected = True
                if test.fail_connect:
                    raise RuntimeError('OS discovery failed after connect')
                if test.connect_gate:
                    await test.connect_gate.wait()

            async def disconnect(self):
                self.disconnect_count += 1
                self.is_connected = False
                self.callback(self)

            async def write_gatt_char(self, char, data, *, response):
                test.assertNotIsInstance(char, str)
                self.writes.append((char.handle, bytes(data), response))

            async def start_notify(self, char, callback):
                test.assertNotIsInstance(char, str)
                if test.fail_notify:
                    raise RuntimeError('permission denied')
                self.input_char, self.input_callback = char, callback
                self.subscribed.set()
                for report in test.reports:
                    callback(char, bytearray(report))

        class Scanner:
            def __init__(self, detection_callback):
                self.callback = detection_callback
                self.started = asyncio.Event()
                self.stopped = 0
                test.scanners.append(self)

            async def start(self):
                self.started.set()
                for addr in test.advertisements:
                    self.callback(types.SimpleNamespace(address=addr, name='Nintendo'),
                                  types.SimpleNamespace(rssi=-40, manufacturer_data={}, service_uuids=[]))

            async def stop(self):
                self.stopped += 1

        modules = {'bleak': fake_module(BleakClient=Client, BleakScanner=Scanner),
                   'bleak.backends.characteristic': fake_module(BleakGATTCharacteristic=object),
                   'bleak.backends.device': fake_module(BLEDevice=object),
                   'bleak.backends.scanner': fake_module(AdvertisementData=object)}
        with patch.dict(sys.modules, modules):
            self.module = load_module('ble/bleak_backend.py')
        self.module._INPUT_READY_TIMEOUT = 0.01
        self.module._log = lambda message: None
        self.module.sys = types.SimpleNamespace(platform='darwin')
        self.backend = self.module.BleakBackend()
        self.data = queue.Queue()
        self.status = []
        self.disconnected = Mock()

    async def asyncTearDown(self):
        await self.backend.close()

    async def connect(self, address='aabb-ccdd'):
        return await self.backend.connect_device(address, 0, self.data,
                                                 self.status.append, self.disconnected)

    async def test_write_without_input_cannot_report_connected(self):
        self.reports = []
        self.assertIsNone(await self.connect())
        self.assertNotIn('Connected via BLE', self.status)
        self.assertEqual(self.clients[0].disconnect_count, 1)
        self.assertFalse(self.backend._clients)
        self.disconnected.assert_not_called()

    async def test_unrelated_service_is_not_probed_with_writes(self):
        self.services = [types.SimpleNamespace(uuid='unrelated',
                                               characteristics=[characteristic(1, ['write', 'notify'])])]
        self.assertIsNone(await self.connect())
        self.assertFalse(self.clients[0].writes)
        self.assertEqual(self.clients[0].disconnect_count, 1)

    async def test_failed_subscription_is_not_ready(self):
        self.fail_notify = True
        self.assertIsNone(await self.connect())
        self.assertEqual(self.clients[0].disconnect_count, 1)
        self.assertNotIn('Connected via BLE', self.status)

    async def test_only_documented_input_size_and_channel_are_accepted(self):
        self.reports = [bytes(n) for n in (0, 29, 30, 62, 64, 128, 63)]
        self.assertEqual(await self.connect(), 'AABB-CCDD')
        self.assertEqual(self.backend.malformed_reports, 6)
        self.assertEqual(self.data.qsize(), 1)
        client = self.clients[0]
        self.assertEqual(client.input_char.handle, 14)
        client.input_callback(characteristic(26, ['notify']), bytearray(63))
        self.assertEqual(self.data.qsize(), 1)
        self.assertEqual(self.backend.malformed_reports, 7)
        self.assertTrue(all(type(response) is bool for _, _, response in client.writes))

    async def test_partial_connect_failure_is_cleaned(self):
        self.fail_connect = True
        self.assertIsNone(await self.connect())
        self.assertEqual(self.clients[0].disconnect_count, 1)
        self.assertFalse(self.backend._pending)

    async def test_cancellation_during_connect_cleans_client(self):
        self.connect_gate = asyncio.Event()
        task = asyncio.create_task(self.connect())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.clients[0].disconnect_count, 1)
        self.assertFalse(self.backend._clients)
        self.assertFalse(self.backend._pending)

    async def test_cancellation_during_readiness_cleans_client(self):
        self.reports = []
        self.module._INPUT_READY_TIMEOUT = 60
        task = asyncio.create_task(self.connect())
        await asyncio.sleep(0)
        await self.clients[0].subscribed.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.clients[0].disconnect_count, 1)
        self.disconnected.assert_not_called()

    async def test_stale_client_callback_cannot_remove_new_session(self):
        await self.connect()
        old = self.clients[0]
        await self.backend.disconnect('aabb-ccdd')
        await self.connect()
        new = self.clients[1]
        old.callback(old)
        self.assertIs(self.backend._clients['AABB-CCDD'], new)
        self.assertIn('AABB-CCDD', self.backend._cmd_chars)
        self.disconnected.assert_not_called()

    async def test_input_overflow_notifies_once_and_disconnects(self):
        self.data = queue.Queue(maxsize=1)
        await self.connect()
        client = self.clients[0]
        client.input_callback(client.input_char, bytearray(63))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.disconnected.assert_called_once()
        self.assertFalse(self.backend._clients)
        self.assertEqual(client.disconnect_count, 1)

    async def test_targeted_reconnect_never_falls_through_to_another_controller(self):
        self.advertisements = ['TARGET-UUID', 'OTHER-UUID']
        self.backend._connect_and_init = AsyncMock(return_value=None)
        result = await self.backend.scan_and_connect(0, self.data, self.status.append,
                                                    self.disconnected, target_address='target-uuid',
                                                    scan_timeout=0)
        self.assertIsNone(result)
        self.assertEqual(self.backend._connect_and_init.await_count, 1)
        self.assertEqual(self.backend._connect_and_init.call_args.args[0], 'TARGET-UUID')

    async def test_imported_mac_on_macos_requires_explicit_selection(self):
        self.backend._connect_and_init = AsyncMock(return_value=None)
        await self.backend.scan_and_connect(0, self.data, self.status.append, self.disconnected,
                                            target_address='AA:BB:CC:DD:EE:FF', scan_timeout=0)
        self.backend._connect_and_init.assert_not_called()
        self.assertFalse(self.scanners)

    async def test_scan_cancellation_stops_scanner(self):
        task = asyncio.create_task(self.backend.scan_only(60))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.scanners[0].stopped, 1)
        self.assertFalse(self.backend._scanners)

    async def test_repeated_advertisements_update_but_retired_scanner_cannot(self):
        found = []
        await self.backend.start_scan(found.append)
        old = self.scanners[0]
        device = types.SimpleNamespace(address='aa-bb', name='Nintendo')
        for rssi in (-60, -40):
            old.callback(device, types.SimpleNamespace(rssi=rssi, manufacturer_data={}, service_uuids=[]))
        self.assertEqual([d['rssi'] for d in found], [-60, -40])
        await self.backend.stop_scan()
        await self.backend.start_scan(found.append)
        old.callback(device, types.SimpleNamespace(rssi=-20, manufacturer_data={}, service_uuids=[]))
        self.assertEqual(len(found), 2)

    async def test_close_cancels_pending_connection_and_stream_scan(self):
        self.connect_gate = asyncio.Event()
        task = asyncio.create_task(self.connect())
        await asyncio.sleep(0)
        await self.backend.start_scan(lambda dev: None)
        await self.backend.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.backend._clients)
        self.assertEqual(self.scanners[0].stopped, 1)

    async def test_windows_request_lifetime_and_multicontroller_release(self):
        self.module.sys.platform = 'win32'
        self.module.platform = types.SimpleNamespace(version=lambda: '10.0.22000')
        request = Mock()
        requester = Mock(request_preferred_connection_parameters=Mock(return_value=request))
        client = types.SimpleNamespace(_backend=types.SimpleNamespace(_requester=requester))
        self.backend._clients['first'] = client
        with patch.dict(sys.modules, {'winrt.windows.devices.bluetooth': fake_module(
                BluetoothLEPreferredConnectionParameters=types.SimpleNamespace(throughput_optimized=object()))}):
            self.backend._request_connection_parameters('first', client)
            self.assertIs(self.backend._conn_param_requests['first'], request)
            self.backend._clients['second'] = object()
            self.backend._request_connection_parameters('second', self.backend._clients['second'])
            self.assertFalse(self.backend._conn_param_requests)
            request.close.assert_called_once()
        self.backend._clients.clear()

    async def test_live_connection_is_not_stolen_by_a_second_attempt(self):
        await self.connect()
        self.assertIsNone(await self.connect())
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.clients[0].disconnect_count, 0)
