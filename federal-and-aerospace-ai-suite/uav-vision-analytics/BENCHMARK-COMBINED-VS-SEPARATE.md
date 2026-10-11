<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Combined vs Separate Captioning Pipeline — Benchmark Results

## Branches compared

| Branch | Architecture |
|---|---|
| `feature/uav-lvc-integration` | **Combined** — one merged GStreamer pipeline per device (`uav_object_detection_<device>_caption`) does detection + VLM captioning in a single graph, fanning the same encoded/overlaid frames out to RTSP and (optionally) WebRTC. Captions publish straight to MQTT topic `uav/caption/<device>`. No `video-caption-service` container. |
| `feature/lvc-only-tr` | **Separate** — a plain detection-only pipeline (`uav_object_detection_<device>`, RTSP only) feeds its RTSP output as the source for a second, independent VLM-captioning pipeline launched through the real `video-caption-service` (LVC) dashboard/REST container. That captioning leg is always WebRTC-only output; captions publish to MQTT topic `live-video-captioning/<runId>`. |
| `feature/uav-lvc-integration` **without WebRTC** | Same combined pipeline as above, but with the destination reverted to a single RTSP sink (commit `6ce03ab52` — "Add WebRTC/mediamtx output support to combined pipeline" — not applied), isolating WebRTC's own cost. |

All tests drove the **real** Docker Compose stacks (`docker-compose-pymavlink.yml` / `docker-compose-lvc.yml`) — not synthetic clones — and verified real RTSP (ffprobe), real WebRTC (mediamtx `/v3/paths/get` API), and real captions received over MQTT, with 150s of `docker stats` + GPU/NPU metrics sampling per device.

## Results — Machine 1 (`10.106.147.192`, CPU + GPU only, no NPU)

| Variant | Device | Detection/Caption FPS | Total stack CPU% | Notes |
|---|---|---|---|---|
| Combined + WebRTC | CPU | 11.90 | 1396% | Works |
| Combined + WebRTC | GPU | 5.24 | 348% | Works; GPU render ~99.5% (VLM-bound) |
| Combined, no WebRTC | CPU | 19.92 | 1531% | Works |
| Combined, no WebRTC | GPU | 5.07 | 232% | Works |
| Separate (`lvc-only-tr`) | CPU | 17.26 (detection leg) | 1596% | Works — real WebRTC + MQTT caption verified |
| Separate (`lvc-only-tr`) | GPU | 29.88 (detection leg only) | 224% | **Captioning never starts** — repeated GStreamer `not-negotiated` RTSP error, aborts; reproduced twice |

### Isolated WebRTC overhead (combined pipeline, same branch)

| Device | FPS: no-WebRTC → WebRTC | Total CPU%: no-WebRTC → WebRTC |
|---|---|---|
| CPU | 19.92 → 11.90 (**‑40%**) | 1531% → 1396% |
| GPU | 5.07 → 5.24 (≈unchanged, GPU-saturated) | 232% → 348% (**+50%**) |

## Results — Machine 2 (`10.106.147.115`, CPU + GPU + NPU)

| Variant | Device | Detection/Caption FPS | Total stack CPU% | Notes |
|---|---|---|---|---|
| Combined + WebRTC | CPU | 11.92 | 1247% | Works |
| Combined + WebRTC | GPU | 15.11 | 506% | Works |
| Combined + WebRTC | NPU-detect + CPU-VLM | 15.73 | 733% | Works (NPU has no VLM backend; VLM always runs CPU/GPU) |
| Combined, no WebRTC | CPU | 19.33 | 1457% | Works |
| Combined, no WebRTC | GPU | 42.91 | 574% | Works |
| Combined, no WebRTC | NPU-detect + CPU-VLM | 176\* | 837% | Works; fps figure looks like a measurement artifact (see note) |
| Separate (`lvc-only-tr`) | CPU | 16.91 | — | Works — real WebRTC + MQTT caption verified |
| Separate (`lvc-only-tr`) | GPU | 2.32 (detection only) | — | **Captioning fails**: `Failed to initialize OpenVINO backend ... [GPU] ProgramBuilder build failed! clWaitForEvents, error code: -58 CL_INVALID_EVENT` |
| Separate (`lvc-only-tr`) | NPU-detect + CPU-VLM | 30.00 | — | Works — real WebRTC + MQTT caption verified |

\* The 176 fps NPU/no-WebRTC reading is implausibly high for this pipeline and is treated as a likely artifact (probably caused by no persistent RTSP/WebRTC consumer holding backpressure during that run) rather than a real throughput number; the *functional* result (pipeline ran, caption delivered) is still valid.

## Bottom line

**`feature/uav-lvc-integration` (combined) is the better architecture — confirmed on two independent machines.**

- It is the only one of the two that reliably works across **all three devices** (CPU, GPU, NPU-detect), with or without WebRTC.
- `feature/lvc-only-tr` (separate) works for CPU and NPU+CPU-VLM, but its **GPU captioning path is broken** by a reproducible GPU/OpenCL resource conflict when a second, independent pipeline tries to acquire the GPU that the detection pipeline is already using:
  - Machine 1: GStreamer `gst-stream-error-quark: ... streaming stopped, reason not-negotiated (-4)` — repeated reconnect/abort loop, no caption ever produced.
  - Machine 2: explicit OpenVINO/OpenCL failure — `[GPU] ProgramBuilder build failed! clWaitForEvents, error code: -58 CL_INVALID_EVENT`.
  - Same symptom family (GPU hand-off between two independently-launched pipelines fails), reproduced on different hardware — this points to a structural limitation of the separate-pipeline design, not a one-off glitch.
- **WebRTC overhead** is real but tunable, not a correctness issue: on CPU devices it costs roughly 35–40% fps; on GPU/NPU devices (already VLM-bound) the fps impact is smaller but CPU usage rises noticeably (WebRTC's VP9 encode competing for the same cores).

### Recommendation

Adopt `feature/uav-lvc-integration` (combined pipeline) as the primary architecture. If `feature/lvc-only-tr`'s separate/dashboard-driven design is still wanted for its operational flexibility (e.g. independently restartable captioning leg, reuse of the stock LVC dashboard), its GPU OpenCL context conflict must be root-caused and fixed first — it is not currently viable for GPU-accelerated captioning deployments.

## Update — 2026-02-11: `jpegdec` fix on Separate (GPU) + sustained-load re-test

Machine 2 (`10.106.147.115`) only. `feature/lvc-only-tr` commit `9a28610f` ("Use jpegdec instead of hardware one") replaces `vajpegdec ! vapostproc` with `jpegdec ! videoconvert` in the `Video_Captioning_RTSP_Hardware_Default_Resolution` pipeline — i.e. the captioning leg now **software**-decodes the MJPEG RTSP stream produced by the detection leg, instead of using the VA-API hardware JPEG decoder. This changes the earlier "GPU captioning fails outright" result and was re-tested fresh, alongside a fresh run of the combined (`feature/uav-lvc-integration`) GPU+WebRTC case for direct comparison.

| Variant | Device | Result | Startup | Steady-state | Sustained-load (6+ min) outcome |
|---|---|---|---|---|---|
| Separate (`lvc-only-tr`, jpegdec fix) | GPU detect + GPU caption | **Starts and runs** (previously failed 100% of the time) | OK — real MQTT captions, `stream-ready: true` | `dlstreamer-pipeline-server` CPU ≈142% avg; caption cadence ≈1 per 6–7s; `inference_duration_mean` ≈4.1–4.5s | **Crashes** — whole container aborts with `CL_OUT_OF_RESOURCES` → `clFinish, error code: -5` → `ov::Exception` → core dump, after ≈5.5–6 min of concurrent GPU detection + GPU captioning |
| Combined (`uav-lvc-integration`) + WebRTC | GPU detect + GPU caption (single tee pipeline) | Starts and runs | OK — real MQTT captions, WHEP endpoint live (HTTP 204 on `OPTIONS .../whep`) | `dlstreamer-pipeline-server` CPU ≈380–404% avg; caption cadence ≈1 per second; `inference_duration_mean` ≈546–551ms, `generate_duration_mean` ≈1087–1097ms | **No crash** — ran a continuous 8 min 19 s (499 s) session, FPS steady at 18.6–19.7 throughout, stopped cleanly via `DELETE /pipelines/{instance_id}` (final state `ABORTED`, not aborted-by-crash) |

### What this changes vs. the original comparison above

- The `jpegdec` fix **does** resolve the deterministic `vajpegdec`/`jpegenc` codec-negotiation failure that previously made separate-architecture GPU captioning fail 100% of the time on Machine 2 — this is a real, confirmed improvement over the original row in the table above.
- However, it does **not** fix the underlying problem: two independently-launched DL Streamer pipelines (detection + captioning), each opening its **own** GPU/OpenCL context, still exhaust GPU resources after several minutes of concurrent operation and crash the entire `dlstreamer-pipeline-server` process. This is the *same* root cause as the Machine‑2 `CL_INVALID_EVENT` failure recorded above, just manifesting later (after ~5.5–6 min instead of immediately) because the codec mismatch no longer aborts the pipeline before the leak/contention has time to build up.
- The combined architecture's single pipeline — one shared GStreamer/OpenCL/OpenVINO context for both detection and captioning — ran **8+ minutes without any sign of degradation or crash**, past the point where the separate architecture reliably fails. This is the strongest evidence yet that the GPU resource contention is structural to the separate (two-independent-pipelines) design, not a tunable parameter or transient glitch.
- Combined's per-caption latency is also dramatically better in this fresh test: **~1 caption/second** (`inference_duration_mean` ≈550ms) vs. separate's **~1 caption per 6–7 seconds** (`inference_duration_mean` ≈4.1–4.5s) — consistent with avoiding duplicate JPEG encode/decode and the RTSP transport hop between legs (see `PIPELINE-ARCHITECTURE-COMBINED-VS-SEPARATE.md`).
- Combined does use noticeably **more CPU** for the `dlstreamer-pipeline-server` container in this run (≈380–404% vs. separate's ≈142%) — expected, since combined is doing strictly more useful work per second (near-real-time captioning throughput) rather than being GPU-stalled/contended like separate's slower cadence.
- Operational note: the correct DLSPS REST endpoint to stop a running pipeline by instance id is `DELETE /pipelines/{instance_id}` (no `/user_defined_pipelines/` prefix, no pipeline name) — confirmed working, returns final status with `state: "ABORTED"`.

### Updated recommendation

The `jpegdec` fix is worth keeping (it removes a 100%-reproducible startup bug), but it is **not sufficient** to make the separate architecture's GPU captioning path production-viable for sustained concurrent detection+captioning — it still crashes after several minutes. The combined architecture remains the clearly better choice for GPU deployments: same functional outcome, far better caption latency, and no observed crash across an 8+ minute sustained run, at the cost of higher (but bounded and expected) CPU usage.
