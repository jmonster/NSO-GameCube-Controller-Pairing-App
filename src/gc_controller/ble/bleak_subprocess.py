#!/usr/bin/env python3
"""BLE subprocess for macOS/Windows — uses Bleak.

No elevated privileges needed. Same IPC protocol as ble_subprocess.py.

Protocol:
  Input data uses a binary format for minimal latency:
    0xFF (1 byte magic) + slot_index (1 byte) + raw_data (64 bytes) = 66 bytes
  All other events use JSON lines (which never start with 0xFF).

  Parent -> Child commands (JSON lines):
    {"cmd": "stop_bluez"}
    {"cmd": "open"}
    {"cmd": "scan_connect", "slot_index": 0, "target_address": "..."}
    {"cmd": "scan_devices", "slot_index": 0}
    {"cmd": "scan_start", "slot_index": 0}
    {"cmd": "scan_stop"}
    {"cmd": "connect_device", "slot_index": 0, "address": "..."}
    {"cmd": "disconnect", "slot_index": 0, "address": "..."}
    {"cmd": "shutdown"}

  Child -> Parent events (JSON lines):
    {"e": "ready"}
    {"e": "bluez_stopped"}
    {"e": "open_ok"}
    {"e": "error", "ctx": "...", "msg": "..."}
    {"e": "status", "s": <slot>, "msg": "..."}
    {"e": "connected", "s": <slot>, "mac": "..."}
    {"e": "connect_error", "s": <slot>, "msg": "..."}
    {"e": "devices_found", "s": <slot>, "devices": [...]}
    {"e": "device_detected", "s": <slot>, "device": {...}}
    {"e": "disconnected", "s": <slot>}

  Child -> Parent data (binary):
    0xFF + slot_index(1) + raw_data(64) = 66 bytes total
"""

import json
import os
import sys


def main():
    # Direct-script and frozen entrypoints both restore the application path.
    if len(sys.argv) > 1:
        for path in sys.argv[1].split(os.pathsep):
            if path and path not in sys.path:
                sys.path.insert(0, path)
    if sys.platform == 'win32':
        try:
            from bleak.backends.winrt.util import uninitialize_sta
            uninitialize_sta()
        except ImportError:
            pass
    try:
        from gc_controller.ble.bleak_backend import BleakBackend
        from gc_controller.ble.child_runtime import run_subprocess
    except ImportError as exc:
        print(json.dumps({'e': 'error', 'ctx': 'import', 'msg': str(exc)}), flush=True)
        return 1
    run_subprocess(BleakBackend())
    return 0


if __name__ == '__main__':
    sys.exit(main())
