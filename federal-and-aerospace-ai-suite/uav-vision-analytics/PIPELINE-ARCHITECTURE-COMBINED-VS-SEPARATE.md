<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Combined vs Separate Captioning Pipeline — Architecture Diagrams

Visual companion to [`BENCHMARK-COMBINED-VS-SEPARATE.md`](./BENCHMARK-COMBINED-VS-SEPARATE.md).
Shows the full GStreamer element graph for both architectures, including the
**hidden RTSP-mount pipeline** that DL Streamer Pipeline Server (DLSPS) builds
automatically for any pipeline whose destination is `type: rtsp` — it is not
present in `config-combined.json` and only appears in DLSPS runtime logs.

## Branches

| Branch | Architecture |
|---|---|
| `feature/uav-lvc-integration` | **Combined** — single GStreamer pipeline, `tee`'d after one decode, fans raw frames to both `gvadetect` and `gvagenai` in-process. |
| `feature/lvc-only-tr` | **Separate** — detection pipeline publishes RTSP (via DLSPS's auto-built mount); an independent captioning pipeline (launched by `video-caption-service`) connects back as an RTSP client. |

## Separate architecture — 3 pipelines stitched together by RTSP

```mermaid
flowchart TD
    subgraph P1["Pipeline 1 — Detection (uav_object_detection_gpu)"]
        A1[multifilesrc] --> A2[h264parse] --> A3[decodebin3] --> A4["gvadetect<br/>device=GPU"]
        A4 --> A5["gvapython<br/>overlay"] --> A6[gvametaconvert] --> A7[gvametapublish] --> A8[gvafpscounter] --> A9[appsink]
    end

    subgraph P1B["HIDDEN Pipeline 1b — DLSPS auto-built RTSP mount (not in config JSON)"]
        B1[appsrc] --> B2[videoconvert] --> B3["gvawatermark<br/>(2nd overlay pass)"]
        B3 --> B4["🔴 jpegenc 🔴<br/>re-encode every frame"]
        B4 --> B5["🔴 rtpjpegpay 🔴<br/>RTP packetize"]
        B5 --> B6["RTSP server<br/>:8555/path"]
    end

    A9 -.frame.-> B1
    B6 ==>|"🔴 network / TCP socket 🔴"| C1

    subgraph P2["Pipeline 2 — Captioning (Video_Captioning_RTSP_Hardware_Default_Resolution)"]
        C1["rtspsrc<br/>latency=100ms"] --> C2["🔴 rtpjpegdepay 🔴"] --> C3["🔴 vajpegdec 🔴"] --> C4[vapostproc] --> C5{tee}
        C5 --> C6[queue] --> C7[videorate] --> C8["vapostproc<br/>(2nd convert)"] --> C9["gvagenai<br/>device=GPU<br/>NEW OpenVINO context"]
        C9 --> C10[gvametaconvert] --> C11["gvapython<br/>MQTT publish"] --> C12[fakesink]
        C5 --> C13[queue] --> C14[vapostproc] --> C15["appsink<br/>(stats only)"]
    end

    style B4 fill:#f66,color:#000
    style B5 fill:#f66,color:#000
    style C2 fill:#f66,color:#000
    style C3 fill:#f66,color:#000
    style C8 fill:#faa,color:#000
    style C9 fill:#faa,color:#000
```

🔴 = pure overhead that exists **only** because of the RTSP hop between two
independent pipeline instances; all of it disappears in the combined
architecture. 🟠 (lighter red) = duplicated work (2nd colorspace conversion,
2nd independent GPU/OpenVINO context) caused by the same split.

## Combined architecture — 1 pipeline, 1 process

```mermaid
flowchart TD
    subgraph P["Single pipeline — uav_object_detection_gpu_caption"]
        D1[multifilesrc] --> D2[h264parse] --> D3["decodebin3<br/>(decoded ONCE)"] --> D4{tee}

        D4 --> D5[queue] --> D6["gvadetect<br/>device=GPU"] --> D7["gvapython<br/>overlay"] --> D8[gvametaconvert] --> D9[gvametapublish] --> D10[gvafpscounter] --> D11[appsink]

        D4 --> D12[queue] --> D13[videoconvert] --> D14[videorate] --> D15[videoscale] --> D16["gvagenai<br/>device=GPU<br/>SAME process/context as gvadetect"]
        D16 --> D17[gvametaconvert] --> D18["gvapython<br/>MQTT publish"] --> D19[fakesink]
    end

    style D16 fill:#9f9,color:#000
    style D4 fill:#9cf,color:#000
```

No `jpegenc`, no `rtpjpegpay`, no RTSP hop, no `rtpjpegdepay`, no `vajpegdec`,
no second `vapostproc`, no second OpenVINO/GPU context. Raw `BGRA` frames fan
out via `tee` — zero-copy, in-process.

## What collapses away

| Stage present only in Separate | Removed in Combined because |
|---|---|
| `appsrc → videoconvert` (RTSP mount re-ingest) | No re-ingest — `tee` shares the one decoded frame in-process |
| 2nd `gvawatermark` overlay pass | Overlay only needs to happen once, on the single shared frame |
| `jpegenc` (full JPEG compression, every frame) | No serialization boundary to cross |
| `rtpjpegpay` → RTSP/TCP → `rtpjpegdepay` (+ 100 ms jitter buffer) | No network hop between detection and captioning |
| `vajpegdec` (full JPEG decode, every frame) | Frame is never re-encoded, so never needs re-decoding — also removes the `vajpegdec`/`jpegenc` codec-mismatch bug class entirely |
| 2nd `vapostproc` colorspace conversion | Conversion happens once (`videoconvert`/`videoscale`) before the `tee` |
| 2nd, independent OpenVINO/GPU context (`gvagenai` in its own pipeline) | `gvadetect` and `gvagenai` share one pipeline/context — avoids the `CL_OUT_OF_RESOURCES` → `CL_INVALID_EVENT` cascade seen when both pipelines contend for GPU memory independently |
| Two separately-managed pipeline lifecycles + shared-RTSP-destination `FLUSHING` stall bug class | Structurally impossible with only one pipeline instance |

**Net effect:** per-frame cost drops from *decode → detect → re-encode →
network → re-decode → convert → infer* down to *decode → tee → {detect,
infer}* — one decode, one conversion, one GPU context, zero network hops.
