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

- The live video stream, via **H.264 Relay**: the local viz-backend
  configures a mediamtx sidecar (`docker-compose-mediamtx.yml`, always
  brought up alongside the base stack -- see `make up`) to pull the remote
  UAV host's already-H.264 RTSP stream on demand and re-serve it to the
  browser as WebRTC (WHEP) -- pure remux, no transcoding. Requires the
  remote pipeline to be started with `--h264`; otherwise the dashboard
  shows a placeholder message ("No video stream — start this device with
  --h264.").
  Each card's expand button (or the player's own full-screen control)
  shows that feed full screen; Esc or "Exit full screen" returns.
- Scene captions (`uav/caption/<device>` over MQTT, bridged to the browser via
  SSE), shown under the video.
- Which pipeline/model is currently running for the selected device (from the
  DL Streamer Pipeline Server's REST API), including the VLM model path/device
  when captioning is enabled.
- CPU/GPU/NPU utilization, proxied from `metrics-manager`.
- **Live Natural Language Search**: the **Search Videos** button (top bar or side
  panel, or press `/`) opens a popup where you type a natural-language prompt
  (e.g. "a red car on the road") and get a ranked result list with thumbnails
  next to a clip player (arrow keys move between results, `Esc` closes), from a
  separately deployed
  [Video Search and Summarization (VSS)](https://github.com/open-edge-platform/edge-ai-libraries/tree/main/sample-applications/video-search-and-summarization)
  search stack -- see [VSS search](#vss-search) below.
- An interactive **Architecture** popup: the services on the UAV host, the ground station and the optional VSS stack, color-coded by origin (Open Edge Platform, open source, application). Pick a flow such as live video, captions, live indexing or search to highlight its path, click a service for its role, image and APIs, and see which models run on the NPU, GPU and CPU.

## Project Structure

```text
uav-ground-station/
├── docker-compose.yml   # viz-backend (FastAPI UI + proxy/bridge APIs)
├── docker-compose-mediamtx.yml  # mediamtx sidecar for H.264 Relay video (always used, see `make up`)
├── docker-compose-vss.yml  # optional VSS search containers (make up-vss)
├── .env.example         # Copy to .env; all vars have loopback-safe defaults
└── backend/             # FastAPI app: static UI + proxy/bridge APIs
    ├── main.py
    ├── vss_search.py    # /api/search* proxy to the separate VSS stack
    ├── requirements.txt
    ├── Dockerfile
    └── static/           # index.html, css/, js/ (app.js, search.js, arch.js), img/
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
| `VIZ_COMPANION_HOST` | `127.0.0.1` | Pre-filled value in the dashboard's UAV Companion Compute IP field |
| `VIZ_REMOTE_HTTPS_PORT` | `443` | UAV host's nginx HTTPS port |
| `VIZ_REMOTE_MQTT_PORT` | `1883` | UAV host's nginx MQTT passthrough port |
| `VSS_MINIO_USER` / `VSS_MINIO_PASSWORD` / `VSS_POSTGRES_USER` / `VSS_POSTGRES_PASSWORD` | _(required with `make up-vss`)_ | Credentials for the VSS overlay's MinIO and Postgres |
| `VSS_INGEST_EMBEDDING_DEVICE` / `VSS_DETECTION_DEVICE` / `VSS_QUERY_EMBEDDING_DEVICE` | `CPU` | Devices for VSS indexing, object detection and query embedding |
| `VSS_LIVE_SYNC_INTERVAL_SECONDS` | `15` | How often running UAV pipelines are synced to VSS live streams |
| `VSS_DATASTORE_BUCKET` | `video-summary` | VSS MinIO bucket for uploaded videos |
| `VSS_SEARCH_TIMEOUT_SECONDS` | `60` | Timeout for one search request to VSS |

The remote-port defaults assume the standard nginx-published ports from
`uav-vision-analytics/docker-compose-pymavlink.yml`; only change them if that
stack's nginx is configured differently.

## VSS search

The **Live Natural Language Search** panel and **Search Videos** popup appear
only when the VSS overlay is used. It is a separate compose file,
`docker-compose-vss.yml`, that adds just the 8 containers VSS search needs
(pipeline-manager, video-search, multimodal-dataprep,
multimodal-embedding-serving, vector-retriever, vdms-vector-db, postgres,
minio). With the default `make up` / `make up-relay` the search UI is hidden
and the search routes return 404.

1. Set the required `VSS_MINIO_*` / `VSS_POSTGRES_*` credentials (and,
   optionally, devices) in `.env` -- see `.env.example`.
2. `make up-vss` (base + mediamtx + VSS overlay). The first start downloads
   the CLIP embedding model (and YOLOX, for object crops) into Docker volumes.
3. Open the dashboard and **Connect** to the UAV. Every RUNNING pipeline
   (`uav-mavlink-cpu/gpu/npu` on `REMOTE_RTSP_PORT`) is registered with VSS
   as a live stream and indexed continuously. A backend loop
   (`VSS_LIVE_SYNC_INTERVAL_SECONDS`) adds pipelines that start later and
   pauses ones that stop; **Disconnect** pauses all of them. Indexed footage
   is kept for `VSS_LIVE_RETENTION_HOURS`.

`make down-vss` stops everything but keeps the index; `make clean-vss-index`
also deletes the indexed footage, embeddings and stream registrations but
keeps the downloaded models; `make delete-vss-volumes` (asks for confirmation;
`FORCE=1` skips it) deletes all VSS volumes, including the models
(re-downloaded on the next start). The pipeline-manager API is published on
`VIZ_HOST_IP:VSS_MANAGER_PORT` (default 3001) for uploading videos
(`POST /videos`, then `POST /videos/search-embeddings/<videoId>`).

Live RTSP ingestion needs the `intel/*:2026.3.0-rc1` (or newer) VSS images
(`VSS_REGISTRY` / `VSS_TAG`). To use a VSS
deployed separately with its own `setup.sh --search` instead, set
`VSS_ENABLED=true` and `VSS_MANAGER_URL` / `VSS_DATASTORE_URL` in `.env`.

`viz-backend` proxies everything so the browser only talks to this dashboard:

| Route | Upstream VSS call | Purpose |
|---|---|---|
| `GET /api/search/status` | `GET /manager/health`, `GET /manager/app/features` | Reachability + search feature flag (popup status pill) |
| `POST /api/search` `{"query": "..."}` | `POST /manager/search/query`, `GET /manager/videos`, `GET /manager/streams` | Ranked hits, joined with filenames / live stream names |
| `GET /api/search-video?path=<bucket>/<object>` | `GET /datastore/<bucket>/<object>` | Range-aware clip streaming (seekable `<video>`) |
| `GET /api/search-frame?video_id=...&timestamp=...` | `GET /manager/frames` | Best-matching frame (thumbnail / fallback) |
| `GET/POST/DELETE /api/search/live-sync` | `GET/POST/PATCH/DELETE /manager/streams` | Show / start (`{"host": ...}`) / stop live indexing of the UAV feeds |

Quick check:

```bash
curl -s http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/search/status
curl -s -X POST http://${VIZ_HOST_IP:-127.0.0.1}:${VIZ_PORT:-8080}/api/search \
  -H 'Content-Type: application/json' -d '{"query": "a car on the road"}'
```

Clips whose codec the browser can't decode (for example MJPEG live-stream
segments from pipelines run without `--h264`) fall back to VSS's
server-decoded best-matching frame plus a download link. Live segments older
than VSS's `LIVE_RETENTION_HOURS` may no longer be available.

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
