"""Shared Bleak/Bumble command lifecycle, session ownership and EOF cleanup."""
import asyncio
import base64
import json
import logging
import queue
import signal
import sys
import threading
from dataclasses import dataclass, field

from .ipc import MAX_JSON_BYTES, MAX_SLOTS, PROTOCOL_VERSION, valid_generation
from .output import OutputWriter

_logger = logging.getLogger(__name__)


def _address(value):
    return value.upper().removesuffix('/P').removesuffix('/R') if value else None


@dataclass(eq=False)
class _Session:
    slot: int
    target: str | None
    generation: int
    identifier: str | None = None
    ready: bool = False
    disconnected: bool = False
    pending_input: bytes | None = None
    task: object = None
    feedback: set = field(default_factory=set)


class _InputQueue:
    def __init__(self, runner, session):
        self.runner, self.session = runner, session

    def put_nowait(self, report):
        session = self.session
        if not self.runner.owns(session) or session.disconnected:
            return
        if session.ready:
            self.runner.output.data(session.slot, report, session.generation)
        else:
            # Before readiness there is no active consumer. Seed it with the
            # last initialization state AFTER the connected event is emitted.
            session.pending_input = bytes(report)

    put = put_nowait

    def empty(self):
        return True

    def get_nowait(self):
        raise queue.Empty


class ChildRunner:
    def __init__(self, backend, output, *, open_backend=None, stop_bluez=None):
        self.backend = backend
        self.output = output
        self.open_backend = open_backend
        self.stop_bluez = stop_bluez
        self.sessions = {}
        self.scan_task = None
        self.scan_token = None
        self._last_generation = 0

    def owns(self, session):
        return self.sessions.get(session.slot) is session

    async def _retire(self, slot):
        session = self.sessions.pop(slot, None)  # Invalidate callbacks FIRST.
        if session is None:
            return
        tasks = [t for t in [session.task, *session.feedback]
                 if t is not None and t is not asyncio.current_task()]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        identifier = session.identifier or session.target
        if identifier:
            try:
                await asyncio.wait_for(self.backend.disconnect(identifier), 5)
            except Exception:
                _logger.exception('BLE session disconnect failed')

    async def _connect(self, session, direct, options):
        def status(message):
            if self.owns(session):
                self.output.event({'e': 'status', 's': session.slot, 'g': session.generation, 'msg': message})

        def disconnected():
            if not self.owns(session) or session.disconnected:
                return
            session.disconnected = True
            if session.ready:
                self.output.event({'e': 'disconnected', 's': session.slot, 'g': session.generation})

        accepted = False
        identifier = None
        error = 'Connection failed'
        try:
            kwargs = dict(slot_index=session.slot, data_queue=_InputQueue(self, session),
                          on_status=status, on_disconnect=disconnected)
            # Bleak has connect_device; Bumble uses targeted scan_and_connect.
            if direct and hasattr(self.backend, 'connect_device'):
                operation = self.backend.connect_device(address=session.target, **kwargs)
            else:
                kwargs.update(target_address=session.target,
                              exclude_addresses=options.get('exclude_addresses'))
                if options.get('connect_timeout') is not None:
                    kwargs['connect_timeout'] = float(options['connect_timeout'])
                operation = self.backend.scan_and_connect(**kwargs)
            identifier = await asyncio.wait_for(operation, timeout=45)
            if identifier and self.owns(session) and not session.disconnected:
                session.identifier = identifier
                session.ready = True
                self.output.event({'e': 'connected', 's': session.slot, 'g': session.generation, 'mac': identifier})
                if session.pending_input is not None:
                    self.output.data(session.slot, session.pending_input, session.generation)
                    session.pending_input = None
                accepted = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        finally:
            if not accepted:
                if self.owns(session):
                    self.sessions.pop(session.slot, None)
                    try:
                        self.output.event({'e': 'connect_error', 's': session.slot, 'g': session.generation, 'msg': error})
                    except ConnectionError:
                        pass  # A dead parent must not prevent device cleanup.
                # Backend cancellation must also release partial connections;
                # the target fallback catches those not yet returned as IDs.
                if identifier or session.target:
                    try:
                        await asyncio.wait_for(self.backend.disconnect(identifier or session.target), 5)
                    except Exception:
                        _logger.exception('Failed connection cleanup failed')

    async def _stop_scan(self):
        self.scan_token = None
        task, self.scan_task = self.scan_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        stop = getattr(self.backend, 'stop_scan', None)
        if stop is not None:
            await stop()

    async def _scan(self, slot, token, continuous, generation):
        def found(device):
            if self.scan_token is token:
                self.output.event({'e': 'device_detected', 's': slot, 'g': generation, 'device': device})
        try:
            if continuous:
                await self.backend.start_scan(on_device_found=found)
            else:
                devices = await self.backend.scan_only()
                if self.scan_token is token:
                    self.output.event({'e': 'devices_found', 's': slot, 'g': generation, 'devices': devices})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.scan_token is token:
                self.output.event({'e': 'connect_error', 's': slot, 'g': generation, 'msg': str(exc)})

    def _feedback(self, session, action, data):
        async def run():
            if self.owns(session) and not session.disconnected:
                if action == 'rumble':
                    await self.backend.send_rumble(session.identifier, data)
                else:
                    await self.backend.set_led(session.identifier, data)
        if len(session.feedback) >= 64:
            raise BufferError('Too many outstanding BLE feedback commands')
        task = asyncio.create_task(run())
        session.feedback.add(task)
        def done(task):
            session.feedback.discard(task)
            if not task.cancelled() and task.exception() is not None:
                _logger.error('BLE feedback failed: %s', task.exception())
        task.add_done_callback(done)

    def _command_session(self, cmd):
        """Feedback/disconnect addresses identify ownership after UI slot moves."""
        address = cmd.get('address')
        if address is None:
            session = self.sessions.get(cmd['slot_index'])
            return session if session and session.generation == cmd.get('g') else None
        if not isinstance(address, str) or not address:
            raise ValueError('Invalid BLE command address')
        matches = [session for session in self.sessions.values()
                   if _address(address) in (_address(session.identifier), _address(session.target))]
        return matches[0] if len(matches) == 1 and matches[0].generation == cmd.get('g') else None

    async def command(self, cmd):
        if not isinstance(cmd, dict) or not isinstance(cmd.get('cmd'), str):
            raise ValueError('Malformed BLE command')
        action = cmd['cmd']
        slot = cmd.get('slot_index')
        if action in {'scan_connect', 'connect_device', 'scan_devices', 'scan_start',
                      'disconnect', 'rumble', 'set_led'}:
            if type(slot) is not int or not 0 <= slot < MAX_SLOTS:
                raise ValueError('Invalid BLE command slot')
        generation = cmd.get('g')
        if action in {'scan_connect', 'connect_device', 'scan_devices', 'scan_start',
                      'disconnect', 'rumble', 'set_led'}:
            if not valid_generation(generation):
                raise ValueError('Missing or invalid BLE command generation')
        if action in {'scan_connect', 'connect_device', 'scan_devices', 'scan_start'}:
            if generation <= self._last_generation:
                raise ValueError('Replayed BLE session generation')
            self._last_generation = generation
        if action in ('close', 'shutdown'):
            return False
        if action == 'stop_bluez':
            try:
                if self.stop_bluez is not None:
                    result = await asyncio.to_thread(self.stop_bluez)
                    if result is not True:
                        raise RuntimeError('Bluetooth takeover did not succeed')
                self.output.event({'e': 'bluez_stopped'})
            except Exception as exc:
                self.output.event({'e': 'error', 'ctx': 'stop_bluez', 'msg': str(exc)})
        elif action == 'open':
            try:
                if self.open_backend is not None:
                    await self.open_backend(cmd)
                else:
                    await self.backend.open()
                self.output.event({'e': 'open_ok'})
            except Exception as exc:
                self.output.event({'e': 'error', 'ctx': 'open', 'msg': str(exc)})
        elif action in ('connect_device', 'scan_connect'):
            target = cmd.get('address') if action == 'connect_device' else cmd.get('target_address')
            if target is not None and (not isinstance(target, str) or not target):
                raise ValueError('Invalid BLE target')
            # Never retire another slot's controller just because a duplicate
            # connect command mentions the same address with different casing.
            if target and any(s.slot != slot and not s.disconnected and _address(target) in
                              (_address(s.target), _address(s.identifier))
                              for s in self.sessions.values()):
                self.output.event({'e': 'connect_error', 's': slot, 'g': generation,
                                   'msg': 'Controller is already assigned to another slot'})
                return True
            await self._stop_scan()
            await self._retire(slot)
            session = _Session(slot, target, generation)
            self.sessions[slot] = session
            session.task = asyncio.create_task(self._connect(session, action == 'connect_device', cmd))
        elif action in ('scan_devices', 'scan_start'):
            await self._stop_scan()
            # Discovery is independent of wire slot allocation. Explicit
            # cancellation owns retiring connection attempts, not a UI index.
            token = self.scan_token = object()
            self.scan_task = asyncio.create_task(self._scan(slot, token, action == 'scan_start', generation))
        elif action == 'scan_stop':
            await self._stop_scan()
        elif action == 'cancel_all_scans':
            generations = cmd.get('cancel_generations', [])
            if not isinstance(generations, list) or len(generations) > MAX_SLOTS or not all(valid_generation(g) for g in generations):
                raise ValueError('Invalid cancellation generations')
            for index, session in list(self.sessions.items()):
                if not session.ready or session.generation in generations:
                    await self._retire(index)
            await self._stop_scan()
        elif action == 'disconnect':
            session = self._command_session(cmd)
            if session is not None:
                await self._retire(session.slot)
        elif action in ('rumble', 'set_led'):
            session = self._command_session(cmd)
            if session and session.ready and not session.disconnected:
                if action == 'rumble':
                    data = base64.b64decode(cmd['data'], validate=True)
                    if len(data) > 512:
                        raise ValueError('Oversized BLE rumble command')
                else:
                    data = cmd.get('new_slot_index', slot)
                    if type(data) is not int or not 0 <= data < MAX_SLOTS:
                        raise ValueError('Invalid player LED index')
                self._feedback(session, action, data)
        else:
            raise ValueError('Unknown BLE command')
        return True

    async def run(self, commands):
        try:
            self.output.event({'e': 'ready', 'protocol': PROTOCOL_VERSION})
            while True:
                cmd = await asyncio.to_thread(commands.get)
                if cmd is None or not await self.command(cmd):
                    break
        finally:
            # asyncio cancellation does not cancel an already running get().
            # Wake that executor thread before asyncio.run shuts its pool down.
            try:
                commands.put_nowait(None)
            except queue.Full:
                pass
            # EOF, malformed commands, explicit shutdown and output failure all
            # take the same cleanup path. No un-awaited cancellation or orphan scans.
            try:
                await self._stop_scan()
            finally:
                try:
                    await asyncio.gather(*(self._retire(slot) for slot in list(self.sessions)),
                                         return_exceptions=True)
                finally:
                    await asyncio.wait_for(self.backend.close(), 5)


def run_subprocess(backend, *, open_backend=None, stop_bluez=None):
    """Entrypoint used after platform imports/path setup in each child script."""
    commands = queue.Queue(maxsize=128)

    def stop(error=None):
        if error:
            print(f'BLE IPC stopped: {error}', file=sys.stderr, flush=True)
        # Stopping has priority over queued commands, without blocking the writer.
        while True:
            try:
                commands.put_nowait(None)
                return
            except queue.Full:
                try:
                    commands.get_nowait()
                except queue.Empty:
                    pass

    def read_commands():
        try:
            while True:
                line = sys.stdin.readline(MAX_JSON_BYTES + 1)
                if not line:
                    break
                if len(line.encode('utf-8')) > MAX_JSON_BYTES or not line.endswith('\n'):
                    raise ValueError('Oversized or truncated BLE command')
                if line.strip():
                    commands.put_nowait(json.loads(line))
        except Exception as exc:
            stop(exc)
            return
        stop()

    output = OutputWriter(sys.stdout.buffer.fileno(), stop)
    runner = ChildRunner(backend, output, open_backend=open_backend, stop_bluez=stop_bluez)
    threading.Thread(target=read_commands, name='ble-commands', daemon=True).start()
    async def serve():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        old_handler = None
        installed = False
        if sys.platform != 'win32' and threading.current_thread() is threading.main_thread():
            old_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM,
                          lambda *_: loop.call_soon_threadsafe(task.cancel))
            installed = True
        try:
            await runner.run(commands)
        finally:
            if installed:
                signal.signal(signal.SIGTERM, old_handler)

    try:
        asyncio.run(serve())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except Exception as exc:
        print(f'BLE subprocess failed: {exc}', file=sys.stderr, flush=True)
    finally:
        stop()  # Release a cancelled to_thread(commands.get) before interpreter exit.
        output.close()
