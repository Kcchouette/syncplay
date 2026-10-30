---
feature: player-modernization
status: delivered
updated: 2026-10-30
source: feat/player-modernization
---

# Player Integration Modernization (2026)

## Report

**What was built** — Syncplay's media-player adapters were modernised in eight
independently committed stages plus one review-fix commit, on top of
`49e2a1b` (master):

- **mpv family** (mpv, mpv.net, Memento, IINA — one shared adapter):
  status is now **event-driven JSON IPC** (`bind_property_observer` /
  `bind_event` on the vendored `python_mpv_jsonipc`, zero vendor changes).
  A lock-fed `MpvStatusCache` (`syncplay/players/mpv_status.py`) serves
  `askForStatus()` as a pure non-blocking read while preserving the
  load-transition gates (`isReadyForSend` early-return, file-loaded
  gating, `recentlyReset`). Log scraping of `print-text` `ANS_*` answers
  and `term-playing-msg` markers is gone. Chat and the options handshake
  travel as `client-message` events (`syncplay-chat` /
  `syncplay-intf-ready`); `syncplayintf.lua` no longer contains a single
  `print-text` call. Real-mpv E2E sessions drove three correctness
  fixes: a pre-load pause gate that stored the client's initial float
  `0.0` (making `setPaused(False)`'s guard swallow every unpause, leaving
  mpv frozen while positions were wall-clock extrapolation), a
  keep-open-EOF runaway (dead-reckoning now freezes at `eof-reached` and
  caps at duration), and a load-time re-read of `pause`/`time-pos`
  (observers are change-only). A load-generation guard aborts the
  file-load resolver when a newer load starts mid-resolve, and the
  options handshake hops off the vendor's single EventHandler thread.
- **VLC**: launch argv is built per instance (`buildVlcCall`), the
  control port is per-instance (was frozen at class definition), the Lua
  interface is hash-skip installed (`_ensureVlcInterface`), `vlc.net.close`
  nil-call fixed, `VLC_MIN_VERSION` raised to 3.0.0 with a gate that
  actually drops unsupported versions (floor + VLC-4 ceiling; the old
  branch referenced a non-existent message key and continued anyway).
  VLC 4 remains deliberately unsupported (Lua quits under major > 3);
  README states VLC 3.x only and recommends mpv.
- **mplayer2**: deprecated in place (docstring + README); dead macOS
  `/usr/bin/python` youtube-dl hack removed.
- **MPC-HC/BE**: `_thread.start_new_thread` replaced by a single
  `threading.Thread(daemon=True)` dispatch helper; dead command
  constants removed.
- **BasePlayer**: the five capability flags are declared and documented;
  `openCustomOpenDialog` is a plain instance method (the GUI's zero-arg
  instance calls would have rejected the old staticmethod-with-`self`).
- **Tests/CI**: a pure-logic suite (115 tests, no player binaries, GUI,
  or network) with an ubuntu unit-tests CI job and a `make test` target.

**Verification** —
- `PYTHONPATH=. python3 -m unittest discover -s tests -v` → **115 tests, OK (1 skipped: mpcbe needs pywin32)**.
- Import smoke via `getAvailablePlayers()` (MPC entries fall back to DummyPlayer on Linux, as designed).
- Real-player E2E on this machine: **mpv 0.41** — file-load/status/EOF (positions 0→8.010884 on an 8.023 s file, EOF pause reported honestly, no runaway), 2-client chat both directions with verbatim special characters, options handshake fires; **VLC 3.0.23** — version accepted, file loads, positions stay within duration, Lua interface hash-skip verified across two launches (mtime unchanged on the second).
- Neutral review of the full branch → APPROVE-WITH-FIXES (1 critical, 1 major, 2 minor) → fix commit `48a9980` → focused re-review → **APPROVE** (all four findings fixed, no new issues).

## [S1] Problem

See the investigation conclusions embedded in the commits: mpv-family
status came from log scraping against mpv's explicit "do not parse
terminal output" guidance despite a full JSON IPC being available; VLC's
launcher mutated shared constants and re-copied its Lua interface every
run; the VLC version gate warned-but-continued (via a message key that
raised KeyError); mplayer2 and MPC carried dead legacy code; the player
capability contract was undeclared; and the repository had no unit-test
gate.

## [S2] Design

Delivered as eight stage commits (T1–T8) plus one review-fix commit on
`feat/player-modernization`. The load-bearing invariants — preserved
verbatim from the pre-change behaviour and pinned by tests — are: the
`isReadyForSend` early-return in `askForStatus` (no status report during
file transitions), file-loaded gating of position/pause stores (with the
`MPV_NEWFILE_IGNORE_TIME` window), `recentlyReset` ownership on the
player, `_onFileUpdate` kept as the FILE_LOAD_DONE hook for IINA's
placeholder override, the `Exiting... (reason)` log parse (the mpv
`quit` event carries no reason), and the load-failure `readyToSend` fast
path. Threading contract: vendor callbacks (single EventHandler thread)
only touch the lock-guarded cache and hop to the reactor for
client-facing work; blocking `get_property` resolution runs on a
short-lived worker with bounded waits and a load-generation guard.

## [S3] Out of Scope

- VLC 4 support (any form), VLC HTTP interface, reviving `origin/VLC4`.
- Rewriting `client.py` / the server protocol / replacing Twisted.
- Modifying the vendored `python_mpv_jsonipc`.
- Deleting mplayer2 or finishing MPC feature work (Windows-only smoke
  remains manual).
- GUI `setDaemon` sweep; packaging/installers.

## Tasks

- [x] T1: mpv/mpvnet/memento/mpcbe quick fixes — argv idempotent, playerArgs not polluted, imports clean (covers: S2.1)
- [x] T2: extract `mpv_status.py` + `build_mpv_args` + 3.8-safe tests (covers: S2.2) (depends: T1)
- [x] T3: event-driven `MpvPlayer` wiring + `iina.py` (covers: S2.3) (depends: T2)
- [x] T4: slim `syncplayintf.lua` + chat/handshake via client-message (covers: S2.4) (depends: T3)
- [x] T5: VLC 3-only cleanup (8 fixes, drop-gate, docs, tests) (covers: S2.5) (depends: T3 for constants.py ordering)
- [x] T6: mplayer deprecation + MPC threading hygiene (covers: S2.6)
- [x] T7: BasePlayer capability flags + signature-safe `openCustomOpenDialog` (covers: S2.7)
- [x] T8: test-suite gate — CI job + `make test` (covers: S2.8) (depends: T2–T7)
- [x] T9 (review-fix): handshake reactor hop, load-generation guard, drop dead Signal.QUIT, test hardening — re-review APPROVE

## Journey log

- Investigation (3 explore agents + web): Syncplay never used `cvlc`; VLC
  is a custom Lua-intf TCP protocol; VLC 4 is hard-incompatible (stable
  was still 3.0.24 in Oct 2026); `origin/VLC4` is dead (+4/−483, 2022);
  mpv was half-modern (IPC writes + log-scraping reads); the vendored IPC
  lib already exposed the observers the redesign needed.
- Plan validated by two independent neutral reviewers before approval
  (APPROVE-WITH-AMENDMENTS both; all blockers/majors folded in).
- Real-mpv E2E exposed a pause deadlock the unit tests could not see:
  the pre-load gate stored the client's initial `_globalPaused = 0.0`
  (float), and `0.0 == False` made `setPaused(False)`'s early-return
  swallow every unpause, so mpv never played while status reports were
  pure wall-clock extrapolation. Fixed by bool-normalising the gate
  fallback and re-reading pause/time-pos at load.
- Real-mpv E2E also exposed the keep-open-EOF runaway (time-pos freezes
  while the adapter believed playback continued) — fixed with an
  eof-reached freeze plus a duration cap, and paused/eof states no
  longer count as "unresponsive" (IPC death is the socket callback's job).
- The VLC version gate turned out to reference a message key that does
  not exist (`vlc-version-mismatch` → KeyError on every old-VLC
  connection); the fix uses the existing translated
  `vlc-failed-versioncheck` string and adds an explicit drop.
- Branch review found the options handshake blocking the vendor
  EventHandler thread (fixed with a reactor hop) and a file-load worker
  race (fixed with a load-generation token); focused re-review approved.
