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

## Checklist implementation follow-up

The following code paths now have regression coverage. These are implementation
checks, not evidence of successful physical-controller or signed-app testing.

| Checklist item | Implementation and automated coverage |
| --- | --- |
| Pending output creation versus Stop/Quit | Emulation managers reserve a generation before calling a factory, cancel pending creation, reject stale publication/callbacks and dispose late results. GUI callbacks retain the owning slot and cancellation token. Headless output creation uses a cancellable worker instead of blocking BLE event handling while Dolphin is absent. |
| Parent command-pipe blocking | A dedicated ordered writer owns each process pipe. Producers never block on OS writes; the queue is bounded to 128 frames / 64 KiB, with a two-second no-progress deadline. Failure retires only the owning child and schedules bounded process reaping. Real subprocess tests fill a pipe and exercise EOF. |
| Buffered events across slot reuse | IPC version 2 carries a positive 64-bit generation on binary reports and slot-scoped JSON in both directions. Independent wire-slot allocation avoids conflating player reassignment with child slots. Parent callbacks revalidate ownership when dispatched, not just when read. Up to 256 initial reports are buffered in order until the consumer is bound; overflow is a service failure. Real child round-trip tests include reconnect and stale-generation events. |
| USB initialization loops | Startup, auto-connect and headless paths initialize only the verified device associated with the selected HID path. Explicit USB initialization requires a device argument or established binding. Slot migration transfers the complete HID/feedback identity. USB-to-BLE migration requires an explicit saved device link, not arrival timing. |
| Linux Bluetooth takeover/restoration | A single privileged lease snapshots the service and selected adapter, validates the adapter index, and checks every bounded command. Only the selected adapter is powered down. Cleanup restores the states this lease changed and reports failures; repeated restoration is safe. Mocked service/adapter failures and a real POSIX SIGTERM child test cover cleanup. |
| Settings schema/value validation | Bounded UTF-8 JSON parsing rejects duplicate keys, non-finite values, invalid versions/types/ranges, malformed calibration, ambiguous normalized identifiers and invalid slots/links. Entire payloads validate before live mutation. Bad or future-format files remain intact and block automatic saving until a valid reload; source legacy migration applies the same validation. |
| UI dispatcher memory growth and shutdown | The queue has a 1,024-callback count limit and a per-tick work limit. Overflow rejects further posts, discards stale work and invokes fail-safe shutdown on the Tk owner thread. Quit cancels pending starts, neutralizes outputs before reader waits, and attempts subsequent cleanup even when a callback or resource fails. |
| Windows windowed subprocess streams | Missing Python standard streams are restored from duplicated inherited pipe handles. Required IPC never substitutes null streams for missing input/output. Binary mode preserves all 256 byte values. Tests run a real child and native pythonw on Windows. |

### Compatibility and failure policy

The parent and child must be upgraded together: protocol v2 deliberately rejects
legacy input frames instead of interpreting them as a different session. A slot
number alone is never authorization to deliver old input or feedback. Queue
limits preserve bounded resource usage, not guaranteed delivery through a
crashed or stalled process; exceeding them causes an explicit disconnect and
neutralization rather than silently dropping a release. The dispatcher limit
bounds callback count, not the byte size of arbitrary Python closures.

Linux cleanup covers normal shutdown, EOF, handled failures and SIGTERM. It
cannot execute after SIGKILL, kernel failure or power loss. The service remains
inactive if it was inactive before takeover; an initially off adapter stays off.
The lease prevents two helpers from simultaneously owning restoration. Actual
systemd/BlueZ adapter behavior, privilege-policy interactions and recovery after
forced termination still require hardware/OS acceptance tests.

Strict settings validation is intentionally not a silent repair mechanism.
Rejected files are preserved and automatic saves are blocked, avoiding the loss
of unknown future-format settings or invalid calibration. Repair the retained
file or restore a known-good backup, then reload/restart. Atomic replacement
protects against partial normal writes but is not a universal power-loss or
hostile-filesystem guarantee.

## Frozen application smoke checks

After building with the native hash lock validated against `pyproject.toml`, run the actual executable, not an import of the source checkout:

```sh
# macOS
python tools/check_frozen.py dist/NSO-GameCube-Controller-Pairing-App.app/Contents/MacOS/NSO-GameCube-Controller-Pairing-App frozen-smoke.json
# Windows
python tools/check_frozen.py dist/NSO-GameCube-Controller-Pairing-App.exe frozen-smoke.json
```

The PR workflow builds Linux, macOS and Windows packages using Python 3.12 and runs the
same diagnostic. It launches outside the build tree with an isolated home and
without Python/library-path overrides; checks real backend imports, Tcl data,
font/image resources, macOS bundled libusb and privacy declaration, and the
Windows ViGEm client DLL; then exchanges every byte value through redirected
pipes. It also starts the actual Bleak child for protocol-ready, open, shutdown
and EOF, without scanning Bluetooth. Build diagnostics retain the resolved pip
environment and warnings. The workflow neither installs the ViGEm driver nor
signs, notarizes or releases an application.

A successful smoke check does not establish GUI accessibility, OS privacy-prompt
behavior, HID permissions, a working controller, or a clean-machine release.
CI covers Linux x86_64, macOS arm64 and Windows x86_64 on Python 3.12. Other
architectures/Python build versions fail without a matching reviewed lock.
Python dependencies and build tools are hash locked; the OS image, compiler,
SDK and system libusb are not. This is not a bit-reproducible-build claim.

## USB scheduling and recovery implementation

The GUI now uses a single discovery worker and at most four session workers.
Enumeration, initialization, HID open/close, rumble and player LEDs do not run on
the Tk thread. Cancelling an open immediately invalidates its UI result; the
native worker retains its path/budget until cleanup finishes. Reconnection does
not substitute an unrelated controller. Wire feedback holds only the most recent
rumble and LED state, not an unbounded queue of obsolete effects. This coalescing
applies to feedback only, never controller input/button transitions.

A stuck native HID/libusb call cannot be forcibly cancelled safely in a Python
thread. The worker count stays bounded and GUI shutdown waits only a shared
budget, but a native operation still needs to return for full cleanup. Real
unplug/sleep/wake tests remain required. Headless polling still uses synchronous
USB calls and is not described as a nonblocking event-loop implementation.

Linux management commands use validated root-owned executables from fixed system
directories, a restricted environment, no interactive stdin, a fixed working
directory and timeouts. The root-owned 0700 runtime directory holds a bounded,
0600 recovery journal published before service/adapter mutations. Under the
exclusive lease, a new helper first restores an interrupted record, including a
previously selected different adapter. Malformed/unsafe journals stop takeover;
restoration failures retain ownership and the record rather than claiming success.

This recovers interrupted state on the NEXT helper start, not immediately after
SIGKILL. /run is ephemeral across boots. It does not replace testing systemd,
BlueZ, authentication policy and actual adapter behavior. Elevated source runs
now use isolated Python and a fixed package root; they no longer accept a supplied
import-path list. Source mode requires dependencies installed into that Python
interpreter/venv. The authorized program/checkout and its dependencies still run
with elevated privileges: a separately installed root-owned minimal broker is
not implemented, and untrusted source trees must not be authorized with pkexec.

## Dependency locks and release verification

`requirements/locks` contains native Python 3.12 resolutions for Linux x86_64,
macOS arm64 and Windows x86_64. Both runtime dependencies and the pip/setuptools/
wheel/packaging/PyInstaller build tools are version- and archive-hash-pinned.
The fixed vgamepad revision is fetched as a hash-verifiable source archive, not
a moving branch or unhashed Git checkout. Bootstrap tools install first; source
build isolation is disabled so pip cannot silently fetch unlocked build tools.

```sh
python tools/dependency_lock.py install --venv .build-venv
# Use .build-venv/bin/python on POSIX or .build-venv/Scripts/python.exe on Windows.
```

The installer requires a fresh environment and matching manifest fingerprint;
missing/mismatched locks do not fall back to latest versions. Regeneration is
explicit through the manual candidate workflow or `generate` command, followed
by review of versions, archive hashes and a clean native installation. Lock file
line endings are fixed so Windows checkouts preserve verified bytes. The initial
candidates were generated and clean-installed in run 34287249584.

PR package tests and release builds share the same locked reusable workflow.
Each platform builds and smoke-tests the actual executable, preserves package
permissions/symlinks, and records source commit, dependency manifest digest,
resolved versions, smoke result and archive checksum in a sidecar. These JSON
sidecars are UNSIGNED integrity records, not proofs of a trustworthy publisher.
The release gate requires exactly the three expected archives and sidecars,
checks them against the expected source and committed locks, and generates
SHA256SUMS. A tag-only step then creates GitHub/Sigstore attestations and verifies
signatures against the exact repository, release workflow, source SHA and ref
before publishing. No tag or actual signing/publishing run is performed as part
of this PR; those event-specific steps still require a controlled release test.
Developer ID signing/notarization of the macOS app is a separate requirement.

References: pip secure installs and no-build-isolation documentation; polkit
pkexec security notes; actions/attest v4.2.2 README; GitHub CLI attestation verify.

## Remaining stabilization and release gates

- Exercise the manual hardware matrix above, particularly unplug/sleep during
  worker operations, four controllers, driver reattachment and emulator gameplay.
- Verify interactive GUI behavior and packaged macOS privacy approval, denial,
  revocation, minimized startup and quit on clean supported machines.
- Test actual interrupted BlueZ recovery and root authorization; consider a
  separately installed least-privilege broker before widening Linux deployment.
- Run the tag-specific attestation/verification/publish gate deliberately after
  hardware acceptance. OS/SDK/compiler pinning, additional architecture locks,
  Developer ID signing, hardened runtime and notarization remain separate work.

There is no native CoreHID backend or iOS/tvOS port. Automated concurrency tests,
locked installations and package smoke checks do not prove every hardware race.
