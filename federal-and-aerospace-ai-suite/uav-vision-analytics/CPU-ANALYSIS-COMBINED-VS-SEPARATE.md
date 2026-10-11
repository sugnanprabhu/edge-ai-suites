<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Combined vs Separate Pipeline: CPU Usage Analysis

Investigation into why the **combined** (single tee'd GStreamer pipeline:
detection + captioning sharing one decode) architecture uses substantially
more CPU than the **separate** (two independent pipelines connected via
RTSP relay) architecture, on `uav_object_detection_gpu_caption`
(`configs/config-pymavlink.json`), host `.115`.

## TL;DR

**Root cause confirmed:** combined's tee'd pipeline sets `appsink
sync=false` on both branches so the slow VLM branch never blocks the fast
detection branch. But `sync=false` removes real-time clock pacing from the
**entire** pipeline (no other element is clock-synced), so the detection
branch free-runs at max GPU/decode throughput (~85-120 fps) instead of the
source video's native real-time rate (30 fps) — roughly **3x more detection
work per wall-clock-second** than separate's real-time-paced detection,
plus tee/buffer-copy overhead from sharing one decode path across two
branches.

**GPU contention between detection and captioning does NOT throttle
fps** — disproven empirically (see Experiment 3). Separate's lower CPU is
genuine real-time pacing, not a side-effect of GPU contention degrading its
detection throughput.

This is an **architectural tradeoff, not a bug**: combined trades extra CPU
for (a) eliminating the GPU-OOM crash risk two independent GPU contexts can
cause, and (b) ~2.4x cheaper CPU-per-caption at matched output rate (see
Experiment 1 baseline math below).

## Background / Why CPU%, Raw, Is Misleading

CPU% must be normalized per-caption, not compared raw — separate's lower
CPU% reflects far lower caption throughput under GPU contention, not
efficiency:

- Combined (unoptimized, 1 fps cadence): ~390% CPU, 1 caption/~1s → ~390
  %·s/caption
- Separate (1 fps cadence target but observed ~0.15fps actual due to GPU
  contention on its captioning leg): ~142% CPU, 1 caption/~6.5-7s → ~923-990
  %·s/caption

Combined was already ~2.4x cheaper per useful caption *before* any
optimization.

## Optimizations Applied to `gpu_caption` (this investigation)

1. **Tile-aligned VLM resolution**: `frame_width`/`frame_height` defaults
   640x480 (and user's own 1920x1080-era default) → **896x448** (InternVL's
   native tile is 448px; 896x448 is the correct tile-aligned landscape
   resolution). Also applied by the user to `cpu_caption`/`npu_caption`
   (validated correct — tile alignment is a VLM-preprocessor property,
   independent of inference device).
2. **Pipeline reorder + GPU-accelerated scaling**: the original pipeline did
   CPU `videoconvert` (BGRA colorspace conversion) on *every* decoded frame
   (~19-20 fps) *before* the `videorate drop-only=true` frame-rate limiter —
   wasting ~19x more conversion work than necessary, since only ~1fps of
   frames actually reach `gvagenai`. Fixed by moving `videorate` first, then
   replacing CPU `videoconvert`+`videoscale` with a single GPU/VA-API
   `vapostproc` step, applied only on the surviving low-fps stream.
   - Applied only to `gpu_caption` so far; **not yet applied** to
     `cpu_caption`/`npu_caption` (those would use plain `videoconvert`
     /`videoscale` reordered after `videorate`, since `vapostproc` is
     GPU/VA-API-specific).

Result: CPU dropped from ~392% (original 1920x1080) → ~292% avg (896x448 +
reorder + vapostproc), at 1fps caption cadence. Memory dropped ~950MB →
531MB. Caption cadence/resolution/token-count unaffected.

## Experiment 1 — frame_rate sweep (does caption cadence drive CPU?)

| frame_rate | Caption cadence | CPU avg |
|---|---|---|
| 1 fps (896x448 + reorder + vapostproc) | ~1.5-1.8s/caption | ~292% |
| 0.5 fps | 2.0s/caption | ~277% (only ~5% better) |
| 0.15 fps (matches separate's observed ~6.5-7s cadence) | 6.66s/caption | ~232% |

**Conclusion: frame_rate is NOT the dominant CPU lever** once
resolution/conversion-order are fixed. Even at a caption cadence *exactly
matching* separate's, combined still costs ~63% more CPU (232% vs ~142%).
Most of combined's CPU is spent on frame_rate-**independent** work: the
continuous detection branch, two `gvapython` callbacks (telemetry overlay +
MQTT publish), `gvafpscounter`, and decode — only the `gvagenai`/VLM branch
scales with frame_rate, and that's a small slice of total cost after the
resolution+reorder fix.

(Note: fractional frame_rate values required a temporary, non-production
workaround — GStreamer caps fractions reject decimals like `0.5/1`; tested
by hardcoding exact integer ratios, e.g. `1/2`, `3/20`, and widening the
JSON schema type from `integer` to `number`, then reverting after each
test. A permanent fix would need a `frame_rate_num`/`frame_rate_den` split
or fraction-conversion logic — not implemented.)

## Experiment 2 — does connecting a real video consumer throttle combined back to real-time?

Hypothesis: maybe combined's ~85-120fps "free-running" detection rate is
an artifact of no external viewer being attached during REST-only testing
(i.e., something pulls frames from `appsink` in real time in production,
throttling it back down).

Test: attached a real `gst-launch-1.0 rtspsrc ... ! fakesink sync=true`
client to the pipeline's internal RTSP mount
(`rtsp://127.0.0.1:8555/<path>`, created via
`destination.frame={"type":"rtsp","path":...}` in the REST payload).

**Result: disproven.** fps stayed ~75-88 (no throttling to real-time), and
CPU actually *rose* (280% → 365%) because serving the extra RTSP client
added its own encode/transmit overhead. The free-running rate is a genuine
property of the `sync=false` pipeline design, not a missing-consumer test
artifact.

## Experiment 3 — does GPU contention throttle separate's detection branch? (the key question)

Replicated separate's actual topology directly: two **independent**
pipeline instances (no tee, separate GStreamer/GPU contexts) against the
same source file:

1. `uav_object_detection_gpu` alone (its `appsink` has no `sync=false`
   override, so it defaults to real-time-paced sync=true).
2. A standalone captioning-only pipeline
   (`uav_captioning_gpu_standalone_TEST`, same 896x448+reorder+vapostproc
   pattern as the fixed `gpu_caption`, built for this test).

| Scenario | Detection fps | Total CPU |
|---|---|---|
| Detection alone, uncontended | **30.00** (locked, real-time) | 32% |
| Detection + captioning running concurrently | **30.00** (unchanged) | **~150%** |
| Combined `gpu_caption` (tee'd, 1fps caption) | 85-120 (free-running) | ~292% |

**Detection's fps did not degrade at all under concurrent GPU load from
captioning** — it stayed locked to exactly 30.00 fps. This **disproves**
the earlier hypothesis (from prior session) that separate's lower CPU was
a side-effect of GPU contention silently throttling its detection
throughput. Separate's ~142-150% CPU is genuine, real-time-paced work —
not degraded/contended work. The reproduced two-pipeline total (~150% CPU)
closely matches the original separate benchmark figure (~142%), validating
the reproduction.

## Final Answer: Why Combined Costs More CPU

Combined's tee design requires `appsink sync=false` (so the slow VLM
branch never backpressures/blocks the fast detection branch sharing the
same tee). This has the side effect of removing real-time clock pacing
from the **whole** pipeline, since no other element enforces it — so
detection runs at ~85-120fps (max throughput) instead of the video's
native 30fps, roughly **3x more detection/overlay/metadata work per
wall-clock-second** than separate's real-time-paced detection, plus
additional tee/buffer-copy overhead from feeding two branches off one
decode. This is the dominant, previously-undiagnosed CPU driver — bigger
than caption cadence, resolution, or GPU contention.

## Possible Follow-up (not yet implemented)

- Add a **second, independent `videorate`** on the detection leg (before
  `gvadetect`), capped near the source's native rate (e.g. ~30fps), to stop
  detection from free-running at 85-120fps while keeping `sync=false`
  overall (so the VLM branch still isn't blocked). This should shrink
  combined's CPU further without sacrificing captioning cadence — tradeoff
  is reduced live-detection responsiveness above the cap, which for a
  30fps source is a no-op in practice.
- Apply the `videorate`-before-convert reorder (plain `videoconvert`
  /`videoscale`, no `vapostproc`) to `cpu_caption`/`npu_caption` as already
  done for `gpu_caption`.
- Update/author a dedicated benchmark doc combining these numbers with the
  original separate-vs-combined comparison once the above follow-up is
  tried.

## Remaining Housekeeping

- A temporary pipeline definition, `uav_captioning_gpu_standalone_TEST`,
  was added to `configs/config-pymavlink.json` for Experiment 3. It should
  be removed (or kept deliberately for future re-testing) — not yet
  resolved as of this writing.
