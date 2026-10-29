---
feature: mixed-player-vlc-mpv-desync
status: delivered
updated: 2026-10-30
source: fix/mixed-vlc-mpv-desync
---

# Fix MPV↔VLC continuous desync (HTTP MKV streams)

## Report

**What was built** — VLC position estimator (rate EMA, freeze detection that never stalls the wire position, seek-jump reset), sample-confirmed seek landing with warn-only miss detection, client forced-correction gate (cooldown + growth + 2× escalation) on rewind/fast-forward, and greppable DEBUG instrumentation. Server min() unchanged. Critical review fixes: pause/unpause holds position without wall-time inflation; VLC EOF heuristic uses a raw-sample chain.

**Verification** — `PYTHONPATH=. python3 -m unittest discover tests -v` → 21 tests OK. Independent first review REJECT (2 criticals) → both fixed → re-review APPROVE. Joint live repro on the user’s Cloudflare MKV: after adding title×604800 sanitization, VLC raw samples stay in media range, **0 forced corrections**, max \|diff\| ≈ 0.55s during playback (user confirmed “oui ça marche”).

## [S1] Problem

On Syncplay 1.7.6 (and master), MPV+VLC clients watching the same file develop continuous desync and a seek loop. VLC+VLC is fine. Master did not fix this: `players/vlc.py` and the seek/desync algorithm were unchanged since v1.7.6.

Root cause is clock quality (VLC coarse/inaccurate `time` on network MKV) amplified by server room authority `min(watcher positions)`.

## [S2] Design

### S2.1 VLC position estimator

Pure module `syncplay/players/vlcPositionEstimator.py` with injected `now`. Estimates playback rate (EMA, clamped), detects seek jumps, detects freezes internally.

- `position_at(now)`: internal estimate (frozen → hold anchor).
- `reported_position_at(now)`: **always** wall-advances while playing so freeze never stalls the wire position used by server min().

Wired into `VlcPlayer` with a lock. Skip `observe` when `_filechanged` (global position injected, not a raw sample).

### S2.2 Seek landing warn-only

On `setPosition`, do not move estimator anchor to target until a sample confirms (or timeout). If sample lands far from target after timeout, log `VLC-seek-landing-miss`. **No automatic re-seek.**

### S2.3 Forced-correction gate

Pure `ForcedCorrectionGate` suppresses rewind/fast-forward for `FORCED_CORRECTION_COOLDOWN` seconds unless `|diff| ≥ FORCED_CORRECTION_ESCALATION_FACTOR × threshold` or desync is growing for `FORCED_CORRECTION_MIN_PERSISTENT_SAMPLES` samples. Cleared on unpause and explicit `doSeek`. Soft slowdown not suppressed. Applied inside rewind/FF helpers.

### S2.4 Server min()

Unchanged.

### S2.5 Instrumentation

DEBUG_MODE greppable tags: `VLCpos …`, `syncdiff …`, `sync-correction-suppressed …`, `sync-correction-applied …`.

## [S3] Out of Scope

- Frame-accurate A/V sync; audio-delay compensation
- VLC 4
- Default threshold changes
- Dual-audio track sync
- Server aggregation redesign
- Automatic seek-landing retry

## Tasks

- [x] T1: Pure VlcPositionEstimator + unit tests — acceptance: quanta/freeze/seek/rate tests; freeze does not stall reported position; no Twisted (covers: S2.1)
- [x] T2: Wire estimator into VlcPlayer — acceptance: lock; skip when _filechanged; reported position used (covers: S2.1)
- [x] T3: Seek landing warn-only + sample-confirmed anchor — acceptance: miss logs warning; no auto re-seek (covers: S2.2)
- [x] T4: ForcedCorrectionGate + wire into rewind/FF — acceptance: cooldown/persist tests; doSeek always applies (covers: S2.3)
- [x] T5: DEBUG instrumentation — acceptance: greppable tags with setBy/messageAge (covers: S2.5)
- [x] T6: Verify unit tests + document joint repro protocol (covers: verification)

## Journey log

- Independent reviews (arch/clean-code/risk) all APPROVE WITH CHANGES; freeze-must-not-stall-reported-position, defer auto seek-retry, cooldown in rewind/FF helpers.
- Freeze-vs-seek ordering bug caught by unit tests: stalled time must not look like a large seek jump (`abs(dt_vlc - dt_wall)`).
- First implementation review REJECT: (1) unpause could add pause wall-time via `position_at`; (2) EOF equality on wall-advancing `_position` was dead. Fixed with paused-aware re-anchor and raw-sample EOF chain; re-review APPROVE.
- Live repro (user’s Cloudflare MKV, MPV+VLC): VLC `get_time()` returned title×604800+time (610582s vs 5782s) → seek loop (60 applied corrections). Fixed by `sanitize_vlc_title_offset` in Python + duration clamp in `syncplay.lua get_time()`. Post-fix: 0 corrections, max \|diff\| 0.55s. User: “oui ça marche”.
