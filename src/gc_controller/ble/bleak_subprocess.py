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
