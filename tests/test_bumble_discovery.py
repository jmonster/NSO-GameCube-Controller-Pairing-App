"""Regression coverage for Jared Brick's advertisement-name fix and scan cleanup."""
import asyncio
import types
import unittest
from _support import load_definitions


class Device:
    def __init__(self, advertisements=()):
        self.advertisements = advertisements
        self.listeners = []
        self.started = asyncio.Event()
        self.stopped = 0

    def on(self, event, callback):
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        self.listeners.remove(callback)

    async def start_scanning(self, **kwargs):
        self.started.set()
        for adv in self.advertisements:
            for callback in self.listeners:
                callback(adv)

    async def stop_scanning(self):
        self.stopped += 1


def advertisement(name, address='AA:BB:CC:DD:EE:FF', fallback=''):
    return types.SimpleNamespace(address=address, rssi=-42,
                                 data={0x09: name}, name=fallback)


class BumbleDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, advertisements=()):
        definitions = load_definitions('ble/bumble_backend.py', {'scan_only'},
                                      {'asyncio': asyncio}, class_name='BumbleBackend')
        backend = types.SimpleNamespace(_device=Device(advertisements), _connections={})
        backend.scan_only = types.MethodType(definitions['scan_only'], backend)
        return backend

    async def test_decoded_names_are_not_discarded(self):
        for raw, expected in [('Nintendo', 'Nintendo'), (b'Nintendo', 'Nintendo'),
                              (None, 'fallback'), (b'\xff', '\ufffd')]:
            with self.subTest(raw=raw):
                backend = self.backend([advertisement(raw, fallback='fallback')])
                results = await backend.scan_only(0)
                self.assertEqual(results[0]['name'], expected)
                self.assertEqual(backend._device.listeners, [])

    async def test_cancel_stops_scan_and_removes_only_its_listener(self):
        backend = self.backend()
        other = lambda event: None
        backend._device.listeners.append(other)
        task = asyncio.create_task(backend.scan_only(60))
        await backend._device.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(backend._device.stopped, 1)
        self.assertEqual(backend._device.listeners, [other])

    async def test_repeated_scans_do_not_leak_listeners(self):
        backend = self.backend([advertisement('Nintendo')])
        for _ in range(3):
            self.assertEqual(len(await backend.scan_only(0)), 1)
            self.assertEqual(backend._device.listeners, [])

    async def test_connected_devices_are_excluded(self):
        backend = self.backend([advertisement('Nintendo')])
        backend._connections['AA:BB:CC:DD:EE:FF'] = object()
        self.assertEqual(await backend.scan_only(0), [])
