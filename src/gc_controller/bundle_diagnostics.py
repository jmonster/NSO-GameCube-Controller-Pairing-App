"""Offline checks for the shipped macOS bundle; never scan or open a controller."""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys

# Dynamic imports in Bleak/PyObjC are not reliably exercised by importing the GUI.
MODULES = (
    "bleak.backends.corebluetooth.client",
    "bleak.backends.corebluetooth.scanner",
    "CoreBluetooth", "Foundation", "AppKit", "objc",
    "hid", "usb.backend.libusb1", "_tkinter", "tkinter",
    "customtkinter", "PIL.Image", "pystray._darwin",
    "gc_controller.ble.bleak_backend",
)


def is_inside(path: Path, root: Path) -> bool:
    """Resolve symlinks: a Homebrew symlink inside the app is not a bundled library."""
    return path.resolve().is_relative_to(root.resolve())


def check_module(name: str, bundle_root: Path) -> dict:
    try:
        module = importlib.import_module(name)
        origin = getattr(module, "__file__", None)
        if not origin or not is_inside(Path(origin), bundle_root):
            raise RuntimeError(f"module is not bundled: {origin!r}")
        return {"name": name, "ok": True, "origin": origin}
    except Exception as exc:
        return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def find_bundled_libusb(runtime_root: Path, bundle_root: Path) -> Path:
    candidates = sorted(runtime_root.glob("libusb-1.0*.dylib"))
    for candidate in candidates:
        if candidate.is_file() and is_inside(candidate, bundle_root):
            return candidate.resolve()
    raise RuntimeError("libusb-1.0.dylib is missing from the app bundle")


def inspect_bundle() -> dict:
    result = {"schema_version": 1, "platform": sys.platform,
              "frozen": bool(getattr(sys, "frozen", False)), "checks": []}
    if sys.platform != "darwin" or not result["frozen"]:
        result.update(ok=False, error="Run this check using the frozen macOS app, not system Python.")
        return result
    bundle_root = Path(sys.executable).resolve().parents[1]  # <app>/Contents
    runtime_root = Path(sys._MEIPASS)
    result["checks"] = [check_module(name, bundle_root) for name in MODULES]
    try:
        library = find_bundled_libusb(runtime_root, bundle_root)
        backend_module = importlib.import_module("usb.backend.libusb1")
        # Explicit path prevents an installed Homebrew libusb from hiding a broken bundle.
        backend = backend_module.get_backend(find_library=lambda _name: str(library))
        if backend is None:
            raise RuntimeError("bundled libusb could not be loaded")
        result["checks"].append({"name": "bundled-libusb", "ok": True, "origin": str(library)})
    except Exception as exc:
        result["checks"].append({"name": "bundled-libusb", "ok": False,
                                 "error": f"{type(exc).__name__}: {exc}"})
    result["ok"] = all(check["ok"] for check in result["checks"])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # A windowed PyInstaller executable may not have stdout. Always write a report.
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    result = inspect_bundle()
    args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if result["ok"] else 1
