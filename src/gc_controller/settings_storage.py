"""Stable settings location and failure-safe, same-directory file publication."""
import logging
import os
import stat
import sys
import tempfile

_logger = logging.getLogger(__name__)
SETTINGS_NAME = 'gc_controller_settings.json'
from .settings_schema import MAX_SETTINGS_BYTES, decode_settings

MAX_LEGACY_BYTES = MAX_SETTINGS_BYTES


def atomic_write(path, payload, *, overwrite=True):
    """Publish complete bytes; never truncate the existing settings on failure.

    Migration uses an exclusive hard-link publication to avoid overwriting a
    concurrent first launch. Unsupported filesystems retain the legacy file.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix='.gc-settings-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # Atomic no-clobber, even across processes.
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def get_settings_dir(*, platform=None, home=None, environ=None, frozen=None, cwd=None):
    """Share platform settings between source runs and packaged applications.

    Port of pookee/7a33337. Only source runs look for the legacy cwd file; a
    packaged application must not import configuration from an arbitrary cwd.
    """
    platform = sys.platform if platform is None else platform
    home = os.path.expanduser('~') if home is None else os.fspath(home)
    environ = os.environ if environ is None else environ
    frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
    if platform == 'darwin':
        base = os.path.join(home, 'Library', 'Application Support')
    elif platform == 'win32':
        base = environ.get('APPDATA') or home
        if not os.path.isabs(base):
            base = home
    else:
        base = environ.get('XDG_CONFIG_HOME') or os.path.join(home, '.config')
        if not os.path.isabs(base):
            base = os.path.join(home, '.config')
    directory = os.path.abspath(os.path.join(base, 'NSO-GC-Controller'))
    os.makedirs(directory, mode=0o700, exist_ok=True)

    if not frozen:
        target = os.path.join(directory, SETTINGS_NAME)
        if not os.path.lexists(target):
            try:
                legacy = os.path.join(os.getcwd() if cwd is None else os.fspath(cwd), SETTINGS_NAME)
                info = os.lstat(legacy)
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LEGACY_BYTES:
                    raise ValueError('Legacy settings must be a bounded regular file')
                with open(legacy, 'rb') as stream:
                    payload = stream.read(MAX_LEGACY_BYTES + 1)
                decode_settings(payload)  # Validate, but retain the original bytes/version.
                atomic_write(target, payload, overwrite=False)
                _logger.info('Migrated settings from %s to %s; original retained', legacy, target)
            except FileNotFoundError:
                pass
            except FileExistsError:
                pass  # Another process published settings first; never replace it.
            except (OSError, ValueError, UnicodeError, RecursionError) as exc:
                _logger.warning('Legacy settings migration skipped: %s', exc)
    return directory
