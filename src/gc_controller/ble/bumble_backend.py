"""
Bumble BLE Backend

Linux-only BLE backend using Google Bumble with raw HCI sockets.
Bypasses BlueZ entirely for full control over SMP key distribution.
"""

import asyncio
import inspect
import logging
import queue
from typing import Callable, Optional

from bumble.device import Device, Peer, ConnectionParametersPreferences
from bumble.hci import Address, HCI_LE_1M_PHY, HCI_LE_2M_PHY, OwnAddressType
from bumble.pairing import PairingConfig, PairingDelegate
from bumble.transport import open_transport
from bumble import smp  # noqa: F401

from ..ble_identifiers import NINTENDO_COMPANY_ID

from .sw2_protocol import sw2_init, translate_ble_to_usb, public_host_address

_logger = logging.getLogger(__name__)
_INPUT_READY_TIMEOUT = 5.0

# Known Nintendo BLE MAC OUI prefixes (first 3 octets)
_NINTENDO_OUIS = (
    '3C:A9:AB', '98:B6:E9', '7C:BB:8A', '58:2F:40',
    'D8:6B:F7', '04:03:D6', 'A4:C0:E1', '40:F4:07',
    'E0:EF:BF', '94:8E:6D',  # Jared Brick: observed NSO GC prefixes
)


class BumbleBackend:
    """Manages HCI transport and Bumble Device for BLE connections."""

    def __init__(self):
        self._transport = None
        self._device: Optional[Device] = None
        self._connections: dict[str, object] = {}  # mac -> connection
        self._peers: dict[str, Peer] = {}  # mac -> Peer
        self._hci_index: Optional[int] = None
        self._pending = {}
        self._background_tasks = set()
        self._security_tasks = {}
        self.malformed_reports = 0

    @property
    def is_open(self) -> bool:
        return self._device is not None

    async def open(self, hci_index: int):
        """Open the HCI transport and power on the Bumble device."""
        if self._device is not None:
            if hci_index == self._hci_index:
                return
            await self.close()
        try:
            self._hci_index = hci_index
            transport_name = f"hci-socket:{hci_index}"

            self._transport = await open_transport(transport_name)
            hci_source, hci_sink = self._transport

            self._device = Device.with_hci(
                "Bumble-GC",
                Address("F0:F1:F2:F3:F4:F5"),
                hci_source,
                hci_sink,
            )

            # Configure SMP for Legacy "Just Works" with exact BlueRetro key distribution
            self._device.pairing_config_factory = lambda connection: PairingConfig(
                sc=False,
                mitm=False,
                bonding=True,
                delegate=PairingDelegate(
                    io_capability=PairingDelegate.IoCapability.NO_OUTPUT_NO_INPUT,
                    local_initiator_key_distribution=(
                        PairingDelegate.KeyDistribution.DISTRIBUTE_IDENTITY_KEY
                    ),
                    local_responder_key_distribution=(
                        PairingDelegate.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
                    ),
                ),
            )

            await self._device.power_on()
        except BaseException:
            await self.close()
            raise

    async def scan_and_connect(
        self,
        slot_index: int,
        data_queue: queue.Queue,
        on_status: Callable[[str], None],
        on_disconnect: Callable[[], None],
        target_address: Optional[str] = None,
        exclude_addresses: Optional[list[str]] = None,
        scan_timeout: float = 15.0,
        connect_timeout: float = 15.0,
    ) -> Optional[str]:
        """Scan for an NSO GC controller, connect, pair, and init SW2 protocol.

        Args:
            slot_index: Controller slot (0-3)
            data_queue: Queue for input data (64-byte packets with 0x00 prefix)
            on_status: Status message callback
            on_disconnect: Callback for unexpected disconnect
            target_address: If set, connect directly to this MAC (skip scan)
            exclude_addresses: MACs to skip during scanning (other slots' controllers)
            scan_timeout: Seconds to scan before giving up
            connect_timeout: Seconds to wait for connection

        Returns:
            MAC address string on success, None on failure.
        """
        if not self._device:
            on_status("BLE not initialized")
            return None

        # Determine target MAC
        mac = target_address
        if not mac:
            on_status("Scanning for controller...")
            mac = await self._scan(scan_timeout, exclude_addresses)
            if not mac:
                on_status("No controller found")
                return None

        mac = mac.upper().removesuffix('/P').removesuffix('/R')
        if mac in self._connections or mac in self._pending:
            on_status("Controller already has an active connection")
            return None
        try:
            public_host_address(self._device)
        except (ValueError, TypeError) as exc:
            on_status(str(exc))
            return None

        task = asyncio.current_task()
        self._pending[mac] = task
        connection = None
        ready = False
        disconnected, input_received = asyncio.Event(), asyncio.Event()
        try:
            on_status("Connecting...")
            connection = await self._device.connect(
                Address(mac, Address.PUBLIC_DEVICE_ADDRESS),
                # Port of jaredrbrick/09ca5b1: the proprietary pairing handshake
                # registers public_address, so CONNECT_IND must use PUBLIC too.
                own_address_type=OwnAddressType.PUBLIC,
                connection_parameters_preferences={
                    phy: ConnectionParametersPreferences(
                        connection_interval_min=7.5, connection_interval_max=15.0,
                        max_latency=0, supervision_timeout=5000)
                    for phy in (HCI_LE_1M_PHY, HCI_LE_2M_PHY)
                },
                timeout=connect_timeout,
            )
            self._connections[mac] = connection
            security_tasks = self._security_tasks[mac] = set()
            pairing_lock = asyncio.Lock()

            def lost(reason=None):
                nonlocal ready
                disconnected.set()
                input_received.set()
                if self._connections.get(mac) is not connection:
                    return
                self._connections.pop(mac, None)
                self._peers.pop(mac, None)
                for security in self._security_tasks.pop(mac, set()):
                    security.cancel()
                if ready:
                    ready = False
                    on_disconnect()

            connection.on("disconnection", lost)

            async def pair():
                async with pairing_lock:
                    if self._connections.get(mac) is connection:
                        try:
                            await connection.pair()
                        except Exception:
                            # Existing firmware may still accept proprietary
                            # pairing after an SMP rejection. Readiness is required.
                            _logger.debug("SMP pairing rejected", exc_info=True)

            def security_request(auth_req):
                if self._connections.get(mac) is connection:
                    security = self._background(pair())
                    security_tasks.add(security)
                    security.add_done_callback(security_tasks.discard)

            connection.on("security_request", security_request)
            on_status("SMP pairing...")
            await pair()
            if disconnected.is_set():
                return None
            on_status("MTU exchange...")
            peer = Peer(connection)
            self._peers[mac] = peer
            try:
                await peer.request_mtu(512)
            except Exception:
                _logger.debug("MTU request rejected", exc_info=True)
            self._log_connection_params(connection, peer, on_status)
            on_status("Discovering services...")
            await peer.discover_services()
            for service in peer.services:
                await service.discover_characteristics()
                for char in service.characteristics:
                    await char.discover_descriptors()
            if disconnected.is_set():
                return None

            def input_report(value):
                if self._connections.get(mac) is not connection or disconnected.is_set():
                    return
                if len(value) != 63:
                    self.malformed_reports += 1
                    return
                try:
                    data_queue.put_nowait(translate_ble_to_usb(value))
                except Exception:
                    lost()
                    self._background(self._disconnect_connection(mac, connection))
                    return
                input_received.set()

            on_status("Initializing controller...")
            success = await sw2_init(
                peer=peer, connection=connection, device=self._device,
                slot_index=slot_index, on_input=input_report,
                on_status=on_status, disconnected=disconnected)
            if not success or disconnected.is_set():
                on_status("Controller init failed")
                return None
            await asyncio.wait_for(input_received.wait(), timeout=_INPUT_READY_TIMEOUT)
            if disconnected.is_set():
                return None
            ready = True
            return mac
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            on_status(f"Controller initialization failed: {str(exc) or type(exc).__name__}")
            return None
        finally:
            try:
                if not ready and connection is not None:
                    await self._disconnect_connection(mac, connection)
            finally:
                if self._pending.get(mac) is task:
                    self._pending.pop(mac, None)

    def _background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        def done(task):
            self._background_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                _logger.error("BLE cleanup/security task failed: %s", task.exception())
        task.add_done_callback(done)
        return task

    async def _disconnect_connection(self, mac, connection):
        if self._connections.get(mac) is connection:
            self._connections.pop(mac, None)
            self._peers.pop(mac, None)
            tasks = self._security_tasks.pop(mac, set())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(connection.disconnect(), 5)
        except Exception:
            _logger.debug("Controller disconnect failed", exc_info=True)

    @staticmethod
    def _log_connection_params(connection, peer, on_status):
        """Log negotiated BLE connection parameters for latency diagnostics."""
        parts = []
        try:
            params = getattr(connection, 'parameters', None)
            if params:
                interval = getattr(params, 'connection_interval', None)
                if interval is not None:
                    parts.append(f"interval={interval:.1f}ms")
                latency = getattr(params, 'peripheral_latency',
                                  getattr(params, 'max_latency', None))
                if latency is not None:
                    parts.append(f"latency={latency}")
                sup_to = getattr(params, 'supervision_timeout', None)
                if sup_to is not None:
                    parts.append(f"sup_timeout={sup_to}")
        except Exception:
            pass
        try:
            phy = getattr(connection, 'phy', None)
            if phy:
                parts.append(f"PHY={phy}")
        except Exception:
            pass
        try:
            mtu = getattr(peer, 'mtu', None)
            if mtu:
                parts.append(f"MTU={mtu}")
        except Exception:
            pass
        if parts:
            msg = "BLE params: " + ", ".join(parts)
            on_status(msg)
            print(f"  [BLE] {msg}", file=__import__('sys').stderr)

    async def _scan(self, timeout: float,
                    exclude_addresses: Optional[list[str]] = None,
                    ) -> Optional[str]:
        """Scan for NSO GC controllers, return first found MAC.

        Matches by Nintendo OUI prefix in the MAC address, mirroring
        the PoC's approach of matching by known MAC.
        """
        exclude = {a.upper().removesuffix('/P').removesuffix('/R')
                   for a in (exclude_addresses or [])}
        found_event = asyncio.Event()
        found_mac = [None]

        def on_advertisement(advertisement):
            try:
                if found_event.is_set():
                    return
                addr_str = str(advertisement.address).upper().removesuffix('/P').removesuffix('/R')
                # Skip controllers that are already connected
                if addr_str in self._connections:
                    return
                # Skip controllers assigned to other slots
                if addr_str in exclude:
                    return
                # Company data supports controllers with an unfamiliar OUI.
                # This is discovery only, not proof of identity or readiness.
                data = getattr(advertisement, 'data', None)
                manufacturer = data.get(0xFF) if data is not None else None
                if isinstance(manufacturer, (bytes, bytearray)) and len(manufacturer) >= 2:
                    if int.from_bytes(manufacturer[:2], 'little') == NINTENDO_COMPANY_ID:
                        found_mac[0] = addr_str
                        found_event.set()
                        return
                for oui in _NINTENDO_OUIS:
                    if addr_str.startswith(oui):
                        found_mac[0] = addr_str
                        found_event.set()
                        return
            except Exception:
                pass

        self._device.on("advertisement", on_advertisement)
        try:
            await self._device.start_scanning(filter_duplicates=False)
            await asyncio.wait_for(found_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                await self._device.stop_scanning()
            finally:
                self._device.remove_listener("advertisement", on_advertisement)

        return found_mac[0]

    async def scan_only(self, scan_timeout: float = 10.0) -> list[dict]:
        """Run a full BLE scan and return all discovered devices.

        Returns a list of dicts with keys: address, name, rssi.
        Unlike _scan(), this captures ALL advertising devices, not just
        Nintendo OUI matches.
        """
        if not self._device:
            return []

        found: dict[str, dict] = {}

        def on_advertisement(advertisement):
            try:
                addr_str = str(advertisement.address).upper().removesuffix('/P').removesuffix('/R')
                # Skip devices already connected
                if addr_str in self._connections:
                    return
                rssi = getattr(advertisement, 'rssi', -999) or -999
                # AdvertisingData.get() returns str (or None), never bytes, so
                # calling .decode() on it raises AttributeError for *every*
                # advertisement. The bare `except Exception: pass` below then
                # swallows it, leaving `found` empty and making this method
                # always return [].
                raw = advertisement.data.get(0x09) if hasattr(advertisement, 'data') else None
                if isinstance(raw, bytes):
                    name = raw.decode('utf-8', errors='replace')
                elif isinstance(raw, str):
                    name = raw
                else:
                    name = ''
                if not name:
                    name = getattr(advertisement, 'name', '') or ''
                # Keep the strongest signal if seen multiple times
                if addr_str not in found or rssi > found[addr_str].get('rssi', -999):
                    found[addr_str] = {
                        'address': addr_str,
                        'name': name,
                        'rssi': rssi,
                    }
            except Exception:
                pass

        self._device.on("advertisement", on_advertisement)
        try:
            await self._device.start_scanning(filter_duplicates=False)
            await asyncio.sleep(scan_timeout)
        finally:
            try:
                await self._device.stop_scanning()
            finally:
                self._device.remove_listener("advertisement", on_advertisement)

        return list(found.values())

    async def send_rumble(self, mac: str, packet: bytes) -> bool:
        """Send rumble packet to controller via ATT write (no response)."""
        mac = mac.upper().removesuffix('/P').removesuffix('/R')
        peer = self._peers.get(mac)
        if not peer:
            return False
        try:
            from .sw2_protocol import H_OUT_CMD
            await peer.gatt_client.write_value(
                attribute=H_OUT_CMD, value=packet, with_response=False)
            return True
        except Exception:
            return False

    async def set_led(self, mac: str, slot_index: int) -> bool:
        """Update the player LED on a connected controller."""
        mac = mac.upper().removesuffix('/P').removesuffix('/R')
        peer = self._peers.get(mac)
        if not peer:
            return False
        try:
            from .sw2_protocol import H_OUT_CMD, LED_MAP, build_led_cmd
            led_idx = min(slot_index, len(LED_MAP) - 1)
            await peer.gatt_client.write_value(
                attribute=H_OUT_CMD,
                value=bytearray(build_led_cmd(LED_MAP[led_idx])),
                with_response=False)
            return True
        except Exception:
            return False

    async def disconnect(self, mac_address: str):
        mac_address = mac_address.upper().removesuffix('/P').removesuffix('/R')
        task = self._pending.get(mac_address)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        connection = self._connections.get(mac_address)
        if connection is not None:
            await self._disconnect_connection(mac_address, connection)

    async def close(self):
        """Close connections AND await the HCI transport's asynchronous close."""
        pending = [t for t in self._pending.values() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            for mac in list(self._connections):
                await self.disconnect(mac)
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
        finally:
            transport, self._transport = self._transport, None
            self._device = None
            if transport is not None:
                result = transport.close()
                if inspect.isawaitable(result):
                    await result
