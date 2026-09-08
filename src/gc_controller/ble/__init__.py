"""
BLE Subpackage

Provides Bluetooth Low Energy connectivity for NSO GameCube controllers
using Google Bumble on Linux, or Bleak on macOS/Windows.
"""

import os
import sys


def is_ble_available() -> bool:
    """Check if BLE support is available (Linux + bumble, or macOS/Windows + bleak)."""
    if sys.platform == 'linux':
        try:
            import bumble  # noqa: F401
            return True
        except ImportError:
            return False
    elif sys.platform in ('darwin', 'win32'):
        try:
            import bleak  # noqa: F401
            return True
        except ImportError:
            return False
    return False


def get_ble_unavailable_reason() -> str:
    """Return a human-readable reason why BLE is not available."""
    if sys.platform == 'linux':
        try:
            import bumble  # noqa: F401
        except ImportError:
            return "The 'bumble' package is not installed. Install with: pip install bumble"
        return ""
    elif sys.platform in ('darwin', 'win32'):
        try:
            import bleak  # noqa: F401
        except ImportError:
            return "The 'bleak' package is not installed. Install with: pip install bleak"
        return ""
    return "BLE support is only available on Linux, macOS, and Windows."


def find_hci_adapter() -> int | None:
    """Find the first available HCI adapter index by checking /sys/class/bluetooth/."""
    bt_dir = '/sys/class/bluetooth'
    if not os.path.isdir(bt_dir):
        return None
    for entry in sorted(os.listdir(bt_dir)):
        if entry.startswith('hci'):
            try:
                return int(entry[3:])
            except ValueError:
                continue
    return None
