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
FIFO validation, USB transfer cleanup, and device-specific rumble/player LEDs. Follow-up coverage includes shared
BLE child lifecycle, bounded output, partial writes, EOF/malformed IPC, owned
callbacks, ready-input gates, targeted reconnects, settings publication failure,
Tk worker dispatch, scan-response updates, Dolphin digital clicks and DSU
packet/subscription validation. A real loopback UDP test checks malformed-traffic
survival and per-slot streaming; Bluetooth and USB devices remain faked.

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

## BLE and persistence behavior in the follow-up batch

Both child entry points use one command/session runtime. A successful GATT write
is not sufficient to become ready: Bleak requires the documented service layout,
input subscription and a 63-byte input report on the expected input channel;
Bumble requires successful initialization plus input. This validates framing and
readiness, not cryptographic authenticity or every proprietary response field.
Connection attempts are bounded, cancelled tasks are awaited, and stale client
callbacks do not remove a replacement client's state. Exact GATT layout and
five-second ready-input deadlines need firmware/hardware validation.

The child output queue is ordered and bounded. It handles partial/interrupted
writes on a dedicated thread. Overflow is a transport failure, not permission to
throw away a release event. Parent readers reject partial frames and retire BLE
outputs on EOF or protocol loss. USB slots are left alone. A service failure
requires a deliberate retry rather than an automatic privilege-prompt loop.
This does not guarantee delivery after an OS/process failure or through a game
that polls more slowly than a press/release transition.

Both source and frozen applications use the platform settings directory. Source
runs can migrate a bounded valid UTF-8 JSON settings file from their old working
directory when no platform file exists. The old file is retained. Migration
requires exclusive hard-link publication; unsupported filesystems log the
failure and keep the legacy file intact. Normal saves serialize before writing
and atomically replace a same-directory flushed/fsynced temporary file. This is
not a guarantee against arbitrary filesystem corruption or all power-loss cases.

DSU retains loopback-only binding, uses non-blocking sends, validates framing and
CRC, limits expiring subscriptions and respects requested slots/MACs. UDP remains
lossy and unauthenticated; do not expose it to an untrusted network. DNS/network
access is not used in these tests; the UDP smoke test stays on loopback.

Additional manual gates: cancel during scan/connect/pairing/init; kill the child
while holding a button; stall parent output; reconnect after sleep; move a BLE
controller between UI player slots and check rumble/disconnect ownership; test
multiple WinRT connections; verify Bumble public host identity; and compare
Dolphin partial trigger travel with digital clicks. Confirm denial/revocation
of Bluetooth permission in the actual `.app`, not only source execution.

## Remaining stabilization work

The new failure handling is not a complete proof of all application lifetimes.
Review pending virtual-gamepad creation versus Stop/Quit, UI/subprocess slot
allocation across every reconnect path, parent command-pipe blocking, and
application-level USB initialization loops. Strict settings value/schema
validation, dependency lockfiles, full frozen-build smoke tests, signing and
notarization remain outstanding. Linux BlueZ restoration and real USB driver
reattachment still need release-level review and hardware testing.

The BLE IPC format has bounded validation and child/process ownership but no
wire-level generation identifier; already-buffered events across every possible
same-process slot reuse need additional end-to-end testing. No claim is made
that every race or output failure is resolved.

There is no native CoreHID backend, iOS/tvOS port, new signing identity, or
notarization automation in this batch.
