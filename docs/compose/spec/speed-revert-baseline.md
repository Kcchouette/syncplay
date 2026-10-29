---
feature: speed-revert-baseline
status: in-progress
updated: 2026-07-07
source: fix/speed-revert-baseline
---

# Speed Revert Baseline + Internal Speed Option

## Report

## [S1] Problem

When Syncplay corrects a desync by slowing the player down (`_slowDownToCoverTimeDifference`, `syncplay/client.py:434-450`), the revert restores a hardcoded `setSpeed(1.00)` (line 446) instead of the user's baseline speed. A user running at `--speed=1.21` via player arguments (or mpv.conf / in-session hotkeys) gets silently reset to 1.0× after the first desync correction, desynchronising them from a room playing at 1.21× (drift ~0.21 s/s, correction loops). Only a `_speedChanged` boolean is kept (`client.py:149`); the pre-change speed is never captured. Identical on v1.7.6 and master (`49e2a1b`). Systemically, the only way to set a speed is raw per-player arguments (`--speed=` mpv, `-speed` mplayer, `--rate=` VLC) that Syncplay never parses.

## [S2] Design

### S2.1 Revert restores captured baseline (relative slowdown)

- In the slowdown branch: capture `self._speedBeforeSlowdown = player.getSpeed()` **before** `setSpeed`, then apply `baseline × constants.SLOWDOWN_RATE` (0.95, relative instead of absolute).
- In the revert branch: `setSpeed(baseline)` instead of `setSpeed(1.00)`; clear `_speedChanged` and `_speedBeforeSlowdown`.
- Baseline resolution helper `_baselineSpeedOrDefault()`: live read if `is not None` (never `or` — 0.0 is a valid speed), else `self._config['speed']` (defaults to 1.0).
- `_speedChanged` guard, `SLOWDOWN_RESET_THRESHOLD` (0.1) revert condition, and OSD messages unchanged. No new translation keys.

### S2.2 BasePlayer.getSpeed() contract

`getSpeed() -> float | None` on `BasePlayer` with default `return None` (optional capability; `None` = unknown → baseline fallback; called only behind the `speedSupported` gate at `client.py:477`, so MPC-HC/BE never reaches it).

### S2.3 Per-player baseline read

- **mpv** (+mpvnet/IINA/memento via inheritance): synchronous `mpvpipe.command("get_property","speed")` in try/except → float/None; guards for missing listener/mpvpipe. Same IPC path as existing `set_property` writes (vendor timeout 120 s — same exposure, noted in commit message).
- **mplayer**: `_getProperty('speed')` + new `lineReceived` branch `ANS_speed=` → `_speedAsk` Event (**cleared before ask**); bounded `wait(SPEED_GET_TIMEOUT=0.5)` → None on timeout. Safe: `lineReceived` runs on the dedicated listener thread.
- **VLC**: **poll-cached rate** (an on-demand request/response getter cannot work: both `_slowDownToCoverTimeDifference` and `VLCProtocol.lineReceived` run on the Twisted reactor → any Event wait times out every time). Lua `detectchanges()` emits `rate: <value>` each poll (after the position line); `vlc.py` caches it in `_rate` (parse wrapped in try/except — Lua may emit `nil`; comma radix handled; `no-input` keeps previous cache); `getSpeed()` returns the cache non-blockingly. Bundled Lua is copied to the VLC intf dir every launch (`vlc.py:469-475`) → no version guard needed; old Lua/new Python → None → safe fallback.

### S2.4 Internal `speed` config option (entry channel + fallback)

- Config key `speed` (float, default 1.0): `_config` default, `_numeric` list, `_iniStructure.client_settings` (required for persistence; existing INI files without the key keep the default), CLI `--speed` (argparse dest `speed` binds directly via `_overrideConfigWithArgs`).
  - CLI note: bare `syncplay --speed=1.21` is already an argparse error today; the documented player-args form `file -- --speed=1.21` (and the GUI "Player arguments" field) are unaffected. `speed=abc` in INI raises an uncaught `ValueError` — pre-existing behavior of all `_numeric` keys, out of scope. `--speed 0` is dropped by the shared `if val:` filter — documented limitation (GUI range starts at 0.10).
- Application via extracted helper `_applyConfiguredSpeed()` (`speedSupported`, `!= 1.0` and **`not self._speedChanged`** guards — a file reload mid-desync must not cancel the in-flight slowdown; the revert restores the baseline afterwards) called from **two hooks**: `initPlayer` (`client.py:185`, shared by all adapters) **and** `updateFile` (`client.py:562`, after the `not path` guard) — because VLC's `set-rate` is a no-op before any media exists (`set_var` requires `vlc.object.input()`) and the rate is input-scoped / may reset on file load; the re-apply makes the option survive playlist advances. The `!= 1.0` guard guarantees Syncplay never clobbers an unconfigured startup (player-args path preserved).
- GUI: `QDoubleSpinBox` (`objectName == "speed"`, Sync tab desync group, range 0.10–4.00, step 0.01) with **dedicated load/save isinstance branches** (`QDoubleSpinBox` is not a `QSpinBox` subclass); label/tooltip keys `speed-label` / `speed-tooltip` in `messages_en.py` only (English fallback is the established pattern).
- The option is an **entry channel and fallback only** — the live read stays the primary revert source (reality wins over declaration: hotkeys can change speed mid-session). No special-casing of the option in the revert logic (DRY).

### S2.5 Explicit boundaries

- No room-wide speed sync (PR #754 territory: protocol `speed` field, `speedSync` feature flag, `sp` command).
- No parsing of player arguments as baseline source.
- No new translation keys for the revert messages; no sync-threshold / dead-reckoned position changes.
- Assumed limitations: a hotkey speed change *during* the slowdown→revert window is overwritten by the restored snapshot; GUI/mplayer-integration not automated (manual/unit-scoped).
- VLC cold-cache window: VLC's `getSpeed()` reads a cache fed by the Lua poll (`rate:` every `PLAYER_ASK_DELAY`=0.1 s, only while an input exists). Before the first `rate:` response, capture is `None` → fallback `config['speed']` (default 1.0). In practice a slowdown requires `diff > 1.5 s`, i.e. ≥1.5 s of divergent playback, while the poll warms the cache within ≤0.1 s of input existing — the cache is warm before any slowdown can fire; exotic residual setups fall back cleanly (no crash, no mpv/mplayer regression).
- mplayer `getSpeed()` blocks the reactor up to `SPEED_GET_TIMEOUT` (0.5 s max) — same nature as the existing position/pause waits, bounded by design; note-only.
- VLC `rate: no-input` branch is defensive only — the Lua emits `rate` solely inside the input branch; guard kept for robustness.

## Out of Scope

- Room-wide speed sync across the room (open PR #754).
- Player-argument dialect parsing as baseline source.
- Fixing the pre-existing `_numeric` uncaught `ValueError` for bad INI values.
- New translations beyond English keys; speed command/menu in GUI console.
- Dead-reckoned position extrapolation at non-1.0× speeds (separate concern).

## Tasks

- [ ] T0: Workspace + feature doc — acceptance: worktree `.worktrees/speed-revert-baseline` on branch `fix/speed-revert-baseline`, this doc written (covers: S2)
- [ ] T1: Config `speed` key — ConfigurationGetter default + `_numeric` + `_iniStructure` + `--speed` CLI — acceptance: `speed=1.21` persists/reloads from INI; legacy INI without key loads with default 1.0; `--speed 1.21` binds `config['speed']` (covers: S2.4)
- [ ] T2: Client baseline capture + contract — `BasePlayer.getSpeed()`, `_speedBeforeSlowdown` state, `_baselineSpeedOrDefault()`, rewrite `_slowDownToCoverTimeDifference` — acceptance: slowdown captures before setSpeed and applies baseline×0.95; revert restores baseline; None read → config fallback → 1.0; `_speedChanged` guard, 0.1 threshold, OSD unchanged (covers: S2.1, S2.2)
- [ ] T3: Option application (2 hooks) — `_applyConfiguredSpeed()` called from `initPlayer` and `updateFile` — acceptance: config 1.21 → setSpeed(1.21) at init and each updateFile; config 1.0 → never called; `speedSupported=False` → never called (covers: S2.4)
- [ ] T4: GUI QDoubleSpinBox — widget `objectName=speed` in addSyncTab desync group, dedicated load/save branches, `speed-label`/`speed-tooltip` in messages_en.py — acceptance: GUI ↔ config round-trip; tooltip shown; range 0.10–4.00 (covers: S2.4)
- [ ] T5: mpv getSpeed — synchronous `command("get_property","speed")` + guards + try/except — acceptance: returns float or None, never raises; inherited by mpvnet/IINA/memento (covers: S2.3)
- [ ] T6: mplayer getSpeed — `SPEED_GET_TIMEOUT=0.5`, `_speed`/`_speedAsk` state (clear before ask), lineReceived branch, `_takeLocksDown` — acceptance: `ANS_speed=1.21` → 1.21; timeout → None ≤0.5 s (covers: S2.3)
- [ ] T7: VLC poll-cached rate — Lua `rate:` emission in `detectchanges()`, `_rate` cache, non-blocking getSpeed, protected lineReceived branch — acceptance: after ≥1 poll with input getSpeed returns float; no wait in getSpeed; `1,21` → 1.21; `rate: nil` → no exception, cache unchanged (covers: S2.3)
- [ ] T8: Regression tests — `tests/test_speed_revert.py` (stdlib unittest, cases 1–17 of plan) — acceptance: `python3 -m unittest discover -s tests -v` green, including revert→1.21, config fallback, application guards (covers: S2.1, S2.2, S2.4)
- [ ] T9: Verification — py_compile, test suite, manual mpv smoke (both CLI forms) — acceptance: all green; `player >> ['set_property', 'speed', '1.21']` observed on revert (depends: T3–T8)

## Journey log

- Investigation: bug byte-identical on v1.7.6 and master; speed is write-only in Syncplay; open PR #754 is room-wide prior art (out of scope).
- Initial VLC design (on-demand `get-rate` + Event wait) rejected during design: reactor reentrancy makes the wait always time out → poll-cache instead.
- Plan reviewed by two independent neutral agents (both APPROVE WITH CHANGES, 0 CRITICAL): added the two-hook option application (VLC init no-op + no re-apply on file load) and the protected VLC rate parse; corrected the CLI migration narrative (bare `--speed=` was already an argparse error; documented `file -- --speed=` form intact) and smoke commands (`--player-path`; no `--player`/`--player-args` flags exist); softened T1 acceptance (`_numeric` is a bare `float()`); added Event clear-before-ask, explicit 120 s vendor timeout note, `--speed 0` and revert-snapshot limitations.
