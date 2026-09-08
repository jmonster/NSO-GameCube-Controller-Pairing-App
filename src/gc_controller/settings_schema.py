"""Bounded settings decoding and typed v1-v4 migration, before any state change."""
import json
import math

from .controller_constants import BLE_DEVICE_CAL_KEYS, MAX_SLOTS

MAX_SETTINGS_BYTES = 1024 * 1024
MAX_DEVICES = 256
GLOBAL_KEYS = {
    'auto_connect', 'auto_scan_ble', 'emulation_mode', 'trigger_bump_100_percent',
    'minimize_to_tray', 'stick_deadzone', 'map_home_to_guide', 'rumble_intensity',
    'known_ble_devices', 'run_at_startup', 'slot_assignments', 'device_links',
}
BOOL_KEYS = {'auto_connect', 'auto_scan_ble', 'trigger_bump_100_percent',
             'minimize_to_tray', 'map_home_to_guide', 'run_at_startup'}


def _mapping(value, label, limit=MAX_DEVICES):
    if not isinstance(value, dict) or len(value) > limit:
        raise ValueError(f'{label} must be an object with at most {limit} entries')
    return value


def _text(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid device identifier')
    return value


def _number(value, low, high, *, exclusive_low=False, exclusive_high=False):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError('Calibration values must be finite numbers, not booleans/strings')
    if value < low or value > high or (exclusive_low and value == low) or (exclusive_high and value == high):
        raise ValueError(f'Calibration value outside permitted range {low}..{high}')
    return value


def _calibration(value):
    result = {}
    for key, val in _mapping(value, 'Device calibration', 64).items():
        if key not in BLE_DEVICE_CAL_KEYS:
            continue
        if key.endswith('_octagon'):
            if val is None:
                result[key] = None
                continue
            if not isinstance(val, (list, tuple)) or len(val) != 8:
                raise ValueError('Stick octagon must contain eight points')
            points = []
            for point in val:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError('Stick octagon points must have two coordinates')
                points.append([_number(coordinate, -1, 1) for coordinate in point])
            result[key] = points
        elif key.startswith('trigger_'):
            result[key] = _number(val, 0, 255)
        elif '_range_' in key:
            result[key] = _number(val, 0, 4095, exclusive_low=True)
        else:
            result[key] = _number(val, 0, 4095)
    return result


def validate_global(value):
    result = {}
    for key, val in _mapping(value, 'Global settings', 64).items():
        if key not in GLOBAL_KEYS:
            continue
        if key in BOOL_KEYS:
            if type(val) is not bool:
                raise ValueError(f'{key} must be boolean')
        elif key == 'emulation_mode':
            if not isinstance(val, str) or val not in {'xbox360', 'dolphin_pipe', 'dsu'}:
                raise ValueError('Unknown emulation mode')
        elif key == 'stick_deadzone':
            _number(val, 0, 1, exclusive_high=True)
        elif key == 'rumble_intensity':
            _number(val, 0, 1)
        elif key == 'slot_assignments':
            val = {_text(identity): index for identity, index in _mapping(val, key).items()}
            if any(type(index) is not int or not 0 <= index < MAX_SLOTS for index in val.values()):
                raise ValueError('Player assignments must be valid integer slot indices')
        elif key == 'device_links':
            val = {_text(left): _text(right) for left, right in _mapping(val, key).items()}
            if any(left == right or (right in val and val[right] != left) for left, right in val.items()):
                raise ValueError('Device links must be unambiguous pairs')
            if len(set(val.values())) != len(val):
                raise ValueError('Multiple device links point to the same identity')
        elif key == 'known_ble_devices':
            devices = {}
            for identity, calibration in _mapping(val, key).items():
                identity = _text(identity).upper()
                calibration = _calibration(calibration)
                if identity in devices and devices[identity] != calibration:
                    raise ValueError('Conflicting case-insensitive controller identities')
                devices[identity] = calibration
            val = devices
        result[key] = val
    return result


def normalize_settings(saved):
    """Return a fresh, validated global mapping, never mutate caller objects."""
    _mapping(saved, 'Settings root', 128)
    version = saved.get('version', 1)
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise ValueError('Unsupported settings version; refusing a destructive downgrade')
    if version == 1:
        global_settings = dict(saved)
        if 'bump_100_percent' in saved and 'trigger_bump_100_percent' not in saved:
            global_settings['trigger_bump_100_percent'] = saved['bump_100_percent']
    else:
        global_settings = dict(_mapping(saved.get('global', {}), 'Global settings', 64))
    if version == 2:
        slots = _mapping(saved.get('slots', {}), 'Legacy slots', MAX_SLOTS)
        if any(key not in {str(i) for i in range(MAX_SLOTS)} for key in slots):
            raise ValueError('Invalid legacy slot index')
        devices = dict(_mapping(global_settings.get('known_ble_devices', {}), 'Known devices'))
        old_addresses = global_settings.pop('known_ble_addresses', [])
        if not isinstance(old_addresses, list) or len(old_addresses) > MAX_DEVICES:
            raise ValueError('Invalid legacy Bluetooth address list')
        for slot in slots.values():
            _mapping(slot, 'Legacy slot', 128)
            addr = slot.get('preferred_ble_address')
            if addr is not None and not isinstance(addr, str):
                raise ValueError('Invalid legacy preferred Bluetooth address')
            if addr:
                devices.setdefault(_text(addr).upper(), _calibration(slot))
        for addr in old_addresses:
            devices.setdefault(_text(addr).upper(), {})
        global_settings['known_ble_devices'] = devices
    return validate_global(global_settings)


def decode_settings(payload):
    if len(payload) > MAX_SETTINGS_BYTES:
        raise ValueError('Settings file exceeds size limit')
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate settings key: {key}')
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError(f'Non-finite JSON number: {value}')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('JSON numeric overflow')
        return number
    saved = json.loads(payload.decode('utf-8'), object_pairs_hook=unique_pairs,
                       parse_constant=reject_constant, parse_float=finite_float)
    return normalize_settings(saved)


def encode_settings(global_settings):
    result = validate_global(global_settings)
    payload = json.dumps({'version': 4, 'global': result}, indent=2, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_SETTINGS_BYTES:
        raise ValueError('Settings file exceeds size limit')
    return payload
