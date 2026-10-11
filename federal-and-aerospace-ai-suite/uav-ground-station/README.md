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

- The live video stream, via **WebRTC - Direct**: the dashboard iframes the
  remote UAV host's own native WHEP viewer page
  (`https://<host>/webrtc/<path>/`) directly -- no local relay, no
  transcoding, no extra backend code. Only available while a `*_caption`
  pipeline variant (the only ones that attach a native WebRTC destination)
  is running on the remote host; otherwise the dashboard shows a
  placeholder message ("No native WebRTC — start this device with
  --caption."). See `LEGACY_STREAM_MODES.md` for the MJPEG/WebRTC-Relay
  fallbacks this project used to have instead, removed to keep the code
  minimal.
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
├── docker-compose.yml   # 1 service: viz-backend (FastAPI UI + proxy/bridge
│                        # APIs + WebRTC - Direct iframe support)
├── .env.example         # Copy to .env; all vars have loopback-safe defaults
├── VIDEO_DELIVERY_COMPARISON.md  # Why MJPEG-over-HTTP was once chosen over WebRTC/MQTT
├── LEGACY_STREAM_MODES.md  # How the removed MJPEG/WebRTC-Relay modes worked
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
UAV host's IP (defaults to `127.0.0.1`), and press **Connect**. Device cards
(`cpu`/`gpu`/`npu`) are detected automatically from whichever pipelines are
currently running on the remote host.

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
| `VIZ_COMPANION_HOST` | `127.0.0.1` | Pre-filled value in the dashboard's Companion Compute IP field |
| `VIZ_REMOTE_HTTPS_PORT` | `443` | UAV host's nginx HTTPS port |
| `VIZ_REMOTE_MQTT_PORT` | `1883` | UAV host's nginx MQTT passthrough port |

The remote-port defaults assume the standard nginx-published ports from
`uav-vision-analytics/docker-compose-pymavlink.yml`; only change them if that
stack's nginx is configured differently.

## Notes

- The UAV host's TLS certificate is self-signed; the backend skips
  verification when calling it (trusted-LAN tool posture, consistent with the
  `-k`/`--insecure` curl examples in `uav-vision-analytics`'s own README).
  The browser's direct WebRTC iframe is the one exception: it loads the
  remote host's HTTPS page *directly* (not proxied through viz-backend), so
  it will show its own self-signed-certificate warning the first time --
  accept it once per browser (e.g. by visiting `https://<host>/` directly
  first) and it won't reappear.
- This project makes outbound, read-only requests to whatever host you enter;
  it never starts/stops pipelines on the remote stack.
