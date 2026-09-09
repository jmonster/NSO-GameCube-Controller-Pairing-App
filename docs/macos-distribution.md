# macOS: one app, no end-user package installation

The distributable is a `.app` inside an architecture-labelled ZIP. Copy it to
Applications, launch it, grant Bluetooth access, then pair with the controller's
SYNC button. Python, pip, Bleak, PyObjC and libusb belong inside the app, not in a
user installation checklist. The source-development instructions in the main
README are for developers, not for people running this packaged build.

This packaging change does **not** add system-wide controller emulation. The
current Python Mac application still uses Dolphin pipes / DSU for game output.
A Bluetooth connection or working input visualizer is not proof a game can see
its controller. Switch 2 Pro/Joy-Con support is not added by bundling Bleak.

## Build and verify

On a Mac with Python 3.12+ (including Tk) and libusb available to the builder:

```sh
brew install libusb  # builder only
bash platform/macos/build.sh
```

The script resolves the repository root, uses an isolated `.venv-macos`, builds
an onedir bundle (no extraction on launch), copies it to a path containing spaces,
and runs its offline `--bundle-self-test`. The check imports the dynamically
loaded CoreBluetooth/PyObjC backend and UI dependencies, verifies module origins
stay inside the relocated bundle, and explicitly loads the bundled libusb.
It neither scans Bluetooth nor opens USB devices or a Tk window.

A sanitized PATH and Python/DYLD environment are used for this check. This catches
missing bundled dependencies; it is not a substitute for testing on a clean Mac
without developer software, testing GUI startup, or physical-controller tests.
The native architecture CI matrix builds Apple Silicon and Intel separately;
it does not label a single-architecture artifact as universal.

## Signed, low-friction releases

```sh
MACOS_CODESIGN_IDENTITY='Developer ID Application: YOUR IDENTITY' \
MACOS_NOTARY_PROFILE='YOUR_KEYCHAIN_PROFILE' \
  bash platform/macos/build.sh
```

PyInstaller signs nested binaries using that identity. The build then submits
the ZIP to Apple's notarization service, staples the app, and repackages it.
Without those variables the output is a development artifact, not a notarized
consumer release. No signing credentials or provisioning profiles are committed.
Verify Gatekeeper behavior on a downloaded/quarantined artifact before release;
do not tell users to disable SIP or security checks as the normal installation.

The restricted virtual-HID entitlement is a separate requirement of the planned
native controller-output path. Notarization, Bluetooth permission, and virtual-HID
authorization are three different things; this PR does not claim any approval.

## Why bundle Bleak rather than vendor/reimplement it?

Bundling resolves the end-user dependency immediately while keeping upstream
backend fixes available. Copying Bleak source into this repository still leaves
its Python/PyObjC runtime dependencies. Reimplementing a general Bluetooth
library in Python buys little. A future native Swift/CoreBluetooth product can
remove Python, PyObjC and Bleak from the runtime altogether without changing
Nintendo's wire protocol. Keep that migration separate from packaging the
currently working GameCube application.
