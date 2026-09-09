# BLE stdout delivery: compatibility boundary

This change only queues the existing helper-to-parent stream. It does not change
SW2 pairing/encryption, MTU requests, GATT handles, initialization commands,
rumble/LED payloads, input decoding or controller connection/readiness deadlines.
The binary format remains `0xFF + slot + 64 bytes`; the existing truncation and
zero-padding from controller reports is preserved. JSON serialization is unchanged.
Both message kinds share one ordered writer. There is no batching delay or
latest-only/drop-oldest policy, and queued mutable packets become byte snapshots.

Outstanding output (including an in-flight write) is limited to 512 messages and
128 KiB. A full queue, broken/zero write, or five seconds without write progress
fails the helper; an idle pipe with nothing pending never times out. Partial and
interrupted writes finish the current message before starting the next. Failure
wakes the command task out-of-band, attempts backend cleanup, and exits nonzero.
The parent then uses the helper-loss handling from PR #2. No restart is automatic.

Shutdown attempts task cancellation (0.5 s), backend close (2 s), and output drain
(1 s). A blocked raw OS write may remain in a daemon thread until process exit;
only that writer closes its duplicated fd, avoiding descriptor-reuse races.
This is not a general thread-cancellation mechanism or a BlueZ recovery fix.

Tests cover exact legacy bytes, all byte values and slots, short/interrupted
writes, immutable snapshots, message/byte limits, idle vs stalled pipes, and both
actual helper entry points with fake radios. A real filled OS pipe verifies
bounded failure. The existing parent parser reads unchanged press/release frames.
No hardware, handshake capture, physical latency or frozen-app acceptance is
claimed: queueing can affect scheduling, and sustained host stalls now cause an
explicit disconnect. Before release, test pairing/reconnect, rapid input and
rumble with real controllers on each claimed platform, plus helper failure while
an unrelated USB controller remains active. Parent command-write blocking,
backend session races and same-helper slot reuse remain separate work.
