"""Bounded GUI-facing USB actors; hardware calls never run on the Tk owner.

A connection owns its blocking manager until cleanup finishes, even after the
UI cancels it. Feedback is latest-state (not an input/event queue). Native calls
cannot be forcibly cancelled; the session/path and worker budget stay reserved
until they return. No replacement thread is spawned to evade that bound.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)


class USBService:
    def __init__(self, manager_factory, post, *, max_sessions=4, max_scans=16):
        if any(type(n) is not int or n < 1 for n in (max_sessions, max_scans)):
            raise ValueError('USB worker limits must be positive integers')
        self.factory, self.post = manager_factory, post
        self._limit, self._scan_limit = max_sessions, max_scans
        self._condition = threading.Condition()
        self._sessions = set()
        self._paths = set()
        self._scan_pending, self._scan_tokens = {}, {}
        self._closed = False
        self._scanner = threading.Thread(target=self._scan_loop, name='usb-discovery', daemon=True)
        self._scanner.start()

    def connection(self, on_status, on_progress, on_failure=None):
        return USBConnection(self, on_status, on_progress, on_failure)

    def scan(self, key, callback):
        """One scanner; at most max_scans coalesced subscribers/results."""
        with self._condition:
            if self._closed or (key not in self._scan_tokens and
                                len(self._scan_tokens) >= self._scan_limit):
                return False
            token = object()
            self._scan_tokens[key] = token
            self._scan_pending[key] = (token, callback)
            self._condition.notify_all()
        return True

    def cancel_scan(self, key):
        with self._condition:
            self._scan_tokens.pop(key, None)
            self._scan_pending.pop(key, None)

    def _scan_loop(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._scan_pending)
                if self._closed:
                    return
                batch, self._scan_pending = self._scan_pending, {}
            try:
                devices = [dict(d) for d in self.factory.enumerate_devices()]
                error = None
            except Exception as exc:
                devices, error = [], str(exc)
            for key, (token, callback) in batch.items():
                if not self.post(self._deliver_scan, key, token, callback, devices, error):
                    with self._condition:
                        if self._scan_tokens.get(key) is token:
                            self._scan_tokens.pop(key, None)

    def _deliver_scan(self, key, token, callback, devices, error):
        with self._condition:
            if self._closed or self._scan_tokens.get(key) is not token:
                return
            self._scan_tokens.pop(key, None)
        callback([dict(d) for d in devices], error)

    def _reserve(self, owner, path, callback):
        with self._condition:
            if self._closed or path in self._paths or len(self._sessions) >= self._limit:
                return None
            actor = _USBActor(self, owner, path, callback)
            self._sessions.add(actor)
            self._paths.add(path)
            return actor

    def _release(self, actor):
        with self._condition:
            self._sessions.discard(actor)
            self._paths.discard(actor.path)

    def close(self, timeout=0.25):
        """Cancel immediately; wait only a single shared cleanup budget."""
        with self._condition:
            self._closed = True
            self._scan_pending.clear()
            self._scan_tokens.clear()
            actors = list(self._sessions)
            self._condition.notify_all()
        for actor in actors:
            actor.stop()
        deadline = time.monotonic() + max(0, timeout)
        for thread in [self._scanner, *(a.thread for a in actors)]:
            if thread is not threading.current_thread() and thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in [self._scanner, *(a.thread for a in actors)])


class USBConnection:
    """Nonblocking facade used by GUI slots; raw managers belong to actors."""
    def __init__(self, service, on_status, on_progress, on_failure):
        self.service = service
        self._on_status, self._on_progress = on_status, on_progress
        self._on_failure = on_failure
        self._actor = None

    @property
    def device(self):
        actor = self._actor
        return actor.manager.device if actor and actor.installed and not actor.stopped else None

    @property
    def device_path(self):
        actor = self._actor
        return actor.path if actor and actor.installed and not actor.stopped else None

    @property
    def is_connecting(self):
        actor = self._actor
        return bool(actor and not actor.installed and not actor.stopped)

    def connect_async(self, device_path, callback):
        if not device_path or self._actor is not None:
            return False
        actor = self.service._reserve(self, device_path, callback)
        if actor is None:
            self._on_status('USB device busy or previous operations still finishing; retry after cleanup')
            return False
        self._actor = actor
        try:
            actor.thread.start()
        except Exception:
            self._actor = None
            self.service._release(actor)
            raise
        return True

    def disconnect(self):
        actor, self._actor = self._actor, None
        if actor:
            actor.stop()

    def send_rumble(self, state):
        actor = self._actor
        return bool(actor and actor.installed and actor.feedback('rumble', bool(state)))

    def set_player_led(self, player_num):
        if type(player_num) is not int or not 1 <= player_num <= 4:
            return False
        actor = self._actor
        return bool(actor and actor.installed and actor.feedback('led', player_num))

    def transfer_to(self, destination):
        # GUI owner thread only. Transfer the actor itself, not its manager's
        # hardware lock, so a slow feedback call cannot block slot reassignment.
        actor = self._actor
        if (destination is self or destination.service is not self.service or
                destination._actor is not None or actor is None or
                not actor.installed or actor.stopped):
            return False
        self._actor = None
        actor.owner = destination
        destination._actor = actor
        with actor.condition:
            actor.pending.clear()
            actor.pending['rumble'] = False
            actor.condition.notify_all()
        return True


class _USBActor:
    def __init__(self, service, owner, path, callback):
        self.service, self.owner, self.path, self.callback = service, owner, path, callback
        self.condition = threading.Condition()
        self.pending = {}
        self.stopped = False
        self.installed = False
        self.manager = None
        self.thread = threading.Thread(target=self._run, name='usb-session', daemon=True)

    def stop(self):
        with self.condition:
            self.stopped = True
            self.pending.clear()
            self.condition.notify_all()

    def feedback(self, kind, value):
        with self.condition:
            if self.stopped:
                return False
            self.pending[kind] = value  # at most one LED and one rumble state
            self.condition.notify_all()
        return True

    def _status(self, kind, value):
        owner = self.owner
        self.service.post(self._deliver_status, owner, kind, value)

    def _deliver_status(self, owner, kind, value):
        if owner._actor is self and not self.stopped:
            (owner._on_status if kind == 'status' else owner._on_progress)(value)

    def _complete(self, success):
        owner = self.owner
        if owner._actor is not self or self.stopped or self.service._closed:
            self.stop()
            return
        was_installed = self.installed
        self.installed = success
        if not success:
            owner._actor = None
        try:
            if was_installed and not success:
                if owner._on_failure is not None:
                    owner._on_failure()
            else:
                self.callback(success)
        except Exception:
            if owner._actor is self:
                owner.disconnect()
            raise

    def _run(self):
        try:
            self.manager = self.service.factory(
                on_status=lambda msg: self._status('status', msg),
                on_progress=lambda val: self._status('progress', val))
            if self.stopped:
                return
            success = self.manager.connect_hid(device_path=self.path)
            if self.stopped:
                return
            if not self.service.post(self._complete, success):
                self.stop()
            if not success:
                return
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or self.pending)
                    if self.stopped:
                        return
                    # Stop packets supersede stale ON pulses; input reports are
                    # never coalesced here (this actor carries feedback only).
                    kind = 'rumble' if 'rumble' in self.pending else 'led'
                    value = self.pending.pop(kind)
                if kind == 'rumble':
                    self.manager.send_rumble(value)
                else:
                    self.manager.set_player_led(value)
        except Exception as exc:
            logger.exception('USB worker failed')
            self._status('status', f'USB operation failed: {exc}')
            self.service.post(self._complete, False)
        finally:
            if self.manager is not None:
                for operation in (lambda: self.manager.send_rumble(False), self.manager.disconnect):
                    try:
                        operation()
                    except Exception:
                        logger.exception('USB worker cleanup failed')
            self.service._release(self)
