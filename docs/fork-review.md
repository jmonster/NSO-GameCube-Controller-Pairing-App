# Fork review and stabilization ports

Snapshot: September 8, 2026. The review inventory covers 13 repositories and
81 branches: the upstream repository, its recursively listed public forks,
the target repository, and the renamed/detached `MarcanBat2a/gamecube-remote-mac`
repository. All listed branches were fetched without checking out or executing
fork code. The inventory collection reported no errors. Exact branch heads are
retained in `fork-review-inventory.json`.

Reviewing a fork does not establish hardware compatibility. Unique histories and
patches were compared with the target base
`8473b3e021dd6ebc72f8cf5796f5c1381f7f5868`. Changes already inherited, unrelated
features, conflicting optimizations and unvalidated protocol rewrites were not
blindly merged. Adapted ports identify their sources in commit messages and below.

## Adopted or adapted

| Source | Upstream change | Integration decision |
| --- | --- | --- |
| [jaredrbrick `5ce342a`](https://github.com/jaredrbrick/NSO-GameCube-Controller-Pairing-App/commit/5ce342a91b1eb5554a5ab4a3e96769a68cc5136d) | `scan_only()` called `.decode()` on already decoded names, then swallowed the exception | Cherry-picked with original authorship; added string/bytes, exclusion, repeated scan and cancellation tests. |
| [jaredrbrick `9324bae`](https://github.com/jaredrbrick/NSO-GameCube-Controller-Pairing-App/commit/9324bae00386c81d8cc70de0ff584b08a30378c2), following `07fb7ef` | Additional observed GC Bluetooth prefixes | Added E0:EF:BF and 94:8E:6D to transport and picker detection. Did not import superseded unrelated controller prefixes. |
| [jaredrbrick `09ca5b1`](https://github.com/jaredrbrick/NSO-GameCube-Controller-Pairing-App/commit/09ca5b12531369dc6fd831f9dcea5cd266e79448) | Public initiator address must match the proprietary pairing identity | Adapted for Bumble; explicitly reject unavailable/zero/invalid public addresses instead of registering a random or invented address while connecting publicly. |
| [HotTownJohnny `4bff55e`](https://github.com/HotTownJohnny/NSO-GameCube-Controller-Pairing-App/commit/4bff55e54135cb799868de249f7f1cd4ea42b177) | Dolphin digital L/R clicks | Adapted across constants, emulation and FIFO output. Digital clicks remain separate from analog travel. No unsupported Dolphin ZL token is emitted; Xbox mapping is unchanged. |
| [dennis-michaelis `29fb22d`](https://github.com/dennis-michaelis/NSO-GameCube-Controller-Pairing-App/commit/29fb22d20ce2d7ff9b2eb8fbb0c3fb55d969f527) | Worker callbacks must not call Tk APIs | Added a main-thread dispatcher with a per-tick work cap, close/discard behavior and exception isolation. Kept Darwin pystray disabled; did not port private tray calls. |
| [pookee `7a33337`](https://github.com/pookee/NSO-GameCube-Controller-Pairing-App/commit/7a33337ef7214d6901d9fb524f67b944dc505ade) | Development and packaged runs silently used different settings | Unified platform directory, with source-run-only legacy migration. Added bounded UTF-8 JSON checks, no-clobber migration, retained originals and atomic saves. |
| [pookee `1b48887`](https://github.com/pookee/NSO-GameCube-Controller-Pairing-App/commit/1b4888728a59bf762ec3f6017db3188c528d8c1d) | WinRT preferred-parameter request lifetime; non-blocking DSU sends and subscriber pruning | Retain/release WinRT requests, but withdraw throughput preferences for multiple controllers. Adapted non-blocking DSU and scheduled pruning; additionally validate packets, filter per-slot/MAC subscriptions, bound subscription storage, survive UDP client reset and clean up failed starts. |
| [RyanCopley `c107280`](https://github.com/RyanCopley/NSO-GameCube-Controller-Pairing-App/commit/c107280ed76948c8eef352f7a21497870608fbc8), following `2171474` | Manufacturer/service-based BLE discovery | Selectively adopted the discovery approach. Nintendo's assigned Bluetooth company ID is 0x0553. Do not use the proposed 0x037E/0x057E Bluetooth IDs: those identify other companies. The USB VID remains 0x057e. |
| [gtorresini `025e5ec`](https://github.com/gtorresini/NSO-GameCube-Controller-Pairing-App/commit/025e5ec4f2072f5126cd0245c3c2f8e3ff78ed61), [`ecd66b6`](https://github.com/gtorresini/NSO-GameCube-Controller-Pairing-App/commit/ecd66b65eaf78b54371fbfbe1bd8393f534cd58a) | Consistent host identity, bounded connection attempts, and initialization disconnects stalling auto-scan | Addressed the same failure classes in the shared child runtime and backend ownership cleanup, rather than copying overlapping task-management code. Do not skip SMP simply because a target was supplied, or fall through a targeted reconnect to a different controller. |

## Per-repository disposition and deferred changes

| Repository | Disposition |
| --- | --- |
| RyanCopley/NSO-GameCube-Controller-Pairing-App | Main is inherited. Reviewed non-default mission-control, Pro Controller and Qt branches. Selective discovery port above; defer pairing crypto, report/command-channel and factory-calibration rewrite until captured exchanges and hardware tests establish correctness. A challenge verifier that accepts when crypto support is missing is not adopted. Pro Controller/Qt are scope-expanding features, not stabilization prerequisites. |
| MarcanBat2a/gamecube-remote-mac | Reviewed the two unique refactor commits. Do not import mac-only packaging/UI restructuring wholesale into a cross-platform app, unconditional address-discard behavior, or unrelated agent/workspace content. |
| mantoangabe/NSO-GameCube-Controller-Pairing-App | No unique branch commits relative to the target snapshot. |
| devin-thomas/NSO-GameCube-Controller-Pairing-App | No unique branch commits relative to the target snapshot. |
| sebastiaanvandingenen/NSO-GameCube-Controller-Pairing-App | Both captured branches point to the target base; already inherited. |
| jmonster/NSO-GameCube-Controller-Pairing-App | Target base and active stabilization branch; do not re-import our own work. |
| julio9873-commits/NSO-GameCube-Controller-Pairing-App | Unique default-branch change is a controller asset update; not needed for stabilization. Other experimental branches overlap upstream. |
| HotTownJohnny/NSO-GameCube-Controller-Pairing-App | Digital trigger port above. Remaining branch histories overlap upstream. |
| dennis-michaelis/NSO-GameCube-Controller-Pairing-App | Tk dispatch port above. |
| jaredrbrick/NSO-GameCube-Controller-Pairing-App | Three relevant fix branches adopted as described above. |
| gtorresini/NSO-GameCube-Controller-Pairing-App | Reconnect failure classes addressed above. Defer unrelated ignore-file changes. Reject target-to-blind-scan fallback and target-presence-as-bond-proof heuristic. |
| mjd4219/NSO-GameCube-Controller-Pairing-App-Windows | Compared Windows, single-instance/logging and ZL branches. Tray RGBA materialization, WinRT bundling/STA handling and a single-instance mechanism are already present in the target. Do not replace newer macOS onedir/libusb packaging with the fork's older packaging. Optional ZL-to-disconnect is a behavioral feature and remains deferred; always-on logging is not imported. |
| pookee/NSO-GameCube-Controller-Pairing-App | Settings, WinRT lifetime and DSU ports above. Reject drop-oldest/latest-only input and removal of framing checks: they conflict with preserving digital transitions. Defer renderer/cache optimizations and autostart-state caching pending focused tests; cached preferences are not proof of actual OS registration. Defer one-click emulator configuration and its follow-up mappings/dialog work to a separate feature review with backups and conflict handling for external user configuration. |

## References used to validate selective ports

* [Bluetooth SIG Assigned Numbers, section 7](https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Assigned_Numbers/out/en/index-en.html): 0x0553 Nintendo; 0x037E lulabytes; 0x057E Beco. Company IDs are not USB vendor IDs.
* [Bleak client API](https://bleak.readthedocs.io/en/latest/api/client.html): characteristic objects/handles avoid ambiguity when UUIDs repeat; writes use explicit response mode.
* [Microsoft preferred connection parameters](https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.bluetoothledevice.requestpreferredconnectionparameters): throughput preferences can reduce the number of concurrent connections; keep/release the request object deliberately.
* [Dolphin pipe token implementation](https://github.com/dolphin-emu/dolphin/blob/master/Source/Core/InputCommon/ControllerInterface/Pipes/Pipes.cpp): digital L/R and analog L/R are separate commands; no ZL token.
* [Cemuhook protocol reference](https://v1993.github.io/cemuhook-protocol/): declared length, CRC, client subscription flags and request layouts.
* [Bumble advertisement usage](https://github.com/google/bumble/blob/53dd584cede9f0db6f867ec6acc088c582b7f21f/apps/auracast.py): manufacturer data is normally a decoded `(company_id, bytes)` tuple, not a raw byte string.

None of these sources substitutes for testing this exact controller firmware,
radio, driver and packaged application on supported operating systems.
