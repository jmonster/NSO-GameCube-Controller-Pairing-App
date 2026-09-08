"""Scoped Linux Bluetooth takeover with checked commands and restoration.

Stopping bluetooth.service is system-wide. Only the selected adapter receives
explicit power commands; restore the service only if it was initially active.
A root-owned runtime journal allows the next helper to recover interrupted
restoration. It cannot restore hardware immediately after uncatchable termination.
"""
from contextlib import contextmanager
import logging
import json
import secrets
import os
import re
import stat
import subprocess
import threading

logger = logging.getLogger(__name__)

_SYSTEM_PATH = ('/usr/sbin', '/usr/bin', '/sbin', '/bin')
_SAFE_ENV = {'PATH': ':'.join(_SYSTEM_PATH), 'LC_ALL': 'C', 'LANG': 'C',
             'SYSTEMD_PAGER': 'cat', 'SYSTEMD_COLORS': '0'}


def _system_tool(name):
    """Never execute system management tools from the invoking user's PATH."""
    if name not in ('systemctl', 'btmgmt', 'hciconfig'):
        raise ValueError('Unsupported Bluetooth management tool')
    for directory in _SYSTEM_PATH:
        path = os.path.realpath(os.path.join(directory, name))
        try:
            info = os.stat(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                    info.st_mode & 0o022 or not os.access(path, os.X_OK)):
                continue
            parent = os.path.dirname(path)
            while True:
                info = os.stat(parent)
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise RuntimeError('Unsafe system tool directory')
                if parent == '/': break
                parent = os.path.dirname(parent)
            return path
        except OSError:
            continue
    raise RuntimeError(f'Required root-owned system tool is unavailable: {name}')


def _validate_state(value):
    fields = {'version', 'hci_index', 'restore_service', 'restore_adapter', 'original_power'}
    if (type(value) is not dict or set(value) != fields or
            type(value['version']) is not int or value['version'] != 1 or
            type(value['hci_index']) is not int or not 0 <= value['hci_index'] <= 65535 or
            any(type(value[key]) is not bool for key in
                ('restore_service', 'restore_adapter', 'original_power'))):
        raise RuntimeError('Invalid Bluetooth recovery journal; administrator review required')
    return dict(value)


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate Bluetooth recovery field')
        result[key] = value
    return result


class RecoveryJournal:
    """Small protected /run record, published before a system mutation.

    Access is serialized by the system-wide lease, not by this object. /run is
    intentionally ephemeral across boots. No executable path is read from disk.
    """
    def __init__(self, directory='/run/nso-gc-controller', *, owner_uid=0):
        self.directory = directory
        self.owner_uid = owner_uid
        self.name = 'bluetooth-state.json'

    @contextmanager
    def _directory(self):
        try: os.mkdir(self.directory, 0o700)
        except FileExistsError: pass
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o077:
                raise RuntimeError('Unsafe Bluetooth recovery directory')
            yield fd
        finally:
            os.close(fd)

    def read(self):
        with self._directory() as directory:
            try:
                fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=directory)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or
                        info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 4096):
                    raise RuntimeError('Unsafe Bluetooth recovery file')
                data = stream.read(4097)
                if len(data) > 4096: raise RuntimeError('Oversized Bluetooth recovery file')
        return _validate_state(json.loads(data.decode('utf-8'), object_pairs_hook=_unique_fields))

    def write(self, state):
        payload = (json.dumps(_validate_state(state), sort_keys=True) + '\n').encode('utf-8')
        with self._directory() as directory:
            name = '.bluetooth-' + secrets.token_hex(16)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(payload); stream.flush(); os.fsync(stream.fileno())
                os.replace(name, self.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try: os.unlink(name, dir_fd=directory)
                except FileNotFoundError: pass

    def clear(self):
        with self._directory() as directory:
            try: os.unlink(self.name, dir_fd=directory)
            except FileNotFoundError: pass
            os.fsync(directory)


@contextmanager
def _exclusive_lease():
    # The lock covers the system-wide service, not just a particular adapter.
    # Never follow a link or reuse a lock file owned by an unprivileged user.
    import fcntl
    fd = os.open('/run/lock/nso-gc-bluetooth.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise RuntimeError('Unsafe Bluetooth ownership lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Bluetooth adapter is already owned by another helper') from exc
        yield
    finally:
        os.close(fd)  # Do not unlink: another opener might already hold this inode.


class BlueZLease:
    def __init__(self, hci_index, *, run=None, lock_factory=None, adapter_tool=None,
                 resolve_tool=None, journal=None):
        if type(hci_index) is not int or not 0 <= hci_index <= 65535:
            raise ValueError('Invalid Bluetooth HCI adapter index')
        self.hci_index = hci_index
        self._run = subprocess.run if run is None else run
        self._lock_factory = _exclusive_lease if lock_factory is None else lock_factory
        self._tool_name = adapter_tool
        self._tool = None
        self._resolve_tool = _system_tool if resolve_tool is None else resolve_tool
        self._systemctl = None
        self._journal = RecoveryJournal() if journal is None else journal
        self._journal_written = False
        self._restoring_index = hci_index
        self._guard = threading.RLock()
        self._lease = None
        self._acquired = False
        self._restore_service = False
        self._restore_adapter = False
        self._original_power = False

    def _command(self, args):
        result = self._run(args, capture_output=True, text=True, timeout=5,
                           env=dict(_SAFE_ENV), cwd='/', stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip()
            raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}): {detail}")
        return result.stdout

    def _power(self, on):
        if os.path.basename(self._tool) == 'btmgmt':
            self._command([self._tool, '--index', str(self._restoring_index),
                           'power', 'on' if on else 'off'])
        else:
            self._command([self._tool, f'hci{self._restoring_index}', 'up' if on else 'down'])

    def _adapter_is_up(self):
        if os.path.basename(self._tool) == 'btmgmt':
            text = self._command([self._tool, '--index', str(self.hci_index), 'info'])
            match = re.search(r'^\s*current settings:\s*([^\r\n]*)', text, re.MULTILINE)
            if match is None:
                raise RuntimeError('Cannot determine current Bluetooth adapter power state')
            return 'powered' in match.group(1).split()
        text = self._command([self._tool, f'hci{self.hci_index}'])
        # Restrict to the flags line, not e.g. an adapter name containing "UP".
        flags = re.search(r'^\s*(UP(?:\s+[^\r\n]*)?|DOWN)\s*$', text, re.MULTILINE)
        if flags is None:
            raise RuntimeError('Cannot determine current Bluetooth adapter power state')
        return flags.group(1).split()[0] == 'UP'

    def acquire(self):
        """Snapshot before mutation; rollback even on timeout/partial failure."""
        with self._guard:
            if self._acquired:
                return True
            if self._lease is not None:
                raise RuntimeError('Bluetooth restoration must finish before acquiring again')
            self._systemctl = self._resolve_tool('systemctl')
            if self._tool_name:
                self._tool = self._resolve_tool(self._tool_name)
            else:
                try: self._tool = self._resolve_tool('btmgmt')
                except RuntimeError: self._tool = self._resolve_tool('hciconfig')
            lease = self._lock_factory()
            lease.__enter__()
            self._lease = lease
            try:
                recovery = self._journal.read()
                if recovery is not None:
                    recovery = _validate_state(recovery)
                    self._journal_written = True
                    self._restoring_index = recovery['hci_index']
                    self._original_power = recovery['original_power']
                    self._restore_service = recovery['restore_service']
                    self._restore_adapter = recovery['restore_adapter']
                    self._restore()
                    logger.warning('Recovered interrupted Bluetooth ownership for hci%s', self._restoring_index)
                self._restoring_index = self.hci_index
                state = self._command([self._systemctl, 'show', 'bluetooth.service',
                                       '--property=ActiveState', '--value']).strip()
                if state not in ('active', 'inactive', 'failed'):
                    raise RuntimeError(f'Bluetooth service state is not stable: {state!r}')
                was_up = self._adapter_is_up()
                self._journal.write({'version': 1, 'hci_index': self.hci_index,
                                     'original_power': was_up, 'restore_adapter': True,
                                     'restore_service': state == 'active'})
                self._journal_written = True
                self._original_power = was_up
                self._restore_adapter = True
                # Record responsibility BEFORE commands: a timeout does not
                # prove the command had no side effects.
                if state == 'active':
                    self._restore_service = True
                    self._command([self._systemctl, 'stop', 'bluetooth.service'])
                self._power(False)
                self._acquired = True
                return True
            except BaseException:
                try:
                    self.release()
                except Exception:
                    logger.exception('Bluetooth rollback failed')
                raise

    @property
    def is_acquired(self):
        with self._guard:
            return self._acquired

    def _restore(self):
        errors = []
        # The durable record remains conservative until BOTH operations finish.
        # If killed part-way, the next helper can safely repeat restoration.
        if self._restore_service:
            self._restore_adapter = True
            try:
                self._command([self._systemctl, 'start', 'bluetooth.service'])
                self._restore_service = False
            except Exception as exc:
                errors.append(str(exc))
        if self._restore_adapter:
            try:
                self._power(self._original_power)
                self._restore_adapter = False
            except Exception as exc:
                errors.append(str(exc))
        if not errors and self._journal_written:
            try:
                self._journal.clear()
                self._journal_written = False
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('Could not restore Bluetooth state: ' + '; '.join(errors))

    def release(self):
        """Keep the journal and ownership if restoration fails; caller may retry."""
        with self._guard:
            self._acquired = False
            self._restore()
            if self._lease is not None:
                lease, self._lease = self._lease, None
                lease.__exit__(None, None, None)
