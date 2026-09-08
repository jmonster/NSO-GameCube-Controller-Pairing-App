# Stabilization regression and hardware validation

## Automated checks

Run from the repository root with Python 3.12 or 3.13:

```sh
python -m unittest discover -s tests -v
python -m compileall -q src
```

The isolated suite uses the standard library only. It does not require HID,
libusb, a Bluetooth adapter, CustomTkinter, Dolphin, or a display server. The
input processor and connection manager are loaded from production source with
explicit hardware/dependency fakes. GUI entry points and Dolphin path helpers
are extracted from the production AST to avoid unrelated application startup.
This verifies the changed logic, not optional-library integration or packaging.

Coverage includes ordered USB/BLE reports, malformed-frame rejection, reader
shutdown/restart, persistent BLE slot assignments, minimized startup without a
tray backend, the macOS Bluetooth purpose string, first-run Dolphin paths and
FIFO validation, USB transfer cleanup, and device-specific rumble/player LEDs.

The workflow runs these checks on Linux, macOS, and Windows with Python 3.12 and
3.13. POSIX FIFO and symlink tests are skipped where the necessary filesystem
operations are unavailable or require special privileges. Passing the isolated
suite is not evidence that a controller or packaged application works.

## Compatibility decisions in this batch

USB feedback now requires a verified association with the opened HID device.
On macOS it uses the HID registry ancestry and both USB bus and device address.
On Linux it resolves hidraw through sysfs. A unique, readable serial number can
provide a fallback. There is deliberately no first-device or bus-only fallback.
If identity is unavailable or ambiguous, HID input remains usable but USB rumble
and player LEDs return false. The log records the missing association.

Initialization no longer resets an already configured USB device. Permission,
configuration, and claim errors terminate that operation instead of being
silently treated as success. Interface ownership and handles are released on
failure as well as success. A macOS driver detached by an operation is restored
on cleanup. Driver attachment/detachment must be exercised on real hardware.

Every queued input report is now offered to the existing input processor in
order. This prevents the reader from deleting a complete button press/release
by keeping only the newest report. It does not establish end-to-end latency or
guarantee that a polling game observes arbitrarily fast transitions. The
existing half-second startup warmup remains unchanged to avoid removing the
BLE-to-USB phantom-input safeguard without hardware evidence.

Dolphin's explicit user-directory override is authoritative, including before
that directory exists. On macOS the default is Application Support, even on
first run. FIFO destinations cannot be path-traversing names, symlinks, or
existing regular files. This is not a complete defense against a concurrently
hostile process replacing paths after validation.

## Required hardware/release gate — not automated here

Record OS version, architecture, controller firmware, transport, output backend,
and pass/fail details for each run. Preserve logs without publishing controller
addresses or serial numbers unnecessarily.

| Area | Required scenario | Success criterion |
| --- | --- | --- |
| macOS privacy | Fresh packaged app; approve, deny, revoke, and retry Bluetooth permission | Correct system prompt and actionable failure; no crash or false ready state |
| Multiplayer | Two identical USB controllers on one hub, then four controllers; reverse connection order | Each slot's LEDs and rumble affect only its own controller |
| USB lifecycle | Unplug during initialization/rumble; reconnect; suspend/resume | No leaked ownership; no command sent to a replacement device; input resumes |
| Feedback identity | Capture actual IOKit and libusb identities on supported macOS versions | Exact device association, or explicitly disabled feedback; never a bus-only guess |
| Input | Fast taps, simultaneous buttons, analog triggers and trigger clicks; CPU/output contention | No reader-induced loss of ordered transitions; bounded recovery after stalls |
| Window lifecycle | Start minimized, restore via Dock/taskbar, hide/show, and quit | The window remains recoverable and the process exits cleanly |
| Dolphin | No existing profile; custom user directory; reader exit and restart | Correct profile location and no collateral file overwrite; reconnection behavior verified |
| Distribution | Clean Macs without Homebrew; each supported architecture | Complete native dependencies, functional Bluetooth, and successful signing/notarization checks |

## Remaining stabilization work

This batch does not fix BLE readiness validation, stale BLE callback ownership,
subprocess output backpressure, cancellation/EOF cleanup, or output neutralization
on every failure path. It also does not eliminate all application-level loops
that initialize multiple USB devices during hotplug/reconnect. Those remain
separate high-priority changes requiring full integration review and tests.

There is no native CoreHID backend, iOS/tvOS port, new signing identity, or
notarization automation in this batch.
