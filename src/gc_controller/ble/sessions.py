"""Parent-owned wire generations, independent of UI player-slot placement."""
from collections import deque
from dataclasses import dataclass, field
import threading

from .ipc import MAX_SLOTS, valid_generation

CONNECT = {'connect_device', 'scan_connect'}
SCAN = {'scan_devices', 'scan_start'}
TERMINAL = {'disconnected', 'connect_error', 'devices_found'}


def address(value):
    return value.upper().removesuffix('/P').removesuffix('/R') if value else None


@dataclass
class _Route:
    wire: int
    generation: int
    ui_slot: int
    kind: str
    address: str | None = None
    confirmed: bool = False
    consumer: object = None
    pending: deque = field(default_factory=deque)


class SessionRouter:
    """All operations are serialized with command publication in CommandTransport.

    The reader may run before the UI handles Connected. Buffer a bounded number
    of ordered reports until that exact generation has an installed consumer.
    Retired generations never deliver input, status, failures or disconnects to
    a later occupant, even when their bytes were already in the OS pipe.
    """
    def __init__(self, max_pending=256):
        self._lock = threading.RLock()
        self._generation = 0
        self._connections = {}  # wire slot -> route
        self._scan = None
        self._max_pending = max_pending

    def clear(self):
        with self._lock:
            self._connections.clear()
            self._scan = None

    def prepare(self, command):
        """Return a wire command, or None for an already retired target."""
        cmd = dict(command)
        action = cmd.get('cmd')
        with self._lock:
            if action in CONNECT | SCAN:
                ui = cmd.get('slot_index')
                if type(ui) is not int or not 0 <= ui < MAX_SLOTS:
                    raise ValueError('Invalid requested player slot')
                self._generation += 1
                if not valid_generation(self._generation):
                    raise OverflowError('BLE session generation exhausted')
                if action in CONNECT:
                    # Reuse only an unfinished attempt for the same UI request,
                    # never a live controller moved to another player slot.
                    previous = next((r for r in self._connections.values()
                                     if r.ui_slot == ui and r.consumer is None), None)
                    free = [s for s in range(MAX_SLOTS) if s not in self._connections]
                    wire = previous.wire if previous else (ui if ui in free else next(iter(free), None))
                    if wire is None:
                        raise BufferError('No free BLE transport slot')
                    route = _Route(wire, self._generation, ui, 'connect',
                                   address(cmd.get('address') or cmd.get('target_address')))
                    self._connections[wire] = route
                    self._scan = None  # A connect stops discovery in the child.
                else:
                    route = self._scan = _Route(ui, self._generation, ui, 'scan')
                cmd.update(slot_index=route.wire, g=route.generation)
            elif action in {'rumble', 'set_led', 'disconnect'}:
                wanted = address(cmd.get('address'))
                matches = [r for r in self._connections.values()
                           if (r.address == wanted if wanted else r.ui_slot == cmd.get('slot_index'))]
                if len(matches) != 1:
                    return None
                route = matches[0]
                cmd.update(slot_index=route.wire, g=route.generation)
                if action == 'disconnect':
                    del self._connections[route.wire]  # Invalidate buffered callbacks now.
            elif action == 'cancel_all_scans':
                # Include Connected-but-not-yet-installed sessions. The child
                # may already consider them ready; it must still retire them.
                pending = [r for r in self._connections.values() if r.consumer is None]
                cmd['cancel_generations'] = [r.generation for r in pending]
                for route in pending:
                    del self._connections[route.wire]
                self._scan = None
            elif action == 'scan_stop':
                self._scan = None
            elif action in {'shutdown', 'close'}:
                self.clear()
            return cmd

    def _route(self, wire, generation):
        route = self._connections.get(wire)
        if route and route.generation == generation:
            return route
        route = self._scan
        if route and route.wire == wire and route.generation == generation:
            return route
        return None

    def event(self, event):
        """Translate wire slots to requested/installed UI slots; ignore old epochs."""
        with self._lock:
            if 's' not in event:
                return dict(event)
            wire = event.get('_wire', event['s'])
            route = self._route(wire, event.get('g'))
            if route is None:
                return None
            if event['e'] == 'connected':
                if route.kind != 'connect' or not event.get('mac'):
                    raise ValueError('Invalid connected event')
                route.confirmed = True
                route.address = address(event['mac'])
            return {**event, '_wire': wire, 's': route.ui_slot}

    def bind(self, event, ui_slot, consumer):
        """Publish the consumer then flush its initial reports in exact order."""
        with self._lock:
            route = self._route(event.get('_wire', event.get('s')), event.get('g'))
            if route is None:
                return False
            if route.kind != 'connect' or not route.confirmed:
                raise ValueError('Cannot bind a session before Connected')
            if type(ui_slot) is not int or not 0 <= ui_slot < MAX_SLOTS:
                raise ValueError('Invalid destination player slot')
            route.ui_slot, route.consumer = ui_slot, consumer
            while route.pending:
                consumer(route.pending.popleft())  # Queue-full propagates as integrity failure.
            return True

    def data(self, wire, generation, report):
        with self._lock:
            route = self._route(wire, generation)
            if route is None:
                return
            if route.kind != 'connect' or not route.confirmed:
                raise ValueError('Input arrived before Connected')
            if route.consumer is None:
                if len(route.pending) >= self._max_pending:
                    raise BufferError('BLE consumer installation backlog exceeded its limit')
                route.pending.append(bytes(report))
            else:
                route.consumer(report)

    def finish(self, event):
        """Retire a terminal event only after its owning application handles it."""
        if event.get('e') not in TERMINAL:
            return
        with self._lock:
            route = self._route(event.get('_wire', event.get('s')), event.get('g'))
            if route is self._scan:
                self._scan = None
            elif route is not None:
                self._connections.pop(route.wire, None)
