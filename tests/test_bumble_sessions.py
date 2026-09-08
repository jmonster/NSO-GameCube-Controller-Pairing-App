import asyncio
import queue
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from _support import fake_module, load_module


class BumbleSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = []
        self.status = []
        self.disconnected = Mock()
        self.data = queue.Queue()
        self.pair_gate = None
        self.fail_discovery = False
        self.reports = [bytes(63)]
        self.init_success = True
        self.last_connect_options = None
        self.input_callback = None
        test = self

        class Connection:
            def __init__(self):
                self.callbacks = {}
                self.disconnect_count = 0
            def on(self, name, callback):
                self.callbacks[name] = callback
            async def pair(self):
                if test.pair_gate:
                    await test.pair_gate.wait()
            async def disconnect(self):
                self.disconnect_count += 1
                self.callbacks.get('disconnection', lambda r: None)(0)

        class Device:
            def __init__(self):
                self.public_address = b'\x06\x05\x04\x03\x02\x01'
            async def connect(self, target, **kwargs):
                test.last_connect_options = kwargs
                connection = Connection()
                test.connections.append(connection)
                return connection

        class Peer:
            def __init__(self, connection):
                self.services = []
            async def request_mtu(self, size):
                pass
            async def discover_services(self):
                if test.fail_discovery:
                    raise RuntimeError('GATT error')

        class Address:
            PUBLIC_DEVICE_ADDRESS = 0
            def __init__(self, *args):
                pass

        modules = {'bumble': fake_module(smp=object()),
                   'bumble.device': fake_module(Device=Device, Peer=Peer,
                       ConnectionParametersPreferences=lambda **kwargs: kwargs),
                   'bumble.hci': fake_module(Address=Address, HCI_LE_1M_PHY=1, HCI_LE_2M_PHY=2,
                                             OwnAddressType=types.SimpleNamespace(PUBLIC=0)),
                   'bumble.pairing': fake_module(PairingConfig=object, PairingDelegate=object),
                   'bumble.transport': fake_module(open_transport=AsyncMock())}
        with patch.dict(sys.modules, modules):
            self.module = load_module('ble/bumble_backend.py')
        self.module._INPUT_READY_TIMEOUT = 0.01
        self.backend = self.module.BumbleBackend()
        self.backend._device = Device()
        self.backend._log_connection_params = lambda *args: None

        async def init(**kwargs):
            test.input_callback = kwargs['on_input']
            for report in test.reports:
                kwargs['on_input'](report)
            return test.init_success
        self.module.sw2_init = init

    async def asyncTearDown(self):
        await self.backend.close()

    async def connect(self, target='AA:BB:CC:DD:EE:FF'):
        return await self.backend.scan_and_connect(0, self.data, self.status.append,
                                                   self.disconnected, target_address=target)

    async def test_public_initiator_matches_registered_public_identity(self):
        self.assertEqual(await self.connect(), 'AA:BB:CC:DD:EE:FF')
        self.assertEqual(self.last_connect_options['own_address_type'], 0)
        self.assertEqual(self.module.public_host_address(self.backend._device), b'\x06\x05\x04\x03\x02\x01')

    async def test_zero_missing_or_short_public_address_never_connects(self):
        for address in (bytes(6), b'', b'abc', None):
            with self.subTest(address=address):
                self.backend._device.public_address = address
                self.backend._device.random_address = b'123456'
                self.assertIsNone(await self.connect())
                self.assertFalse(self.connections)
                with self.assertRaises(ValueError):
                    self.module.public_host_address(self.backend._device)

    async def test_discovery_exception_cleans_partial_connection(self):
        self.fail_discovery = True
        self.assertIsNone(await self.connect())
        self.assertEqual(self.connections[0].disconnect_count, 1)
        self.assertFalse(self.backend._connections)
        self.assertFalse(self.backend._peers)
        self.assertFalse(self.backend._pending)
        self.disconnected.assert_not_called()

    async def test_cancel_pairing_awaits_security_tasks_and_disconnect(self):
        self.pair_gate = asyncio.Event()
        task = asyncio.create_task(self.connect())
        await asyncio.sleep(0)
        self.connections[0].callbacks['security_request'](0)
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.connections[0].disconnect_count, 1)
        self.assertFalse(self.backend._pending)
        self.assertFalse(self.backend._background_tasks)

    async def test_duplicate_pending_target_is_not_connected_twice(self):
        self.pair_gate = asyncio.Event()
        task = asyncio.create_task(self.connect())
        await asyncio.sleep(0)
        self.assertIsNone(await self.connect('aa:bb:cc:dd:ee:ff/P'))
        self.assertEqual(len(self.connections), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_init_success_without_input_is_not_ready(self):
        self.reports = []
        self.assertIsNone(await self.connect())
        self.assertEqual(self.connections[0].disconnect_count, 1)

    async def test_malformed_input_is_not_a_ready_controller(self):
        self.reports = [bytes(n) for n in (0, 30, 62, 64)]
        self.assertIsNone(await self.connect())
        self.assertEqual(self.backend.malformed_reports, 4)
        self.assertTrue(self.data.empty())

    async def test_stale_disconnect_cannot_clear_replacement(self):
        await self.connect()
        old = self.connections[0]
        await self.backend.disconnect('AA:BB:CC:DD:EE:FF/P')
        await self.connect()
        old.callbacks['disconnection'](0)
        self.assertIs(self.backend._connections['AA:BB:CC:DD:EE:FF'], self.connections[1])
        self.assertIn('AA:BB:CC:DD:EE:FF', self.backend._peers)
        self.disconnected.assert_not_called()

    async def test_overflow_notifies_and_closes_instead_of_dropping_release(self):
        self.data = queue.Queue(maxsize=1)
        await self.connect()
        self.input_callback(bytes(63))
        await asyncio.gather(*self.backend._background_tasks)
        self.disconnected.assert_called_once()
        self.assertFalse(self.backend._connections)
        self.assertEqual(self.connections[0].disconnect_count, 1)

    async def test_hci_transport_close_is_awaited(self):
        transport = types.SimpleNamespace(close=AsyncMock())
        self.backend._transport = transport
        await self.backend.close()
        transport.close.assert_awaited_once()
        self.assertIsNone(self.backend._transport)
        self.assertIsNone(self.backend._device)

    async def test_scan_recognizes_additional_gc_prefixes_and_cleans_listener(self):
        for prefix in ('E0:EF:BF', '94:8E:6D'):
            listeners = []
            address = prefix + ':00:00:01'
            async def start(**kwargs):
                for callback in listeners:
                    callback(types.SimpleNamespace(address=address + '/P'))
            device = types.SimpleNamespace(
                on=lambda name, cb: listeners.append(cb),
                remove_listener=lambda name, cb: listeners.remove(cb),
                start_scanning=start, stop_scanning=AsyncMock())
            self.backend._device = device
            self.assertEqual(await self.backend._scan(0.1), address)
            self.assertFalse(listeners)
            device.stop_scanning.assert_awaited_once()

    async def test_company_identification_handles_new_oui_without_false_vendor_ids(self):
        listeners = []
        async def start(**kwargs):
            for company, suffix in ((0x037E, '01'), (0x057E, '02'), (0x0553, '03')):
                for callback in listeners:
                    callback(types.SimpleNamespace(address='AA:BB:CC:00:00:' + suffix,
                        data={0xFF: company.to_bytes(2, 'little') + b'\x01'}))
        device = types.SimpleNamespace(on=lambda name, cb: listeners.append(cb),
            remove_listener=lambda name, cb: listeners.remove(cb),
            start_scanning=start, stop_scanning=AsyncMock())
        self.backend._device = device
        self.assertEqual(await self.backend._scan(0.1), 'AA:BB:CC:00:00:03')
        self.assertFalse(listeners)
