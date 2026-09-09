#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
if [[ "$(uname -s)" != Darwin ]]; then
    echo "Build the macOS app on a Mac." >&2
    exit 1
fi

# Dependencies are installed on the BUILD machine, never on an end user's Mac.
PYTHON="${PYTHON:-python3}"
VENV="${MACOS_BUILD_VENV:-$ROOT/.venv-macos}"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install -r requirements.txt
"$VENV/bin/python" -c 'import tkinter, CoreBluetooth, bleak.backends.corebluetooth.client'
"$VENV/bin/python" -m PyInstaller --clean --noconfirm --distpath dist/macos gc_controller_enabler.spec

APP="$ROOT/dist/macos/NSO-GameCube-Controller-Pairing-App.app"
"$VENV/bin/python" platform/macos/verify_bundle.py "$APP"
ARCH="$(uname -m)"
ZIP="$ROOT/dist/macos/NSO-GameCube-Controller-Pairing-App-macOS-$ARCH.zip"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

# Optional release notarization. Credentials remain in the builder's keychain.
if [[ -n "${MACOS_NOTARY_PROFILE:-}" ]]; then
    if [[ -z "${MACOS_CODESIGN_IDENTITY:-}" ]]; then
        echo "MACOS_NOTARY_PROFILE requires MACOS_CODESIGN_IDENTITY." >&2
        exit 1
    fi
    xcrun notarytool submit "$ZIP" --keychain-profile "$MACOS_NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
    xcrun stapler validate "$APP"
    /usr/bin/ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
else
    echo "Development artifact only: this build has not been notarized."
fi
printf '\nBuilt: %s\n' "$ZIP"
echo "Users copy the app to Applications; Python, pip, Bleak and Homebrew are not required."
echo "Current macOS game output remains Dolphin pipe / DSU, not system-wide virtual HID."
