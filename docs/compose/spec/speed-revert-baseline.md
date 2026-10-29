---
feature: speed-revert-baseline
status: delivered
updated: 2026-07-07
source: fix/speed-revert-baseline
---

# Speed Revert Baseline + Internal Speed Option

## Report

**What was built** — Syncplay's desync slowdown no longer resets playback to a hardcoded 1.0×: the client captures the player's actual speed before slowing down (`BasePlayer.getSpeed()`, per-player: synchronous mpv IPC, bounded Event ask for mplayer, poll-cached Lua `rate:` for VLC) and restores that baseline on revert, falling back through the new internal `speed` config/CLI/GUI option (default 1.0) when the live read is unavailable. The slowdown itself is now relative (`baseline × 0.95`). The internal option also serves as a portable speed entry channel: applied at `initPlayer` and re-applied on `updateFile` via `_applyConfiguredSpeed()` (guards: `speedSupported`, `!= 1.0`, `not _speedChanged`), so it works uniformly across mpv/mplayer/VLC without per-player argument dialects while leaving existing player-arguments usage untouched.

**Verification** — `python3 -m py_compile` on all 9 touched .py files: PASS. `luac -p syncplay/resources/lua/intf/syncplay.lua`: PASS. `python3 -m unittest discover -s tests -v`: PASS (21 tests, including revert→1.21, config fallback chain, application guards, mplayer/VLC parsing, `rate: nil` safety). Mutation check: reintroducing `setSpeed(1.00)` fails `test_revert_restores_captured_baseline` with `1.0 != 1.21` (and stripping the review guard fails `test_never_applies_while_slowdown_active`); restored → PASS. Config checks: INI round-trip, legacy INI without the key, `--speed` argparse dest binding, documented `-- --speed=` passthrough intact: PASS. Real-mpv IPC roundtrip (get 1.21 → set 1.15 → set 1.21 → get): PASS. Full E2E smoke (real syncplayServer + two console clients with `--speed 1.21` + real mpv, gradual drift injected via IPC): init applies `set_property speed 1.21` on both clients, alice slowdown writes `set_property speed 1.15` (baseline captured, relative rate), revert writes `set_property speed 1.21` after the slowdown, no `speed 1.00` anywhere, OSD "Reverting speed back to normal" shown, final live speeds alice=1.21 bob=1.21: PASS. Manual mplayer/VLC not exercised locally (mplayer absent; VLC logic covered by unit tests + Lua syntax check).

## [S1] Problem

When Syncplay corrects a desync by slowing the player down (`_slowDownToCoverTimeDifference`, `syncplay/client.py`), the revert restores a hardcoded `setSpeed(1.00)` instead of the user's baseline speed. A user running at `--speed=1.21` via player arguments (or mpv.conf / in-session hotkeys) gets silently reset to 1.0× after the first desync correction, desynchronising them from a room playing at 1.21× (drift ~0.21 s/s, correction loops). Only a `_speedChanged` boolean is kept; the pre-change speed is never captured. Identical on v1.7.6 and master (`49e2a1b`). Systemically, the only way to set a speed is raw per-player arguments (`--speed=` mpv, `-speed` mplayer, `--rate=` VLC) that Syncplay never parses.

## [S2] Design

### S2.1 Revert restores captured baseline (relative slowdown)

- In the slowdown branch: capture `self._speedBeforeSlowdown = player.getSpeed()` **before** `setSpeed`, then apply `baseline × constants.SLOWDOWN_RATE` (0.95, relative instead of absolute).
- In the revert branch: `setSpeed(baseline)` instead of `setSpeed(1.00)`; clear `_speedChanged` and `_speedBeforeSlowdown`.
- Baseline resolution helper `_baselineSpeedOrDefault()`: live read if `is not None` (never `or` — 0.0 is a valid speed), else `self._config['speed']` (defaults to 1.0).
- `_speedChanged` guard, `SLOWDOWN_RESET_THRESHOLD` (0.1) revert condition, and OSD messages unchanged. No new translation keys.

### S2.2 BasePlayer.getSpeed() contract

`getSpeed() -> float | None` on `BasePlayer` with default `return None` (optional capability; `None` = unknown → baseline fallback; called only behind the `speedSupported` gate, so MPC-HC/BE never reaches it).

### S2.3 Per-player baseline read

- **mpv** (+mpvnet/IINA/memento via inheritance): synchronous `mpvpipe.command("get_property","speed")` in try/except → float/None; guards for missing listener/mpvpipe. Same IPC path as existing `set_property` writes (vendor timeout 120 s — same exposure, noted in commit message).
- **mplayer**: `_getProperty('speed')` + new `lineReceived` branch `ANS_speed=` → `_speedAsk` Event (**cleared before ask**); bounded `wait(SPEED_GET_TIMEOUT=0.5)` → None on timeout. Safe: `lineReceived` runs on the dedicated listener thread.
- **VLC**: **poll-cached rate** (an on-demand request/response getter cannot work: both `_slowDownToCoverTimeDifference` and `VLCProtocol.lineReceived` run on the Twisted reactor → any Event wait times out every time). Lua `detectchanges()` emits `rate: <value>` each poll (after the position line); `vlc.py` caches it in `_rate` (parse wrapped in try/except — Lua may emit `nil`; comma radix handled; `no-input` keeps previous cache); `getSpeed()` returns the cache non-blockingly. Bundled Lua is copied to the VLC intf dir every launch (`vlc.py:469-475`) → no version guard needed; old Lua/new Python → None → safe fallback.

### S2.4 Internal `speed` config option (entry channel + fallback)

- Config key `speed` (float, default 1.0): `_config` default, `_numeric` list, `_iniStructure.client_settings` (required for persistence; existing INI files without the key keep the default), CLI `--speed` (argparse dest `speed` binds directly via `_overrideConfigWithArgs`).
  - CLI note: bare `syncplay --speed=1.21` is already an argparse error today; the documented player-args form `file -- --speed=1.21` (and the GUI "Player arguments" field) are unaffected. `speed=abc` in INI raises an uncaught `ValueError` — pre-existing behavior of all `_numeric` keys, out of scope. `--speed 0` is dropped by the shared `if val:` filter — documented limitation (GUI range starts at 0.10).
- Application via extracted helper `_applyConfiguredSpeed()` (`speedSupported`, `!= 1.0` and **`not self._speedChanged`** guards — a file reload mid-desync must not Syncplay-side cancel the in-flight slowdown; the revert restores the baseline afterwards) called from **two hooks**: `initPlayer` (`client.py:192`, shared by all adapters) **and** `updateFile` (`client.py:577`, after the `not path` guard) — because VLC's `set-rate` is a no-op before any media exists (`set_var` requires `vlc.object.input()`) and the rate is input-scoped / may reset on file load; the re-apply makes the option survive playlist advances. The `!= 1.0` guard guarantees Syncplay never clobbers an unconfigured startup (player-args path preserved). Residual pre-existing gap (not introduced here): if the player itself resets its rate on file load mid-slowdown, the slowdown is not re-asserted while `_speedChanged` is armed — the revert fires on the next threshold check as designed.
- GUI: `QDoubleSpinBox` (`objectName == "speed"`, Sync tab desync group, range 0.10–4.00, step 0.01) with **dedicated load/save isinstance branches** (`QDoubleSpinBox` is not a `QSpinBox` subclass); label/tooltip keys `speed-label` / `speed-tooltip` in `messages_en.py` only (English fallback is the established pattern).
- The option is an **entry channel and fallback only** — the live read stays the primary revert source (reality wins over declaration: hotkeys can change speed mid-session). No special-casing of the option in the revert logic (DRY).

### S2.5 Explicit boundaries

- No room-wide speed sync (PR #754 territory: protocol `speed` field, `speedSync` feature flag, `sp` command).
- No parsing of player arguments as baseline source.
- No new translation keys for the revert messages; no sync-threshold / dead-reckoned position changes.
- Assumed limitations: a hotkey speed change *during* the slowdown→revert window is overwritten by the restored snapshot; GUI/mplayer-integration not automated (manual/unit-scoped).
- VLC cold-cache window: VLC's `getSpeed()` reads a cache fed by the Lua poll (`rate:` every `PLAYER_ASK_DELAY`=0.1 s, only while an input exists). Before the first `rate:` response, capture is `None` → fallback `config['speed']` (default 1.0). In practice a slowdown requires `diff > 1.5 s`, i.e. ≥1.5 s of divergent playback, while the poll warms the cache within ≤0.1 s of input existing — the cache is warm before any slowdown can fire; exotic residual setups fall back cleanly (no crash, no mpv/mplayer regression).
- mplayer `getSpeed()` blocks the reactor up to `SPEED_GET_TIMEOUT` (0.5 s max, explicitly bounded — unlike the existing unbounded mplayer position/pause waits); the only slowdown/revert path calling it is rare (desync episodes).
- VLC `rate: no-input` branch is defensive only — the Lua emits `rate` solely inside the input branch; guard kept for robustness.

## Out of Scope

- Room-wide speed sync across the room (open PR #754).
- Player-argument dialect parsing as baseline source.
- Fixing the pre-existing `_numeric` uncaught `ValueError` for bad INI values.
- New translations beyond English keys; speed command/menu in GUI console.
- Dead-reckoned position extrapolation at non-1.0× speeds (separate concern).

## Tasks

- [x] T0: Workspace + feature doc — acceptance: worktree `.worktrees/speed-revert-baseline` on branch `fix/speed-revert-baseline`, this doc written (covers: S2)
- [x] T1: Config `speed` key — ConfigurationGetter default + `_numeric` + `_iniStructure` + `--speed` CLI — acceptance: `speed=1.21` persists/reloads from INI; legacy INI without key loads with default 1.0; `--speed 1.21` binds `config['speed']` (covers: S2.4)
- [x] T2: Client baseline capture + contract — `BasePlayer.getSpeed()`, `_speedBeforeSlowdown` state, `_baselineSpeedOrDefault()`, rewrite `_slowDownToCoverTimeDifference` — acceptance: slowdown captures before setSpeed and applies baseline×0.95; revert restores baseline; None read → config fallback → 1.0; `_speedChanged` guard, 0.1 threshold, OSD unchanged (covers: S2.1, S2.2)
- [x] T3: Option application (2 hooks) — `_applyConfiguredSpeed()` called from `initPlayer` and `updateFile` — acceptance: config 1.21 → setSpeed(1.21) at init and each updateFile; config 1.0 → never called; `speedSupported=False` → never called; `not _speedChanged` guard (covers: S2.4)
- [x] T4: GUI QDoubleSpinBox — widget `objectName=speed` in addSyncTab desync group, dedicated load/save branches, `speed-label`/`speed-tooltip` in messages_en.py — acceptance: GUI ↔ config round-trip; tooltip shown; range 0.10–4.00 (covers: S2.4) — GUI verified by code/branch inspection (manual widget check pending on a desktop session)
- [x] T5: mpv getSpeed — synchronous `command("get_property","speed")` + guards + try/except — acceptance: returns float or None, never raises; inherited by mpvnet/IINA/memento (covers: S2.3)
- [x] T6: mplayer getSpeed — `SPEED_GET_TIMEOUT=0.5`, `_speed`/`_speedAsk` state (clear before ask), lineReceived branch, `_takeLocksDown` — acceptance: `ANS_speed=1.21` → 1.21; timeout → None ≤0.5 s (covers: S2.3)
- [x] T7: VLC poll-cached rate — Lua `rate:` emission in `detectchanges()`, `_rate` cache, non-blocking getSpeed, protected lineReceived branch — acceptance: after ≥1 poll with input getSpeed returns float; no wait in getSpeed; `1,21` → 1.21; `rate: nil` → no exception, cache unchanged (covers: S2.3)
- [x] T8: Regression tests — `tests/test_speed_revert.py` (stdlib unittest) — acceptance: `python3 -m unittest discover -s tests -v` green (21 tests), including revert→1.21, config fallback, application guards (covers: S2.1, S2.2, S2.4)
- [x] T9: Verification — py_compile, test suite, manual mpv smoke — acceptance: all green; `player >> ['set_property', 'speed', '1.21']` observed on revert (depends: T3–T8)

## Journey log

- Investigation: bug byte-identical on v1.7.6 and master; speed is write-only in Syncplay; open PR #754 is room-wide prior art (out of scope).
- Initial VLC design (on-demand `get-rate` + Event wait) rejected during design: reactor reentrancy makes the wait always time out → poll-cache instead.
- Plan reviewed by two independent neutral agents (both APPROVE WITH CHANGES, 0 CRITICAL): added the two-hook option application (VLC init no-op + no re-apply on file load) and the protected VLC rate parse; corrected the CLI migration narrative (bare `--speed=` was already an argparse error; documented `file -- --speed=` form intact) and smoke commands (`--player-path`; no `--player`/`--player-args` flags exist); softened T1 acceptance (`_numeric` is a bare `float()`); added Event clear-before-ask, explicit 120 s vendor timeout note, `--speed 0` and revert-snapshot limitations.
- E2E smoke required three environment discoveries (all test-harness, not product): (1) external mpv seeks are detected as real seeks (`_determinePlayerStateChange` dual threshold) and re-sync instantly → inject gradual drift by slowing bob instead; (2) readiness dance needed (`t` before `p`, else `_toggleReady` converts the unpause into "set as ready - unpause again"); (3) headless mpv ran at ~1/15 speed without AV outputs (decode-clock stall) → `-- --vo=null --ao=null` for a real-time clock; also the revert needs `diff < 0.1`, which the room's min-watcher global position parks above (0.1–1.5 dead zone) → a small post-slowdown nudge makes the revert threshold deterministically reachable. UiManager queues OSD text, so final text assertions run after process exit (flush).
- Post-implementation independent review (APPROVE WITH CHANGES, 2 MAJOR): fixed `_applyConfiguredSpeed` lacking the `_speedChanged` guard (file reload could Syncplay-side cancel an active slowdown — regression test added, mutation-proven) and documented the VLC cold-cache window with its temporal analysis (poll 0.1 s vs ≥1.5 s of divergence required to trigger a slowdown). Focused re-review of the delta: APPROVE WITH CHANGES, MINORs only (stale spec line refs, mplayer bounded/unbounded wording, "Syncplay-side cancel" qualification) — all fixed.
