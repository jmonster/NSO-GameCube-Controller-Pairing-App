# BLE helper loss: behavior and validation

This change is based on merged main `d74eb1f9a6de64e4bda9d0418b23cc598989f31f`.
It does not reopen PR #1 or restore the expanded stabilization archive.

## Failure policy

Unexpected helper stdout EOF, a read/framing error, or an exhausted controller
input queue invalidates that entire helper session. Input insertion is
nonblocking. Overflow is **not** a latest-report policy: it explicitly stops the
BLE session rather than silently throwing away a possible button release.
All BLE outputs still owned by that helper are stopped, reset, flushed and
closed. Each output teardown operation is attempted independently. USB and
outputs owned by a replacement helper are not retired.

Readers capture a session containing the actual process. Initialization uses
that session's ordered mailbox; termination wakes its waiters and cancels a
headless pipe-open wait. Queued GUI events, initialization completion, BLE
scan/retry timers, rumble and pipe-completion callbacks check ownership again
when they execute. GUI loss handling takes priority over queued status events.
The headless event loop applies the same session check and failure policy.

Loss handling and process cleanup are each claimed once. Deliberate shutdown
marks the session ended before closing stdin, so the resulting EOF is not an
unexpected-loss notification. Stdin EOF requests the helpers' existing backend
cleanup; wait/terminate/kill are attempted in order. Failed cleanup is logged.
There is no helper auto-restart or repeated privilege-prompt loop. Starting a
new BLE helper after failure requires explicit user action (or restarting the
headless application). Controller reconnect while its helper remains healthy
retains the existing behavior.

Output updates and retirement share a lock. A pending output creation is
invalidated by Stop, and its late result is closed without replacing newer
output. This small guard is needed so helper-loss cleanup cannot be undone by
a pipe creation already in progress; it is not a new runtime architecture.

## Automated evidence

Run `python -m unittest discover -s tests -v` and
`python -m compileall -q src`. The suite uses standard-library hardware/UI
fakes, not real Bluetooth. The existing 80 tests are retained; 35 focused tests
exercise production app methods, headless closures, emulation and legacy IPC.
A real local Python subprocess with OS pipes covers ready followed by exit.

Twenty-two selected behavioral tests were also run with `app.py`,
`controller_slot.py` and `emulation_manager.py` restored to the exact merged-main
versions. All twenty-two failed assertions (28 failures including subtests),
then passed with the fix. The new parser/session module remained available as
a test fixture; the baseline application did not import or execute it. These
failures included non-neutral output after EOF, a blocked GUI reader, silently
dropped headless overflow, stale callbacks reaching replacement state, an init
waiter timing out, omitted neutralization and output creation undoing Stop.
Additional tests cover framing fragmentation, malformed traffic, exactly-once
handling, normal shutdown, error isolation and unrelated-controller preservation.
No new skips, dependencies, CI workflows or packaging changes are required.

## Scope, provenance and remaining limits

The legacy `0xFF + slot + 64-byte payload` / JSON-line IPC is unchanged.
The 64 KiB JSON bound and short-read parser are adapted from archived commit
`fb0ba87c3b8cb67e4548d1943554b3302b7d5cdc` (OpenAI), together with its
process-ownership/neutralization approach. The archive's fork-attribution notes
were reviewed; no other fork patch or shared-runtime component was imported.

Process ownership does **not** distinguish buffered slot reuse within the same
helper. That remains a separate protocol/session issue. Child stdout
backpressure/partial writes, parent command-write blocking, backend cancellation,
readiness validation, persistence and platform restoration are not solved here.
UI/headless dispatch and backend calls are not hard real-time; a stalled native
output backend can still delay cleanup. Reset/flush/close are best-effort when a
backend itself fails, and that failure is logged rather than claimed successful.

## Required hardware acceptance — not performed here

Record OS, controller firmware and output backend for GUI and headless runs.
With a BLE controller holding buttons, sticks and triggers away from neutral,
terminate its helper: the game/emulator must return to neutral and the slot
must retire with an explicit error, without another privilege prompt. Repeat
with an unrelated USB controller active and confirm its input continues.

Manually start a new BLE session after loss and verify input and rumble work
without delayed old-session disconnects or feedback. Kill the helper during
initialization and while headless Dolphin pipe creation waits for a reader;
both waits must end. Quit normally and verify no duplicate failure notification.
Physical gameplay, interactive permission prompts, sleep/wake, four-controller
operation, driver restoration, signing and notarization are not qualified by
these tests. Merge readiness is not hardware-qualified release status.
