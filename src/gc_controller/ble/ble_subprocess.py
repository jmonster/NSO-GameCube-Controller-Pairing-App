#!/usr/bin/env python3
"""BLE subprocess — runs with elevated privileges via pkexec.

Handles all Bluetooth Low Energy operations requiring raw HCI access.
Communicates with the main app via stdin/stdout.

Protocol:
  Input data uses a binary format for minimal latency:
    0xFF (1 byte magic) + slot_index (1 byte) + raw_data (64 bytes) = 66 bytes
  All other events use JSON lines (which never start with 0xFF).

  Parent -> Child commands (JSON lines):
    {"cmd": "stop_bluez"}
    {"cmd": "open", "hci_index": 0}
    {"cmd": "scan_connect", "slot_index": 0, "target_address": "XX:XX:XX:XX:XX:XX"}
    {"cmd": "scan_devices", "slot_index": 0}
    {"cmd": "connect_device", "slot_index": 0, "address": "XX:XX:XX:XX:XX:XX"}
    {"cmd": "disconnect", "slot_index": 0, "address": "XX:XX:XX:XX:XX:XX"}
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
    try:
        from gc_controller.ble import stop_bluez, find_hci_adapter
        from gc_controller.ble.bumble_backend import BumbleBackend
        from gc_controller.ble.child_runtime import run_subprocess
    except ImportError as exc:
        print(json.dumps({'e': 'error', 'ctx': 'import', 'msg': str(exc)}), flush=True)
        return 1
    backend = BumbleBackend()

    async def open_backend(command):
        index = command.get('hci_index')
        if index is None:
            index = find_hci_adapter()
        if type(index) is not int or index < 0:
            raise RuntimeError('No valid Bluetooth HCI adapter found')
        await backend.open(hci_index=index)

    run_subprocess(backend, open_backend=open_backend, stop_bluez=stop_bluez)
    return 0


if __name__ == '__main__':
    sys.exit(main())
