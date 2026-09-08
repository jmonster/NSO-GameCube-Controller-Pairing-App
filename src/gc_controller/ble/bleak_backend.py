"""
Bleak BLE Backend

macOS/Windows BLE backend using the Bleak library.
Verify the documented Nintendo service layout, subscribe to the input channel,
and require a valid input report before publishing a ready connection.

The OS BLE stack handles SMP pairing, MTU negotiation, and encryption automatically.
No elevated privileges needed.
"""

import asyncio
import logging
import platform
import queue
import re
import sys
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .sw2_protocol import (
    LED_MAP, build_led_cmd, translate_ble_native_to_usb,
)

_logger = logging.getLogger(__name__)

# Nintendo BLE manufacturer company ID (from protocol doc)
_NINTENDO_COMPANY_ID = 0x037E
_SW2_SERVICE_UUID = 'ab7de9be-89fe-49ad-828f-118f09df7fd0'
_CONTROL_SERVICE_UUID = '00c5af5d-1964-4e30-8f51-1956f96bd280'
_INPUT_READY_TIMEOUT = 5.0

# Known Nintendo controller name substrings
_NINTENDO_NAME_PATTERNS = (
    'Pro Controller', 'Nintendo', 'Joy-Con', 'HORI', 'NSO', 'DeviceName',
)

# SPI read command used as handshake (same as nso-gc-bridge BLE_HANDSHAKE_READ_SPI)
_HANDSHAKE_CMD = bytearray([
    0x02, 0x91, 0x01, 0x04,
    0x00, 0x08, 0x00, 0x00, 0x40, 0x7e, 0x00, 0x00, 0x00, 0x30, 0x01, 0x00
])

# Init commands sent after handshake (from nso-gc-bridge)
_DEFAULT_REPORT_DATA = bytearray([
    0x03, 0x91, 0x00, 0x0d, 0x00, 0x08,
    0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
])

_SET_INPUT_MODE = bytearray([
    0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x03, 0x30
])


def _log(msg: str):
    """Debug log to stderr (visible in terminal, not in IPC pipe)."""
    _logger.debug(msg)
    print(f"[bleak] {msg}", file=sys.stderr, flush=True)


def _normalize_address(addr: str | None) -> str | None:
    """Strip /P or /R suffix from a BLE address (Linux Bumble format)."""
    if not addr:
        return addr
    return re.sub(r'/[PR]$', '', addr.strip().upper())


# MAC address pattern: XX:XX:XX:XX:XX:XX
_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')


def _is_mac_address(addr: str) -> bool:
    """Return True if addr looks like a MAC address (not a CoreBluetooth UUID)."""
    return bool(_MAC_RE.match(addr))


class BleakBackend:
    """Manages BLE connections via Bleak (macOS/Windows).

    A successful write is not readiness: the expected input channel must
    deliver a correctly sized report, and every callback owns its client.
    """

    def __init__(self):
        self._clients: dict[str, BleakClient] = {}  # identifier -> BleakClient
        self._write_chars: dict[str, object] = {}   # identifier -> handshake char (command writes)
        self._cmd_chars: dict[str, object] = {}     # identifier -> command channel char (for vibration)
        self._last_scan: dict[str, BLEDevice] = {}  # normalized address -> BLEDevice
        self._pending = {}
        self._cleanup_tasks = set()
        self._conn_param_requests = {}
        self._scanners = set()
        self._closing = False
        self.malformed_reports = 0

    @property
    def is_open(self) -> bool:
        return True

    async def open(self):
        """No-op — the OS BLE stack is always available in userspace."""
        self._closing = False

    @staticmethod
    def _log_connection_params(client: BleakClient, address: str):
        """Log negotiated BLE connection parameters for latency diagnostics."""
        parts = []
        try:
            parts.append(f"MTU={client.mtu_size}")
        except Exception:
            pass
        if sys.platform == 'win32':
            try:
                from bleak.backends.winrt.client import BleakClientWinRT
                backend = client._backend
                if isinstance(backend, BleakClientWinRT):
                    session = backend._session
                    if session:
                        status = session.session_status
                        parts.append(f"status={status}")
            except Exception:
                pass
        _log(f"  BLE connection [{address}]: {', '.join(parts) if parts else 'params not available'}")

    async def scan_and_connect(
        self,
        slot_index: int,
        data_queue: queue.Queue,
        on_status: Callable[[str], None],
        on_disconnect: Callable[[], None],
        target_address: Optional[str] = None,
        exclude_addresses: Optional[list[str]] = None,
        scan_timeout: float = 5.0,
        connect_timeout: float = 15.0,
    ) -> Optional[str]:
        """Scan for an NSO GC controller, connect, and init.

        Uses a scan-first approach with early stop: if target_address is set,
        the scan stops as soon as that device is seen (fast reconnect).
        Otherwise scans for the full timeout, then tries each device.

        Returns device identifier string on success, None on failure.
        """
        target_address = _normalize_address(target_address)
        exclude = set(_normalize_address(a) or a for a in (exclude_addresses or []))

        # On macOS, CoreBluetooth uses UUIDs, not MAC addresses.  A saved
        # MAC from Linux will never match — discard it so we don't waste
        # time waiting for a match that can never happen.
        if target_address and sys.platform == 'darwin' and _is_mac_address(target_address):
            on_status("Saved address belongs to another platform; select the controller again")
            return None

        on_status("Scanning for controller...")
        _log(f"Scanning for {scan_timeout}s (target={target_address})...")

        # Collect devices via detection callback.  For early-stop we poll
        # found_devices instead of signalling an asyncio.Event — Bleak's
        # callback threading varies across platforms and the Event approach
        # is unreliable on macOS.
        found_devices: dict[str, BLEDevice] = {}
        found_adv: dict[str, AdvertisementData] = {}

        def _on_detected(device: BLEDevice, adv: AdvertisementData):
            found_devices[_normalize_address(device.address)] = device
            found_adv[_normalize_address(device.address)] = adv

        scanner = BleakScanner(detection_callback=_on_detected)
        self._scanners.add(scanner)
        try:
            await scanner.start()
            if target_address:
                # Poll every 0.3s for the target instead of sleeping the full timeout
                target_upper = target_address.upper()
                deadline = asyncio.get_event_loop().time() + scan_timeout
                while asyncio.get_event_loop().time() < deadline:
                    if any(a.upper() == target_upper for a in found_devices):
                        _log(f"Target {target_address} found during scan")
                        break
                    await asyncio.sleep(0.3)
                else:
                    _log(f"Target {target_address} not found in {scan_timeout}s")
            else:
                await asyncio.sleep(scan_timeout)
        finally:
            try:
                await scanner.stop()
            finally:
                self._scanners.discard(scanner)

        # On Windows, bonded devices may not appear in scan results (WinRT
        # caches them separately).  If we have a target address that wasn't
        # found in the scan, try connecting directly by address — BleakClient
        # can connect to bonded devices without a prior scan result.
        # Skip this on macOS: CoreBluetooth UUIDs can become stale after
        # re-pairing, and direct-connecting a stale UUID blocks for the full
        # connect_timeout (15s) with no hope of success.
        target_in_scan = target_address and any(
            a.upper() == target_address.upper() for a in found_devices)

        if target_address and not target_in_scan and sys.platform != 'darwin':
            _log(f"Target {target_address} not in scan results, "
                 f"trying direct connect (bonded device?)")
            on_status(f"Connecting to {target_address}...")
            result = await self._connect_and_init(
                target_address, None, slot_index, data_queue,
                on_status, on_disconnect, connect_timeout)
            if result:
                return result
            _log(f"Target {target_address} did not connect; skipping "
                 "non-target devices")
            return None

        if not found_devices:
            on_status("No devices found")
            return None

        # Classify each device as Nintendo-like or not
        def _is_nintendo_like(addr):
            d = found_devices[addr]
            name = (d.name or "").lower()
            adv = found_adv.get(addr)
            if adv:
                md = getattr(adv, 'manufacturer_data', {})
                if _NINTENDO_COMPANY_ID in md:
                    return True
            if name == "devicename" or any(
                    p.lower() in name for p in _NINTENDO_NAME_PATTERNS):
                return True
            return False

        # Only try devices that look like Nintendo controllers (or match
        # the target address).  Trying every nearby BLE device wastes time
        # and causes false-positive handshakes on unrelated peripherals.
        candidates = [
            a for a in found_devices
            if (a == target_address if target_address else _is_nintendo_like(a))
        ]

        if not candidates:
            _log(f"Found {len(found_devices)} device(s) but none look "
                 f"like Nintendo controllers")
            on_status("No controller found")
            return None

        _log(f"Found {len(found_devices)} device(s), "
             f"{len(candidates)} Nintendo-like, trying those...")

        # Sort candidates: Nintendo manufacturer data first, then name
        # match, then by signal strength
        def _sort_key(addr):
            d = found_devices[addr]
            adv = found_adv.get(addr)
            rssi = adv.rssi if adv and adv.rssi is not None else -999
            is_nintendo = False
            if adv:
                md = getattr(adv, 'manufacturer_data', {})
                if _NINTENDO_COMPANY_ID in md:
                    is_nintendo = True
            name = (d.name or "").lower()
            name_match = name == "devicename" or any(
                p.lower() in name for p in _NINTENDO_NAME_PATTERNS)
            return (
                0 if is_nintendo else 1,
                0 if name_match else 1,
                -rssi,
                addr,
            )

        ordered_addrs = sorted(candidates, key=_sort_key)

        # Move target to front if found
        if target_address:
            for addr in ordered_addrs:
                if addr.upper() == target_address.upper():
                    ordered_addrs.remove(addr)
                    ordered_addrs.insert(0, addr)
                    break

        for addr in ordered_addrs:
            if addr in exclude:
                continue
            if addr in self._clients:
                continue

            d = found_devices[addr]
            name = d.name or "(no name)"
            _log(f"  Trying {name} ({addr})...")
            on_status(f"Trying {name}...")

            result = await self._connect_and_init(
                addr, d, slot_index, data_queue,
                on_status, on_disconnect, connect_timeout)
            if result:
                return result

        on_status("No controller found")
        return None

    async def scan_only(self, scan_timeout: float = 10.0) -> list[dict]:
        """Run a full BLE scan and return discovered devices.

        Returns a list of dicts with keys: address, name, rssi,
        manufacturer_data, service_uuids.
        Caches BLEDevice objects in self._last_scan for connect_device().
        """
        _log(f"scan_only: scanning for {scan_timeout}s...")
        found_devices: dict[str, BLEDevice] = {}
        found_adv: dict[str, AdvertisementData] = {}

        def _on_detected(device: BLEDevice, adv: AdvertisementData):
            found_devices[_normalize_address(device.address)] = device
            found_adv[_normalize_address(device.address)] = adv

        scanner = BleakScanner(detection_callback=_on_detected)
        self._scanners.add(scanner)
        try:
            await scanner.start()
            await asyncio.sleep(scan_timeout)
        finally:
            try:
                await scanner.stop()
            finally:
                self._scanners.discard(scanner)

        self._last_scan = dict(found_devices)

        result = []
        for addr, device in found_devices.items():
            adv = found_adv.get(addr)
            rssi = adv.rssi if adv and adv.rssi is not None else -999
            mfg = {}
            svc_uuids = []
            if adv:
                mfg = {str(cid): val.hex() for cid, val in
                       getattr(adv, 'manufacturer_data', {}).items()}
                svc_uuids = list(getattr(adv, 'service_uuids', []))
            result.append({
                'address': addr.upper(),
                'name': device.name or '',
                'rssi': rssi,
                'manufacturer_data': mfg,
                'service_uuids': svc_uuids,
            })

        _log(f"scan_only: found {len(result)} device(s)")
        return result

    async def start_scan(self, on_device_found: Callable[[dict], None]):
        """Start a continuous BLE scan. Calls on_device_found for each new device."""
        await self.stop_scan()
        self._stream_devices: dict[str, BLEDevice] = {}
        self._stream_adv: dict[str, AdvertisementData] = {}
        self._stream_seen: set[str] = set()
        self._stream_callback = on_device_found
        scanner = None

        def _on_detected(device: BLEDevice, adv: AdvertisementData):
            if self._active_scanner is not scanner:
                return
            addr = device.address.upper()
            self._stream_devices[addr] = device
            self._stream_adv[addr] = adv
            if self._active_scanner is not None:
                self._stream_seen.add(addr)
                rssi = adv.rssi if adv and adv.rssi is not None else -999
                mfg = {}
                svc_uuids = []
                if adv:
                    mfg = {str(cid): val.hex() for cid, val in
                           getattr(adv, 'manufacturer_data', {}).items()}
                    svc_uuids = list(getattr(adv, 'service_uuids', []))
                self._stream_callback({
                    'address': addr,
                    'name': device.name or '',
                    'rssi': rssi,
                    'manufacturer_data': mfg,
                    'service_uuids': svc_uuids,
                })

        scanner = self._active_scanner = BleakScanner(detection_callback=_on_detected)
        try:
            await self._active_scanner.start()
        except BaseException:
            await self.stop_scan()
            raise
        _log("start_scan: scanner started")

    async def stop_scan(self):
        """Stop the continuous scan and cache results for connect_device."""
        scanner = getattr(self, '_active_scanner', None)
        if scanner is not None:
            self._active_scanner = None
            try:
                await scanner.stop()
            except Exception:
                pass
            self._active_scanner = None
            self._last_scan = dict(getattr(self, '_stream_devices', {}))
            _log(f"stop_scan: cached {len(self._last_scan)} device(s)")

    async def connect_device(
        self,
        address: str,
        slot_index: int,
        data_queue: queue.Queue,
        on_status: Callable[[str], None],
        on_disconnect: Callable[[], None],
        connect_timeout: float = 15.0,
    ) -> Optional[str]:
        """Connect to a specific address using cached BLEDevice from last scan.

        Returns the device address on success, None on failure.
        """
        address = _normalize_address(address) or address

        if address in self._pending or address in self._clients:
            on_status("Controller already has an active connection")
            return None

        ble_device = self._last_scan.get(address)

        if not ble_device:
            # On Windows, bonded devices may not appear in scan results.
            # Try connecting directly by address — BleakClient handles this.
            _log(f"connect_device: {address} not in scan cache, "
                 f"trying direct connect")
            on_status(f"Connecting to {address}...")
        else:
            name = ble_device.name or "(no name)"
            _log(f"connect_device: connecting to {name} ({address})...")
            on_status(f"Connecting to {name}...")

        return await self._connect_and_init(
            address, ble_device, slot_index, data_queue,
            on_status, on_disconnect, connect_timeout)

    def _forget_client(self, address, client):
        """Only the current client may remove address-keyed state."""
        if self._clients.get(address) is not client:
            return
        self._clients.pop(address, None)
        self._write_chars.pop(address, None)
        self._cmd_chars.pop(address, None)
        request = self._conn_param_requests.pop(address, None)
        if request is not None:
            try:
                request.close()
            except Exception:
                _logger.debug("Failed to release connection parameter request", exc_info=True)

    async def _disconnect_client(self, address, client):
        self._forget_client(address, client)
        try:
            # Call even after a failed connect: Bleak may own partial OS state.
            await asyncio.wait_for(client.disconnect(), 5)
        except Exception:
            _logger.debug("Client cleanup failed for %s", address, exc_info=True)

    def _request_connection_parameters(self, address, client):
        # Selective port of pookee/1b488872: retain the WinRT request for the
        # connection's lifetime. Optimization remains optional, not readiness.
        if sys.platform != 'win32':
            return
        # ThroughputOptimized can reduce concurrent-peripheral capacity (MSDN).
        # Do not keep it active when pairing/using multiple controllers.
        if len(self._clients) > 1:
            for request in self._conn_param_requests.values():
                try:
                    request.close()
                except Exception:
                    _logger.debug("Connection request release failed", exc_info=True)
            self._conn_param_requests.clear()
            return
        try:
            if int(platform.version().split('.')[-1]) < 22000:
                return
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters
            requester = getattr(getattr(client, '_backend', None), '_requester', None)
            if requester is not None:
                request = requester.request_preferred_connection_parameters(
                    BluetoothLEPreferredConnectionParameters.throughput_optimized)
                self._conn_param_requests[address] = request
        except Exception:
            _logger.debug("Optional WinRT connection tuning unavailable", exc_info=True)
        # No undocumented CoreBluetooth selectors on macOS.

    async def _connect_and_init(
        self, address, ble_device, slot_index, data_queue,
        on_status, on_disconnect, connect_timeout,
    ):
        address = _normalize_address(address)
        if self._closing or address in self._pending or address in self._clients:
            return None
        task = asyncio.current_task()
        self._pending[address] = task
        disconnected, input_received = asyncio.Event(), asyncio.Event()
        ready = False
        accepted = False
        client = None

        def lost(owner):
            nonlocal ready
            disconnected.set()
            input_received.set()  # Wake initialization without a full timeout.
            if self._clients.get(address) is not owner:
                return
            self._forget_client(address, owner)
            if ready:
                ready = False
                on_disconnect()

        try:
            client = BleakClient(ble_device if ble_device is not None else address,
                                 timeout=connect_timeout, disconnected_callback=lost)
            self._clients[address] = client
            await client.connect()
            if not client.is_connected or disconnected.is_set():
                return None
            self._request_connection_parameters(address, client)
            self._log_connection_params(client, address)

            services = list(client.services)
            sw2 = [svc for svc in services if svc.uuid.lower() == _SW2_SERVICE_UUID]
            if len(sw2) != 1:
                raise ValueError("Unsupported Nintendo GATT service layout")
            chars = list(sw2[0].characteristics)
            inputs = sorted([c for c in chars if 'read' in c.properties and
                             ('notify' in c.properties or 'indicate' in c.properties)],
                            key=lambda c: c.handle)
            writes = sorted([c for c in chars if 'write-without-response' in c.properties],
                            key=lambda c: c.handle)
            # NSO_GC_BLE_PROTOCOL.md: two input formats, three output channels.
            # Resolve discovered objects, never UUID-only calls or fixed handles.
            if len(inputs) != 2 or len(writes) != 3:
                raise ValueError("Unsupported Nintendo input/output characteristics")
            input_char, cmd_char = inputs[1], writes[1]
            write_chars = [c for svc in services
                           if svc.uuid.lower() in (_CONTROL_SERVICE_UUID, _SW2_SERVICE_UUID)
                           for c in svc.characteristics
                           if 'write' in c.properties or 'write-without-response' in c.properties]
            handshake_char = None
            for char in write_chars:
                for command in (_HANDSHAKE_CMD, bytes((1, 1))):
                    try:
                        await client.write_gatt_char(char, command, response='write' in char.properties)
                        handshake_char = char
                        break
                    except Exception:
                        continue
                if handshake_char is not None:
                    break
            if handshake_char is None:
                raise ValueError("Controller initialization write failed")

            def input_report(sender, value):
                if self._clients.get(address) is not client or disconnected.is_set():
                    return
                if sender.handle != input_char.handle or len(value) != 63:
                    self.malformed_reports += 1
                    return
                try:
                    data_queue.put_nowait(translate_ble_native_to_usb(bytes(value)))
                except Exception:
                    # Do not silently lose a release when the consumer stalls.
                    lost(client)
                    cleanup = asyncio.create_task(self._disconnect_client(address, client))
                    self._cleanup_tasks.add(cleanup)
                    cleanup.add_done_callback(self._cleanup_tasks.discard)
                    return
                input_received.set()

            on_status("Subscribing to controller input...")
            await client.start_notify(input_char, input_report)
            for char, command in ((cmd_char, _DEFAULT_REPORT_DATA),
                                  (cmd_char, build_led_cmd(LED_MAP[slot_index])),
                                  (handshake_char, _SET_INPUT_MODE)):
                try:
                    await client.write_gatt_char(char, command, response='write' in char.properties)
                except Exception:
                    # Some firmware rejects redundant mode/LED writes. Actual
                    # input is the acceptance criterion, not a write return value.
                    _logger.debug("Optional initialization write rejected", exc_info=True)
            on_status("Waiting for controller input...")
            await asyncio.wait_for(input_received.wait(), _INPUT_READY_TIMEOUT)
            if disconnected.is_set() or not client.is_connected:
                return None
            self._write_chars[address] = handshake_char
            self._cmd_chars[address] = cmd_char
            ready = accepted = True
            on_status("Connected via BLE")
            return address
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_status(f"Controller initialization failed: {str(exc) or type(exc).__name__}")
            return None
        finally:
            try:
                if not accepted and client is not None:
                    await self._disconnect_client(address, client)
            finally:
                if self._pending.get(address) is task:
                    self._pending.pop(address, None)

    async def send_rumble(self, identifier: str, packet: bytes) -> bool:
        """Send vibration command via the SW2 command channel.

        The Bumble backend (Linux) writes the 0x50-prefix rumble packet
        directly to ATT handle 0x0016 after a full SW2 init.  The Bleak
        backend skips the proprietary pairing, so that handle rejects
        rumble.  Instead, send the standard SW2 vibration command (0x0A,
        same format as USB) to the command channel — this works without
        the full init.  The char object is used directly to avoid
        UUID/handle ambiguity.
        """
        identifier = _normalize_address(identifier)
        client = self._clients.get(identifier)
        cmd_char = self._cmd_chars.get(identifier)
        if not client or not client.is_connected or not cmd_char:
            return False
        # Extract on/off state from the rumble packet (byte 2)
        state = packet[2] if len(packet) > 2 else 0
        # SW2 vibration command: cmd 0x0A, interface 0x01 (BLE)
        vibration_cmd = bytearray([
            0x0A, 0x91, 0x01, 0x02, 0x00, 0x04,
            0x00, 0x00, 0x01 if state else 0x00,
            0x00, 0x00, 0x00,
        ])
        try:
            await client.write_gatt_char(cmd_char, vibration_cmd, response=False)
            return True
        except Exception as e:
            _log(f"  Rumble write failed: {type(e).__name__}: {e}")
            return False

    async def set_led(self, identifier: str, slot_index: int) -> bool:
        """Update the player LED on a connected controller."""
        identifier = _normalize_address(identifier)
        client = self._clients.get(identifier)
        cmd_char = self._cmd_chars.get(identifier)
        if not client or not client.is_connected or not cmd_char:
            return False
        try:
            led_idx = min(slot_index, len(LED_MAP) - 1)
            await client.write_gatt_char(
                cmd_char, bytearray(build_led_cmd(LED_MAP[led_idx])),
                response=False)
            return True
        except Exception as e:
            _log(f"  LED update failed: {type(e).__name__}: {e}")
            return False

    async def disconnect(self, identifier: str):
        """Cancel an in-flight attempt before removing its owned client."""
        identifier = _normalize_address(identifier)
        task = self._pending.get(identifier)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client = self._clients.get(identifier)
        if client is not None:
            await self._disconnect_client(identifier, client)

    async def close(self):
        """Stop discovery and await every pending client cleanup."""
        self._closing = True
        try:
            await self.stop_scan()
            for scanner in list(self._scanners):
                try:
                    await scanner.stop()
                finally:
                    self._scanners.discard(scanner)
        finally:
            pending = [t for t in self._pending.values() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for address, client in list(self._clients.items()):
                await self._disconnect_client(address, client)
            if self._cleanup_tasks:
                await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
