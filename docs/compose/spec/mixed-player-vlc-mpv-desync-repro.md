# Joint repro protocol (MPV + VLC desync)

## Setup

1. Start a local Syncplay server from the feature worktree (or public server).
2. Client A: Syncplay + **mpv**, DEBUG_MODE on.
3. Client B: Syncplay + **VLC 3**, DEBUG_MODE on.
4. Same room, same file.

## Scenarios

| # | Media | Expectation after fix |
|---|-------|------------------------|
| 1 | Cloudflare HTTP MKV URL (user's file) | No continuous seek loop; max \|diff\| stays under FF/rewind thresholds most of the time |
| 2 | Local dual-audio MKV | Same as #1 |
| 3 | VLC + VLC local MKV | Regression: still syncs as before |

## Metrics (per 10 minutes of playback)

- Count `sync-correction-applied` in DEBUG logs
- Count `sync-correction-suppressed`
- Count VLC-seek-landing-miss
- Max observed `syncdiff` line’s `diff` value
- Whether user saw rewind/fastforward OSD spam

## Baseline

Run the same scenarios on unmodified master (or note “pre-fix observed seek loop within minutes”).

## DEBUG enable

Run with `--debug` / set `constants.DEBUG_MODE = True` for the session, or use the existing debug CLI flag of Syncplay.

## Tags to grep

- `VLCpos`
- `syncdiff`
- `sync-correction-applied`
- `sync-correction-suppressed`
- `VLC-seek-landing-miss`
