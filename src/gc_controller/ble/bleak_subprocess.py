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

import asyncio
import base64
import json
import logging
import os
import queue
import sys
import threading


_output = None


def send(event: dict):
    """Queue a JSON event on the same ordered stream as input reports."""
    _output.submit((json.dumps(event, separators=(',', ':')) + '\n').encode('utf-8'))


class PipeQueue:
    """queue.Queue adapter that forwards input data to the parent via binary stdout.

    Uses a compact binary format (0xFF + slot + 64 bytes) instead of
    JSON+base64 to minimize serialization overhead on the hot path.
    Queues immutable snapshots without blocking Bluetooth callbacks.
    """

    def __init__(self, slot_index: int):
        self._slot = slot_index
        self._packet = bytearray(66)
        self._packet[0] = 0xFF
        self._packet[1] = slot_index & 0xFF

    def put_nowait(self, data):
        try:
            pkt = self._packet
            src = bytes(data[:64])
            pkt[2:2 + len(src)] = src
            if len(src) < 64:
                pkt[2 + len(src):66] = b'\x00' * (64 - len(src))
            _output.submit(pkt)
        except Exception as exc:
            _output.fail(exc)

    def put(self, data):
        self.put_nowait(data)

    def empty(self):
        return True

    def get_nowait(self):
        raise queue.Empty()


def _normalize_address(addr):
    """Strip /P or /R suffix from a BLE address."""
    import re
    if not addr:
        return addr
    return re.sub(r'/[PR]$', '', addr)


async def do_scan_connect(backend, slot_index, target_address,
                          exclude_addresses=None, slot_ids=None,
                          connect_timeout=None):
    """Run scan_and_connect as a background asyncio task."""
    target_address = _normalize_address(target_address)
    pq = PipeQueue(slot_index)

    def on_status(msg, _si=slot_index):
        send({"e": "status", "s": _si, "msg": msg})

    def on_disconnect(_si=slot_index):
        if slot_ids is not None:
            slot_ids.pop(_si, None)
        send({"e": "disconnected", "s": _si})

    kwargs = {}
    if connect_timeout is not None:
        kwargs['connect_timeout'] = connect_timeout

    try:
        identifier = await backend.scan_and_connect(
            slot_index=slot_index,
            data_queue=pq,
            on_status=on_status,
            on_disconnect=on_disconnect,
            target_address=target_address,
            exclude_addresses=exclude_addresses,
            **kwargs,
        )
        if identifier:
            if slot_ids is not None:
                slot_ids[slot_index] = identifier
            send({"e": "connected", "s": slot_index, "mac": identifier})
        else:
            send({"e": "connect_error", "s": slot_index,
                  "msg": "Connection failed"})
    except asyncio.CancelledError:
        pass
    except Exception as ex:
        send({"e": "connect_error", "s": slot_index, "msg": str(ex)})


async def do_scan_devices(backend, slot_index):
    """Run scan_only and send back the list of discovered devices."""
    try:
        send({"e": "status", "s": slot_index, "msg": "Scanning for devices..."})
        devices = await backend.scan_only()
        send({"e": "devices_found", "s": slot_index, "devices": devices})
    except asyncio.CancelledError:
        pass
    except Exception as ex:
        send({"e": "connect_error", "s": slot_index, "msg": str(ex)})


async def do_start_scan(backend, slot_index):
    """Start a continuous scan, sending device_detected events as devices appear."""
    def on_device(dev):
        send({"e": "device_detected", "s": slot_index, "device": dev})

    try:
        await backend.start_scan(on_device_found=on_device)
    except Exception as ex:
        send({"e": "connect_error", "s": slot_index, "msg": str(ex)})


async def do_stop_scan(backend):
    """Stop the continuous scan."""
    try:
        await backend.stop_scan()
    except Exception:
        pass


async def do_connect_device(backend, slot_index, address, slot_ids=None):
    """Connect to a specific device address from the last scan."""
    address = _normalize_address(address) or address
    pq = PipeQueue(slot_index)

    def on_status(msg, _si=slot_index):
        send({"e": "status", "s": _si, "msg": msg})

    def on_disconnect(_si=slot_index):
        if slot_ids is not None:
            slot_ids.pop(_si, None)
        send({"e": "disconnected", "s": _si})

    try:
        identifier = await backend.connect_device(
            address=address,
            slot_index=slot_index,
            data_queue=pq,
            on_status=on_status,
            on_disconnect=on_disconnect,
        )
        if identifier:
            if slot_ids is not None:
                slot_ids[slot_index] = identifier
            send({"e": "connected", "s": slot_index, "mac": identifier})
        else:
            send({"e": "connect_error", "s": slot_index,
                  "msg": "Connection failed"})
    except asyncio.CancelledError:
        pass
    except Exception as ex:
        send({"e": "connect_error", "s": slot_index, "msg": str(ex)})


def main():
    # Restore Python path from first argument so imports work
    if len(sys.argv) > 1:
        for p in sys.argv[1].split(os.pathsep):
            if p and p not in sys.path:
                sys.path.insert(0, p)

    from gc_controller.ble.pipe_output import PipeWriter
    global _output
    _output = PipeWriter(sys.stdout.buffer.fileno())

    if sys.platform == "win32":
        try:
            from bleak.backends.winrt.util import uninitialize_sta
            uninitialize_sta()
        except Exception:
            pass

    try:
        from gc_controller.ble.bleak_backend import BleakBackend
    except ImportError as e:
        send({"e": "error", "ctx": "import", "msg": str(e)})
        _output.close()
        sys.exit(1)

    backend = BleakBackend()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Read commands from stdin in a background thread
    cmd_queue = queue.Queue()

    def stdin_reader():
        try:
            for line in sys.stdin:
                line = line.strip()
                if line:
                    cmd_queue.put(json.loads(line))
        except Exception:
            pass
        cmd_queue.put(None)

    threading.Thread(target=stdin_reader, daemon=True).start()

    send({"e": "ready"})

    async def process():
        connect_tasks = {}  # slot_index -> asyncio.Task
        slot_ids = {}       # slot_index -> identifier (for rumble routing)

        while True:
            cmd = await loop.run_in_executor(None, cmd_queue.get)
            if cmd is None:
                break

            action = cmd.get("cmd")

            if action == "stop_bluez":
                # No-op on macOS — no BlueZ to stop
                send({"e": "bluez_stopped"})

            elif action == "open":
                # Lightweight no-op — CoreBluetooth is always available
                try:
                    await backend.open()
                    send({"e": "open_ok"})
                except Exception as ex:
                    send({"e": "error", "ctx": "open", "msg": str(ex)})

            elif action == "scan_connect":
                si = cmd["slot_index"]
                if si in connect_tasks and not connect_tasks[si].done():
                    connect_tasks[si].cancel()
                connect_tasks[si] = asyncio.create_task(
                    do_scan_connect(backend, si, cmd.get("target_address"),
                                    cmd.get("exclude_addresses"),
                                    slot_ids=slot_ids,
                                    connect_timeout=cmd.get("connect_timeout")))

            elif action == "scan_devices":
                si = cmd["slot_index"]
                if si in connect_tasks and not connect_tasks[si].done():
                    connect_tasks[si].cancel()
                connect_tasks[si] = asyncio.create_task(
                    do_scan_devices(backend, si))

            elif action == "scan_start":
                si = cmd["slot_index"]
                await do_stop_scan(backend)
                asyncio.create_task(do_start_scan(backend, si))

            elif action == "scan_stop":
                await do_stop_scan(backend)

            elif action == "connect_device":
                si = cmd["slot_index"]
                addr = cmd.get("address", "")
                if si in connect_tasks and not connect_tasks[si].done():
                    connect_tasks[si].cancel()
                connect_tasks[si] = asyncio.create_task(
                    do_connect_device(backend, si, addr, slot_ids=slot_ids))

            elif action == "rumble":
                si = cmd.get("slot_index")
                data = base64.b64decode(cmd["data"])
                identifier = slot_ids.get(si)
                if identifier:
                    asyncio.create_task(backend.send_rumble(identifier, data))

            elif action == "set_led":
                si = cmd.get("slot_index")
                new_slot = cmd.get("new_slot_index", si)
                identifier = slot_ids.get(si)
                if identifier:
                    asyncio.create_task(backend.set_led(identifier, new_slot))

            elif action == "cancel_all_scans":
                for task in connect_tasks.values():
                    if not task.done():
                        task.cancel()
                connect_tasks.clear()
                await do_stop_scan(backend)

            elif action == "disconnect":
                addr = cmd.get("address")
                si = cmd.get("slot_index")
                if si is not None and si in connect_tasks:
                    if not connect_tasks[si].done():
                        connect_tasks[si].cancel()
                if addr:
                    try:
                        await backend.disconnect(addr)
                    except Exception:
                        pass

            elif action in ("close", "shutdown"):
                for task in connect_tasks.values():
                    if not task.done():
                        task.cancel()
                break  # Shared exit cleanup below also handles EOF/output failure.

    task = loop.create_task(process())

    def output_failed(error):
        # Wake the executor's queue.get as well as the command coroutine. Never
        # try to report a broken stdout by enqueueing another stdout message.
        cmd_queue.put(None)
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:
            pass  # The loop has already completed shutdown.

    _output.on_failure = output_failed
    if _output.error is not None:
        output_failed(_output.error)
    try:
        loop.run_until_complete(task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        _output.on_failure = None
        cmd_queue.put(None)
        pending = asyncio.all_tasks(loop)
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=0.5))
        close_task = loop.create_task(backend.close())
        done, _ = loop.run_until_complete(asyncio.wait({close_task}, timeout=2))
        if close_task in done:
            try:
                close_task.result()
            except Exception:
                logging.getLogger(__name__).warning('BLE backend cleanup failed', exc_info=True)
        else:
            close_task.cancel()
            logging.getLogger(__name__).warning('BLE backend cleanup timed out')
        _output.close()
        loop.close()
    if _output.error is not None:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
