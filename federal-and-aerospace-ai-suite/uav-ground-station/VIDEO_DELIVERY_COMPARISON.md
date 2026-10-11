<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Video Delivery Approach Comparison

Three approaches were evaluated for delivering the UAV host's RTSP video
(with burned-in detection/telemetry overlay) to a remote browser in this
ground-station viewer. This doc records the comparison and the rationale for
the final choice.

Context for the "MQTT-frames" column: Intel's
[SceneScape](https://github.com/open-edge-platform/scenescape) project's
design doc
(`docs/design/streaming-video-webrtc-ui.md`) documents its own experience
moving *away* from MQTT-based frame delivery *to* WebRTC, for reasons quoted
below — directly relevant prior art for this decision.

| | MQTT-frames (SceneScape's legacy design) | WebRTC + mediamtx (this project's first attempt) | MJPEG-over-HTTP (chosen) |
|---|---|---|---|
| **Mechanism** | Custom `gvapython` DLStreamer stage draws overlays, JPEG-encodes every frame, publishes to an MQTT topic; browser subscribes and decodes each JPEG into `<img>`/`<canvas>` | ffmpeg transcodes RTSP MJPEG → H264, republishes to mediamtx, browser negotiates WebRTC (ICE/DTLS/SRTP) via WHEP | ffmpeg re-rates/re-quality's the RTSP MJPEG, FastAPI streams its stdout as one long-lived `multipart/x-mixed-replace` HTTP response, browser `<img src>` renders it natively |
| **Needs custom DLStreamer overlay/encode stage** | Yes — UAV's pipeline doesn't have this; would need to be built from scratch | No | No |
| **New infrastructure required** | MQTT broker carries continuous binary image traffic | mediamtx **+ a TURN server (coturn)** for real-world NAT traversal (SceneScape's own proposed design adds this) | None — reuses the existing HTTP port |
| **Shared-broker impact** | High — image frames compete with telemetry/captions on the same broker | N/A | N/A |
| **Firewall/NAT robustness** | OK (same ports already open) | Poor without a TURN server — confirmed by a real remote-access failure in this project, and independently flagged in SceneScape's design doc | Excellent — a single HTTP port, no ICE/UDP needed |
| **Latency** | Worst — SceneScape's own doc cites "high latency due to MQTT protocol overhead" | Best, when it works (sub-100ms) | Middling (~150–400ms on LAN, dominated by the configured frame-rate cap, not protocol overhead) |
| **Complexity / fragility** | Medium | High — real bitstream corruption (sliced-threads, missing SPS/PPS) and ICE/host-advertisement bugs were hit and fixed in this project before concluding it wasn't worth the operational cost | Low |
| **SceneScape's own stated drawbacks (quoted)** | "High latency due to MQTT protocol overhead"; "Increased CPU and memory usage on the server side"; "Scalability issues with multiple concurrent video streams"; "not optimal for real-time video streaming due to the overhead of encoding/decoding and the limitations of MQTT for high-frequency data transmission" | "WebRTC is less reliable at delivering every single frame"; "WebRTC has more strict requirements for video format" | — |

## Decision

**MJPEG-over-HTTP**, for reasons specific to this project:

1. UAV's DLStreamer pipeline has no custom overlay/JPEG/MQTT-publish stage
   (it only publishes captions to MQTT) — adopting the MQTT-frames approach
   would mean building that stage from scratch, upstream, in a shared
   component.
2. Loading the shared MQTT broker with continuous binary video frames
   alongside telemetry/captions is exactly the scalability problem
   SceneScape's own design doc cites as its reason to move away from MQTT
   video.
3. WebRTC remains the lowest-latency option in theory, but reliable
   real-world remote access requires a TURN server (coturn) — more
   infrastructure than is justified for a single-viewer monitoring
   dashboard, and this project already hit (and fixed, at real engineering
   cost) ICE-host-advertisement and H264-bitstream corruption bugs before
   reaching that conclusion.
4. MJPEG-over-HTTP needs no new ports, no ICE/NAT traversal, and no codec
   negotiation — it reuses the single HTTP port already serving the
   dashboard, which is the primary constraint for this use case (an
   operator viewing from an arbitrary, possibly firewalled, remote machine).

## Why this fits this project's specific constraints

- **The primary requirement is remote reachability, not minimal latency.**
  This viewer exists specifically so an operator on a *different* machine
  (potentially behind a corporate firewall/NAT, as hit in practice during
  development) can see the feed. A single, already-open HTTP port beats any
  amount of raw speed that depends on UDP/ICE ports actually being reachable
  — a WebRTC stream that reliably delivers zero frames to a real remote
  client is strictly worse than a slower stream that always works.
- **Single viewer at a time.** The ground station is a one-operator
  dashboard, not a multi-tenant video service — the per-viewer-ffmpeg-process
  cost of MJPEG-over-HTTP (vs. mediamtx's one-relay-fans-out-to-N model)
  is irrelevant here.
- **The source is already MJPEG.** DLStreamer Pipeline Server's built-in
  RTSP destination is hardcoded to MJPEG (confirmed by reading its upstream
  source — see `/home/aicv-d1-l3t024/drone/edge-ai-libraries/microservices/dlstreamer-pipeline-server/RTSP_H264_ENCODING_PROPOSAL.md`
  for the full writeup). The WebRTC path required an *extra*, unnecessary
  MJPEG→H264 transcode purely to satisfy WebRTC's codec requirements, adding
  CPU cost and a second lossy re-compression for no quality benefit.
  MJPEG-over-HTTP re-encodes MJPEG→MJPEG (just down-sampling rate/quality),
  which is both cheaper and closer to a direct passthrough.
- **Acceptable, tunable latency for this use case.** Estimated end-to-end
  latency is ~150–400ms on a LAN, dominated by the configured frame-rate cap
  (10fps default) rather than protocol overhead — fine for situational-
  awareness monitoring (this viewer is not used for closed-loop remote
  piloting). `-r`/`-q:v` are exposed as environment variables so this can be
  tuned per deployment without code changes.
- **Lowest operational complexity for the gain.** Removes an entire
  container (mediamtx) and its config file, two extra published ports
  (WebRTC TCP + ICE UDP), and the whole class of bugs this project hit and
  had to fix (RTP MTU re-fragmentation, libx264 sliced-threads bitstream
  corruption, SPS/PPS not reaching WebRTC clients, ICE candidates
  advertising unreachable container-internal IPs to real remote clients).
