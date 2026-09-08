"""
Connection Manager

Handles USB initialization and HID device connection for the GameCube controller.
Supports multi-device enumeration and path-targeted open for multi-controller setups.
"""

from contextlib import contextmanager
import logging
from pathlib import Path
import threading
import sys
from typing import Optional, Callable, List

import subprocess

import hid
import usb.control
import usb.core
import usb.util

from .controller_constants import VENDOR_ID, PRODUCT_ID, DEFAULT_REPORT_DATA, SET_LED_DATA

logger = logging.getLogger(__name__)
IS_MACOS = sys.platform == "darwin"


# Initializers and feedback may run on different slot/worker threads. Do not
# let one operation dispose a PyUSB handle another operation is still using.
_USB_COMMAND_LOCK = threading.RLock()


@contextmanager
def _usb_command_interface(dev, *, configure=False):
    """Own interface 1 and release all resources on every exit path."""
    with _USB_COMMAND_LOCK:
        claimed = False
        detached = False
        try:
            if IS_MACOS:
                try:
                    active = dev.is_kernel_driver_active(1)
                except NotImplementedError:
                    active = False
                if active:
                    dev.detach_kernel_driver(1)
                    detached = True
            # Reapplying an active configuration can reset other interfaces.
            # A failed query/claim is not evidence that it was already done.
            if configure and usb.control.get_configuration(dev) == 0:
                dev.set_configuration()
            usb.util.claim_interface(dev, 1)
            claimed = True
            yield dev
        finally:
            if claimed:
                try:
                    usb.util.release_interface(dev, 1)
                except Exception:
                    logger.debug("Failed to release USB command interface", exc_info=True)
            if detached:
                try:
                    dev.attach_kernel_driver(1)
                except Exception:
                    logger.debug("Failed to restore USB kernel driver", exc_info=True)
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                logger.debug("Failed to dispose USB resources", exc_info=True)

class ConnectionManager:
    """Manages USB initialization and HID connection."""

    def __init__(self, on_status: Callable[[str], None], on_progress: Callable[[int], None]):
        self._on_status = on_status
        self._on_progress = on_progress
        self.device: Optional[hid.device] = None
        self.device_path: Optional[bytes] = None

        self._usb_device = None
        self._session_lock = threading.RLock()
    @staticmethod
    def enumerate_devices() -> List[dict]:
        """Return a list of HID device info dicts for all connected GC controllers."""
        devices = hid.enumerate(VENDOR_ID, PRODUCT_ID)
        logger.debug("HID enumerate: %d device(s) for VID=%04x PID=%04x",
                      len(devices), VENDOR_ID, PRODUCT_ID)
        for d in devices:
            logger.debug("  path=%s  product=%s  serial=%s  manufacturer=%s  "
                         "release=0x%04x  interface=%d  usage_page=0x%04x  usage=0x%04x",
                         d.get('path'), d.get('product_string'),
                         d.get('serial_number', ''), d.get('manufacturer_string', ''),
                         d.get('release_number', 0), d.get('interface_number', -1),
                         d.get('usage_page', 0), d.get('usage', 0))
        return devices

    @staticmethod
    def enumerate_usb_devices() -> list:
        """Return a list of all USB device objects matching the GC controller VID/PID."""
        try:
            devices = usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            result = list(devices) if devices else []
            logger.debug("USB enumerate: %d device(s)", len(result))
            return result
        except Exception as e:
            logger.debug("USB enumerate failed (expected on Windows without libusb): %s", e)
            return []

    def initialize_via_usb(self, usb_device=None) -> bool:
        """Initialize controller via USB.

        If usb_device is provided, use it directly instead of scanning.
        """
        try:
            self._on_status("Looking for device...")
            self._on_progress(10)

            dev = usb_device if usb_device is not None else usb.core.find(
                idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if dev is None:
                self._on_status("Device not found")
                return False

            self._on_status("Device found")
            self._on_progress(30)

            with _usb_command_interface(dev, configure=True):
                self._on_progress(50)
                self._on_status("Sending initialization data...")
                dev.write(0x02, DEFAULT_REPORT_DATA, 2000)
                self._on_progress(70)
                self._on_status("Sending LED data...")
                dev.write(0x02, SET_LED_DATA, 2000)
                self._on_progress(90)










            self._on_status("USB initialization complete")
            return True

        except Exception as e:
            self._on_status(f"USB initialization failed: {e}")
            return False

    @staticmethod
    def set_player_led_usb(usb_device, player_num: int) -> bool:
        """Set player LED via USB bulk transfer on a specific USB device.

        Args:
            usb_device: pyusb Device object.
            player_num: 1–4 (cumulative LEDs: P1=1 LED, P2=2 LEDs, etc.)
        """
        if not 1 <= player_num <= 4:
            return False

        led_mask = (1 << player_num) - 1
        led_data = bytearray(SET_LED_DATA)
        led_data[8] = led_mask

        try:
            with _usb_command_interface(usb_device):
                usb_device.write(0x02, bytes(led_data), 2000)
            logger.debug("Set player LED via USB: player=%d mask=0x%02x", player_num, led_mask)
            return True
        except Exception as e:
            logger.debug("Failed to set player LED via USB: %s", e)
            return False

    @staticmethod
    def build_hid_to_usb_address_map() -> dict:
        """Map macOS HID registry IDs to (libusb bus, device address).

        A bus alone is not an identity: every controller on a hub may share
        it. Missing address information deliberately yields no match.
        """
        if not IS_MACOS:
            return {}

        try:
            import plistlib
            result = subprocess.run(
                ['ioreg', '-r', '-c', 'IOUSBHostDevice', '-a'],
                capture_output=True, timeout=5, check=True)
            devices = plistlib.loads(result.stdout)
        except Exception:
            logger.debug("Could not read USB device ancestry", exc_info=True)
            return {}

        def hid_ids(children):

            for child in (children if isinstance(children, list) else [children]):
                if not isinstance(child, dict):
                    continue
                if child.get('IORegistryEntryName') == 'AppleUserUSBHostHIDDevice':
                    entry_id = child.get('IORegistryEntryID')
                    if isinstance(entry_id, int):
                        yield entry_id
                yield from hid_ids(child.get('IORegistryEntryChildren', []))

        mapping = {}
        for dev in devices if isinstance(devices, list) else []:
            if not isinstance(dev, dict):
                continue

            if dev.get('idVendor') != VENDOR_ID or dev.get('idProduct') != PRODUCT_ID:
                continue
            location = dev.get('locationID')
            address = dev.get('USB Address', dev.get('USBAddress'))
            if not isinstance(location, int) or not isinstance(address, int):
                continue
            if not 1 <= address <= 127:
                continue
            # libusb's Darwin backend derives its bus from locationID.
            identity = ((location >> 24) & 0xFF, address)
            for entry_id in hid_ids(dev.get('IORegistryEntryChildren', [])):
                mapping[entry_id] = identity

        return mapping


    @staticmethod
    def build_hid_to_usb_bus_map() -> dict:
        """Legacy diagnostic helper; do not use a bus alone to route output."""
        return {key: identity[0] for key, identity in
                ConnectionManager.build_hid_to_usb_address_map().items()}

    @staticmethod
    def _linux_usb_address(device_path) -> Optional[tuple]:
        """Resolve a hidraw node through its actual USB sysfs ancestry."""
        try:
            path = device_path.decode() if isinstance(device_path, bytes) else str(device_path)
            if not path.startswith('/dev/hidraw') or '/' in path[len('/dev/'):]:
                return None
            node = (Path('/sys/class/hidraw') / Path(path).name / 'device').resolve(strict=True)
            for parent in (node, *node.parents):
                vendor = parent / 'idVendor'
                product = parent / 'idProduct'
                if not vendor.is_file() or not product.is_file():
                    continue
                if (int(vendor.read_text().strip(), 16) != VENDOR_ID or
                        int(product.read_text().strip(), 16) != PRODUCT_ID):
                    return None
                return (int((parent / 'busnum').read_text().strip()),
                        int((parent / 'devnum').read_text().strip()))
        except (OSError, ValueError, UnicodeError):
            logger.debug("Could not resolve hidraw USB ancestry", exc_info=True)
        return None

    def _resolve_usb_device(self, device_path):
        """Resolve only an unambiguous USB peer of this HID path.

        Never fall back to the first VID/PID match, even if only one USB
        device is visible: the opened HID device could be a BLE controller.
        """
        if not device_path:
            return None
        devices = self.enumerate_usb_devices()
        if not devices:
            return None
        identity = None
        if IS_MACOS:
            try:
                path = device_path.decode() if isinstance(device_path, bytes) else device_path
                prefix, registry_id = path.split(':', 1)
                if prefix == 'DevSrvsID':
                    identity = self.build_hid_to_usb_address_map().get(int(registry_id))
            except (AttributeError, ValueError, UnicodeError):
                pass
        elif sys.platform.startswith('linux'):
            identity = self._linux_usb_address(device_path)
        if identity is not None:
            matches = [dev for dev in devices
                       if (dev.bus, dev.address) == identity]
            return matches[0] if len(matches) == 1 else None

        # HIDAPI/libusb paths differ across builds. A verified unique serial
        # is a portable fallback, never an empty or unreadable descriptor.
        infos = [info for info in self.enumerate_devices()
                 if info.get('path') == device_path]
        serials = {info.get('serial_number') for info in infos
                   if info.get('serial_number')}
        if len(serials) != 1:
            return None
        serial = serials.pop()
        matches = []
        for dev in devices:
            try:
                if dev.serial_number == serial:
                    matches.append(dev)
            except Exception:
                # An unreadable peer could have the same serial. Do not
                # claim uniqueness in the face of incomplete evidence.
                return None
            finally:
                try:
                    usb.util.dispose_resources(dev)
                except Exception:
                    logger.debug("Could not release serial-query handle", exc_info=True)
        return matches[0] if len(matches) == 1 else None

    def init_hid_device(self, device_path: Optional[bytes] = None) -> bool:
        """Open a specific HID path and bind feedback to that same device."""
        with self._session_lock:
            if self.device is not None:
                self._on_status("HID session is already open; disconnect it before reconnecting")
                return False
            candidate = None
            self.device_path = None
            self._usb_device = None
            try:
                self._on_status("Connecting via HID...")
                if not device_path:
                    devices = self.enumerate_devices()
                    if not devices or not devices[0].get('path'):
                        self._on_status("Device not found")
                        return False
                    device_path = devices[0]['path']
                candidate = hid.device()
                candidate.open_path(device_path)
                self.device = candidate


                self.device_path = device_path
                try:
                    # Serialize descriptor access with commands as it also
                    # opens and disposes PyUSB handles.
                    with _USB_COMMAND_LOCK:
                        self._usb_device = self._resolve_usb_device(device_path)
                except Exception:
                    logger.debug("USB feedback identity unavailable", exc_info=True)
                if self._usb_device is None:
                    logger.info("USB feedback disabled: no verified peer for HID path %r", device_path)
                if sys.platform == 'win32':
                    self._try_hid_init()
                self._on_status("Connected via HID")
                self._on_progress(100)
                return True
            except Exception as e:
                if candidate is not None:
                    try:
                        candidate.close()
                    except Exception:
                        logger.debug("Could not close failed HID open", exc_info=True)
                self.device = None
                self.device_path = None
                self._usb_device = None
                self._on_status(f"HID connection failed: {e}")
                return False

    def set_player_led(self, player_num: int) -> bool:
        """Set LEDs on this session's verified USB peer, or fail closed."""
        with self._session_lock:
            if self.device is None or self._usb_device is None:
                return False
            return self.set_player_led_usb(self._usb_device, player_num)


    def _try_hid_init(self):
        """Try switching the controller from standard HID to proprietary GC mode.

        On Windows without libusb, the controller stays in standard HID mode
        (report ID 0x0A) which Windows also processes as a native gamepad,
        causing double inputs.  Sending the init + LED commands via HID output
        reports can switch the controller to the proprietary GC format that
        only our app understands.
        """
        if not self.device:
            return
        try:
            self.device.write(list(DEFAULT_REPORT_DATA))
            self.device.write(list(SET_LED_DATA))
            logger.info("HID init: sent init commands via HID write")
        except Exception as e:
            logger.debug("HID init via write failed (expected): %s", e)

    def connect(self, usb_device=None, device_path: Optional[bytes] = None) -> bool:
        """Full connection sequence: USB init then HID.

        Optionally target a specific USB device and/or HID device path.
        """
        if not self.initialize_via_usb(usb_device=usb_device):
            return False
        return self.init_hid_device(device_path=device_path)

    def send_rumble(self, state: bool) -> bool:
        """Send rumble only to this session's verified USB peer.

        USB interface 0 is input-only; there is no HID-write fallback when
        libusb cannot access command interface 1. BLE has its own backend.

        """
        cmd = bytes([0x0a, 0x91, 0x00, 0x02, 0x00, 0x04,
                     0x00, 0x00, 0x01 if state else 0x00,
                     0x00, 0x00, 0x00])

        with self._session_lock:
            if self.device is None or self._usb_device is None:
                return False
            try:
                with _usb_command_interface(self._usb_device):
                    self._usb_device.write(0x02, cmd, 1000)
                return True
            except Exception:
                logger.debug("USB rumble failed for this controller", exc_info=True)
                return False


    def disconnect(self):
        """Close the HID session and invalidate its USB feedback binding."""
        with self._session_lock:
            device = self.device
            self.device = None
            self.device_path = None
            self._usb_device = None
            if device is not None:
                try:
                    device.close()
                except Exception:
                    logger.debug("Could not close HID device", exc_info=True)
