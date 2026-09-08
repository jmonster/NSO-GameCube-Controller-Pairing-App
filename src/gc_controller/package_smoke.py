"""Hardware-free frozen dependency/resource and binary-pipe diagnostic.

Only --package-smoke invokes this. It neither scans Bluetooth, opens controllers,
creates virtual devices, registers autostart, nor touches user settings/FIFOs.
"""
import importlib
import json
import os
from pathlib import Path
import platform
import sys


def inspect_package():
    errors, checked = [], []

    def check(name, function):
        try:
            function()
            checked.append(name)
        except Exception as exc:
            errors.append({'check': name, 'error': f'{type(exc).__name__}: {exc}'})

    modules = ['hid', 'usb.core', 'customtkinter', 'tkinter', '_tkinter', 'PIL.Image',
               'gc_controller.connection_manager', 'gc_controller.emulation_manager',
               'gc_controller.controller_ui', 'gc_controller.ble.parent',
               'gc_controller.ble.sessions', 'gc_controller.ble.child_runtime']
    if sys.platform in ('darwin', 'win32'):
        modules += ['gc_controller.ble.bleak_backend']
        backend = 'corebluetooth' if sys.platform == 'darwin' else 'winrt'
        modules += [f'bleak.backends.{backend}.client', f'bleak.backends.{backend}.scanner']
    else:
        modules += ['gc_controller.ble.bumble_backend', 'gc_controller.ble.bluez']
    for module in modules:
        check(module, lambda module=module: importlib.import_module(module))

    def tcl():
        import tkinter
        interpreter = tkinter.Tcl()
        if not interpreter.eval('info patchlevel'):
            raise RuntimeError('Tcl resource initialization failed')
    check('Tcl resources', tcl)

    def assets():
        from PIL import Image
        directory = Path(__file__).resolve().parent
        if not (directory / 'fonts' / 'VarelaRound-Regular.ttf').is_file():
            raise RuntimeError('Bundled font missing')
        images = list((directory / 'assets' / 'controller').glob('*.png'))
        if not images:
            raise RuntimeError('Bundled controller images missing')
        for image in images:
            with Image.open(image) as opened:
                opened.verify()
    check('Controller resources', assets)

    if sys.platform == 'darwin':
        def usb_library():
            import usb.backend.libusb1
            directory = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
            library = directory / 'libusb-1.0.dylib'
            backend = usb.backend.libusb1.get_backend(find_library=lambda _: str(library))
            if backend is None:
                raise RuntimeError('Bundled libusb could not be loaded')
        check('Bundled libusb', usb_library)

        def privacy():
            import plistlib
            contents = Path(sys.executable).parent.parent
            with (contents / 'Info.plist').open('rb') as source:
                info = plistlib.load(source)
            if not info.get('NSBluetoothAlwaysUsageDescription'):
                raise RuntimeError('Bluetooth purpose string missing')
        check('Bluetooth privacy declaration', privacy)
    if sys.platform == 'win32':
        def vigem():
            # Importing vgamepad may connect to a driver unavailable on CI.
            # Verify the bundled client DLL without creating a virtual device.
            directory = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
            if not list((directory / 'vgamepad').rglob('ViGEmClient.dll')):
                raise RuntimeError('ViGEm client DLL is missing')
        check('Bundled ViGEm client', vigem)
    return {'frozen': bool(getattr(sys, 'frozen', False)), 'platform': sys.platform,
            'architecture': platform.machine(), 'python': platform.python_version(),
            'checked': checked, 'errors': errors}


def read_challenge(stream):
    """Raw inherited pipes may return short reads without reaching EOF."""
    data = bytearray()
    while len(data) < 256:
        part = stream.read(256 - len(data))
        if not part:
            break
        data.extend(part)
    return bytes(data)


def main():
    report = inspect_package()
    challenge = read_challenge(sys.stdin.buffer)
    if challenge != bytes(range(256)):
        report['errors'].append({'check': 'stdin', 'error': 'Binary challenge was corrupted'})
    from gc_controller.ble.output import OutputWriter
    failures = []
    writer = OutputWriter(sys.stdout.buffer.fileno(), failures.append)
    writer.put(json.dumps(report, ensure_ascii=True).encode('ascii') + b'\n' + challenge)
    drained = writer.close(timeout=5)
    return 0 if drained and not failures and not report['errors'] else 1
