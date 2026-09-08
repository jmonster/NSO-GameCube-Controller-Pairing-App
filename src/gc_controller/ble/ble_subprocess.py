#!/usr/bin/env python3
"""BLE child entrypoint. Protocol v2 is defined in ipc.py/child_runtime.py.

Input: 0xFE + wire slot (1 byte) + generation (8 bytes, big endian) + report (64).
Slot-scoped JSON events/commands carry the same positive generation as `g`.
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
        from gc_controller.ble import find_hci_adapter
        from gc_controller.ble.bluez import BlueZLease
        from gc_controller.ble.bumble_backend import BumbleBackend
        from gc_controller.ble.child_runtime import run_subprocess
    except ImportError as exc:
        print(json.dumps({'e': 'error', 'ctx': 'import', 'msg': str(exc)}), flush=True)
        return 1
    backend = BumbleBackend()
    lease = None

    def acquire_bluez():
        nonlocal lease
        if lease is None:
            index = find_hci_adapter()
            if index is None:
                raise RuntimeError('No Bluetooth HCI adapter found')
            lease = BlueZLease(index)
        return lease.acquire()

    async def open_backend(command):
        if lease is None or not lease.is_acquired:
            raise RuntimeError('Acquire Bluetooth ownership before opening HCI')
        index = command.get('hci_index', lease.hci_index)
        if type(index) is not int or index != lease.hci_index:
            raise RuntimeError('Cannot open an adapter outside the Bluetooth lease')
        await backend.open(hci_index=index)

    try:
        run_subprocess(backend, open_backend=open_backend, stop_bluez=acquire_bluez)
    finally:
        # run_subprocess/asyncio.run finishes backend and executor cleanup first,
        # including a cancelled to_thread(acquire). Restore even after failures.
        if lease is not None:
            try:
                lease.release()
            except Exception as exc:
                print(f'Bluetooth restoration failed: {exc}', file=sys.stderr, flush=True)
                return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
