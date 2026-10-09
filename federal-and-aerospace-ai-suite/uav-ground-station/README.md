<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# UAV Ground Station

A minimal, single-container remote viewer for the
[`uav-vision-analytics`](../uav-vision-analytics) stack. Run it on a
**separate machine** — e.g. a ground-station laptop — from the one running
`docker-compose-pymavlink.yml`/`docker-compose-uavsdk.yml`. It never talks to
that host until you enter its IP in the dashboard and press **Connect**.

## What it shows

- The live RTSP (M-JPEG) stream, relayed to the browser as a plain HTTP
  `multipart/x-mixed-replace` MJPEG stream rendered in an `<img>` — no
  WebRTC/ICE, no extra ports, works through any firewall that already lets
  the dashboard's single HTTP port through. See `VIDEO_DELIVERY_COMPARISON.md`
  for why this replaced an earlier mediamtx/WebRTC relay.
- Scene captions (`uav/caption/<device>` over MQTT, bridged to the browser via
  SSE), shown under the video.
- Which pipeline/model is currently running for the selected device (from the
  DL Streamer Pipeline Server's REST API), including the VLM model path/device
  when captioning is enabled.
- CPU/GPU/NPU utilization, proxied from `metrics-manager`.
- A toggleable **Architecture** panel describing how the two machines connect.

## Project Structure

```text
uav-ground-station/
├── docker-compose.yml   # 1 service: viz-backend (FastAPI UI + MJPEG relay)
├── .env.example         # Copy to .env; all vars have loopback-safe defaults
├── VIDEO_DELIVERY_COMPARISON.md  # Why MJPEG-over-HTTP was chosen over WebRTC/MQTT
└── backend/             # FastAPI app: static UI + proxy/bridge APIs
    ├── main.py
    ├── requirements.txt
    ├── Dockerfile
    └── static/           # index.html, css/, js/, img/architecture.svg
```

## Quick Start

```bash
make up      # creates .env from template, builds, and starts the container
```

Then open `http://localhost:8080` (or `${VIZ_HOST_IP}:${VIZ_PORT}`), enter the
UAV host's IP (defaults to `127.0.0.1`), pick a device (`cpu`/`gpu`/`npu`), and
press **Connect**.

```bash
make down    # stop and remove the stack
make logs    # tail the container's logs
```

## Configuration

All configuration is via `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `VIZ_HOST_IP` | `127.0.0.1` | Bind address for this machine's published port |
| `VIZ_PORT` | `8080` | Dashboard port |
| `VIZ_MJPEG_FRAMERATE` | `10` | MJPEG relay frame rate (fps) — trade latency/bandwidth for smoothness |
| `VIZ_MJPEG_QUALITY` | `5` | MJPEG relay JPEG quality (ffmpeg `-q:v` scale, 1=best..31=worst) |
| `VIZ_DEFAULT_HOST` | `127.0.0.1` | Pre-filled value in the dashboard's Host field |
| `VIZ_REMOTE_HTTPS_PORT` | `443` | UAV host's nginx HTTPS port |
| `VIZ_REMOTE_RTSP_PORT` | `8555` | UAV host's nginx RTSP passthrough port |
| `VIZ_REMOTE_MQTT_PORT` | `1883` | UAV host's nginx MQTT passthrough port |

The remote-port defaults assume the standard nginx-published ports from
`uav-vision-analytics/docker-compose-pymavlink.yml`; only change them if that
stack's nginx is configured differently.

## Notes

- The UAV host's TLS certificate is self-signed; the backend skips
  verification when calling it (trusted-LAN tool posture, consistent with the
  `-k`/`--insecure` curl examples in `uav-vision-analytics`'s own README).
- One ffmpeg subprocess is spawned per browser connection to
  `/api/stream/mjpeg` and torn down when that connection closes — fine for
  this single-viewer dashboard's use case.
- This project makes outbound, read-only requests to whatever host you enter;
  it never starts/stops pipelines on the remote stack.
