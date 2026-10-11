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

- The live video stream, in one of three selectable modes (see the dashboard's
  "Stream mode" selector):
  - **WebRTC - Direct** (default): iframes the remote UAV host's own native
    WHEP viewer page (`https://<host>/webrtc/<path>/`) directly -- no local
    relay, no transcoding. Only available while a `*_caption` pipeline
    variant (the only ones that attach a native WebRTC destination) is
    running on the remote host; the dashboard falls back to MJPEG otherwise.
  - **MJPEG**: the remote RTSP (M-JPEG) stream relayed to the browser as a
    plain HTTP `multipart/x-mixed-replace` stream rendered in an `<img>` --
    no WebRTC/ICE, no extra ports. See `VIDEO_DELIVERY_COMPARISON.md` for
    background.
  - **WebRTC - Relay** (legacy, opt-in -- requires `make up-relay`, see
    below): viz-backend transcodes the remote RTSP source to H.264 via a
    per-(host,device) ffmpeg relay, pushed into a local `mediamtx` sidecar
    which serves it to the browser over WebRTC/WHEP. Useful as a fallback
    when the remote pipeline has no native WebRTC destination (e.g. a base,
    non-caption pipeline).
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
├── docker-compose.yml          # 1 service: viz-backend (FastAPI UI + MJPEG
│                                # relay + WebRTC - Direct iframe support)
├── docker-compose-mediamtx.yml # OPT-IN overlay: adds the legacy
│                                # "WebRTC - Relay" mode (mediamtx + ffmpeg)
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
make up         # creates .env from template, builds, and starts the container
                # (WebRTC - Direct + MJPEG modes; no mediamtx/ffmpeg sidecar)
make up-relay   # same, plus the legacy WebRTC - Relay fallback mode
```

Then open `http://localhost:8080` (or `${VIZ_HOST_IP}:${VIZ_PORT}`), enter the
UAV host's IP (defaults to `127.0.0.1`), pick a device (`cpu`/`gpu`/`npu`), and
press **Connect**.

```bash
make down    # stop and remove the stack (both profiles)
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
  **"WebRTC - Direct" mode is the one exception**: the browser iframes the
  remote host's HTTPS page *directly* (not proxied through viz-backend), so
  it will show its own self-signed-certificate warning the first time --
  accept it once per browser (e.g. by visiting `https://<host>/` directly
  first) and it won't reappear.
- One ffmpeg subprocess is spawned per browser connection to
  `/api/stream/mjpeg` and torn down when that connection closes — fine for
  this single-viewer dashboard's use case.
- This project makes outbound, read-only requests to whatever host you enter;
  it never starts/stops pipelines on the remote stack.
