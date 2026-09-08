"""Scoped Linux Bluetooth takeover with checked commands and restoration.

Stopping bluetooth.service is system-wide. Only the selected adapter receives
explicit power commands; restore the service only if it was initially active.
No userspace cleanup can run after SIGKILL, power loss or kernel failure.
"""
from contextlib import contextmanager
import logging
import os
import re
import shutil
import stat
import subprocess
import threading

logger = logging.getLogger(__name__)


@contextmanager
def _exclusive_lease():
    # The lock covers the system-wide service, not just a particular adapter.
    # Never follow a link or reuse a lock file owned by an unprivileged user.
    import fcntl
    fd = os.open('/run/lock/nso-gc-bluetooth.lock',
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise RuntimeError('Unsafe Bluetooth ownership lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Bluetooth adapter is already owned by another helper') from exc
        yield
    finally:
        os.close(fd)  # Do not unlink: another opener might already hold this inode.


class BlueZLease:
    def __init__(self, hci_index, *, run=None, lock_factory=None, adapter_tool=None):
        if type(hci_index) is not int or not 0 <= hci_index <= 65535:
            raise ValueError('Invalid Bluetooth HCI adapter index')
        self.hci_index = hci_index
        self._run = subprocess.run if run is None else run
        self._lock_factory = _exclusive_lease if lock_factory is None else lock_factory
        self._tool = adapter_tool
        self._guard = threading.RLock()
        self._lease = None
        self._acquired = False
        self._restore_service = False
        self._restore_adapter = False
        self._original_power = False

    def _command(self, args):
        result = self._run(args, capture_output=True, text=True, timeout=5,
                           env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip()
            raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}): {detail}")
        return result.stdout

    def _power(self, on):
        if os.path.basename(self._tool) == 'btmgmt':
            self._command([self._tool, '--index', str(self.hci_index),
                           'power', 'on' if on else 'off'])
        else:
            self._command([self._tool, f'hci{self.hci_index}', 'up' if on else 'down'])

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
            self._tool = self._tool or shutil.which('btmgmt') or shutil.which('hciconfig')
            if not self._tool:
                raise RuntimeError('Bluetooth takeover requires BlueZ btmgmt or hciconfig')
            lease = self._lock_factory()
            lease.__enter__()
            self._lease = lease
            try:
                state = self._command(['systemctl', 'show', 'bluetooth.service',
                                       '--property=ActiveState', '--value']).strip()
                if state not in ('active', 'inactive', 'failed'):
                    raise RuntimeError(f'Bluetooth service state is not stable: {state!r}')
                was_up = self._adapter_is_up()
                self._original_power = was_up
                self._restore_adapter = True
                # Record responsibility BEFORE commands: a timeout does not
                # prove the command had no side effects.
                if state == 'active':
                    self._restore_service = True
                    self._command(['systemctl', 'stop', 'bluetooth.service'])
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

    def release(self):
        """Restore only owned changes, trying both even if one operation fails."""
        with self._guard:
            errors = []
            try:
                # Restore the service first: it may auto-enable the adapter.
                # Then restore the selected adapter's original power state.
                if self._restore_service:
                    self._restore_adapter = True  # Service start may change power again.
                    try:
                        self._command(['systemctl', 'start', 'bluetooth.service'])
                        self._restore_service = False
                    except Exception as exc:
                        errors.append(str(exc))
                if self._restore_adapter:
                    try:
                        self._power(self._original_power)
                        self._restore_adapter = False
                    except Exception as exc:
                        errors.append(str(exc))
            finally:
                self._acquired = False
                # Keep ownership on failure until a retry/process exit. Another
                # helper must not inherit an unfinished restoration operation.
                if not errors and self._lease is not None:
                    lease, self._lease = self._lease, None
                    lease.__exit__(None, None, None)
            if errors:
                raise RuntimeError('Could not restore Bluetooth state: ' + '; '.join(errors))
