"""Typed, atomic settings persistence with non-destructive v1-v4 migration."""
import logging
import os
import stat
import threading
from typing import List

from .settings_schema import GLOBAL_KEYS, MAX_SETTINGS_BYTES, decode_settings, encode_settings
from .settings_storage import atomic_write

logger = logging.getLogger(__name__)


class SettingsManager:
    def __init__(self, slot_calibrations: List[dict], settings_dir: str):
        self._slot_calibrations = slot_calibrations
        self._settings_file = os.path.join(settings_dir, 'gc_controller_settings.json')
        self._save_lock = threading.Lock()
        self._load_error = None

    def load(self):
        """Validate the entire document before changing a single live setting.

        Invalid/future settings remain untouched. Autosave is blocked until a
        subsequent explicit reload succeeds (or the user removes the file).
        """
        with self._save_lock:
            try:
                try:
                    info = os.lstat(self._settings_file)
                except FileNotFoundError:
                    self._load_error = None
                    return True
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
                    raise ValueError('Settings must be a bounded regular file')
                with open(self._settings_file, 'rb') as stream:
                    payload = stream.read(MAX_SETTINGS_BYTES + 1)
                global_settings = decode_settings(payload)
                self._slot_calibrations[0].update(global_settings)
                self._load_error = None
                return True
            except Exception as exc:
                self._load_error = str(exc)
                logger.warning('Settings ignored; original preserved and autosave blocked: %s', exc)
                return False

    def save(self):
        """Reject unsafe values before touching the last valid settings file."""
        with self._save_lock:
            if self._load_error is not None:
                raise ValueError(f'Settings were not loaded safely; repair/reload before saving: {self._load_error}')
            calibration = self._slot_calibrations[0]
            payload = encode_settings({key: calibration[key] for key in GLOBAL_KEYS if key in calibration})
            atomic_write(self._settings_file, payload)
