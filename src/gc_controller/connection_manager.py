"""
Connection Manager

Handles USB initialization and HID device connection for the GameCube controller.
Supports multi-device enumeration and path-targeted open for multi-controller setups.
"""

from contextlib import contextmanager
import logging
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
    def build_hid_to_usb_bus_map() -> dict:
        """Map HID DevSrvsID → USB bus number using IOKit registry (macOS only).

        Returns dict of {devsrvs_id: usb_bus_number}.
        """
        if not IS_MACOS:
            return {}

        try:
            import plistlib
            result = subprocess.run(
                ['ioreg', '-r', '-c', 'IOUSBHostDevice', '-a'],
                capture_output=True, timeout=5)
            devices = plistlib.loads(result.stdout)
        except Exception:
            return {}

        mapping = {}

        def _find_hid_entry_id(children):
            for child in (children if isinstance(children, list) else [children]):
                if not isinstance(child, dict):
                    continue
                if child.get('IORegistryEntryName') == 'AppleUserUSBHostHIDDevice':
                    return child.get('IORegistryEntryID')
                sub = child.get('IORegistryEntryChildren', [])
                if sub:
                    r = _find_hid_entry_id(sub)
                    if r:
                        return r
            return None

        for dev in devices:
            if dev.get('idVendor') != VENDOR_ID or dev.get('idProduct') != PRODUCT_ID:
                continue
            loc_id = dev.get('locationID', 0)
            bus = (loc_id >> 24) & 0xFF
            hid_id = _find_hid_entry_id(dev.get('IORegistryEntryChildren', []))
            if hid_id is not None:
                mapping[hid_id] = bus

        logger.debug("HID→USB bus map: %s", mapping)
        return mapping

    def init_hid_device(self, device_path: Optional[bytes] = None) -> bool:
        """Initialize HID connection.

        If device_path is provided, open that specific device by path.
        Otherwise, open the first matching VID/PID device.
        """
        try:
            self._on_status("Connecting via HID...")

            self.device = hid.device()
            if device_path:
                self.device.open_path(device_path)
            else:
                self.device.open(VENDOR_ID, PRODUCT_ID)

            if self.device:
                self.device_path = device_path
                if sys.platform == 'win32':
                    self._try_hid_init()
                self._on_status("Connected via HID")
                self._on_progress(100)
                return True
            else:
                self._on_status("Failed to connect via HID")
                return False

        except Exception as e:
            self._on_status(f"HID connection failed: {e}")
            return False

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
        """Send a rumble ON/OFF command.

        Tries pyusb first (endpoint 0x02 on interface 1), then falls back
        to HIDAPI write for Windows where pyusb/libusb is unavailable.
        """
        cmd = bytes([0x0a, 0x91, 0x00, 0x02, 0x00, 0x04,
                     0x00, 0x00, 0x01 if state else 0x00,
                     0x00, 0x00, 0x00])

        # Try pyusb (works on Linux/macOS)
        try:
            dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
            if dev is not None:
                with _usb_command_interface(dev):
                    dev.write(0x02, cmd, 1000)
                return True
        except Exception:
            logger.debug("USB rumble failed", exc_info=True)

        # Windows USB: HID interface 0 is input-only (no output endpoint),
        # so rumble is unavailable without libusb/WinUSB for interface 1.
        # Rumble works on Windows via BLE (Bleak backend).
        return False

    def disconnect(self):
        """Close and release the HID device."""
        if self.device:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None
            self.device_path = None
