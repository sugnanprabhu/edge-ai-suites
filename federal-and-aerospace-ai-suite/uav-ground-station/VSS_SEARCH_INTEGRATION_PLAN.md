<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Plan: Integrate VSS Natural-Language Search into UAV Ground Station

Status: **Implemented.** Phases 2–5 (proxy + popup), plus the minimal
`docker-compose-vss.yml` overlay (search shown only when it is used, `make
up-vss`) and Phase 6: all running UAV pipelines are auto-registered as VSS
live streams while connected and paused when they stop (`backend/vss_live_sync.py`).

Implementation notes (deviations from the original draft):

- `VSS_HOST` defaults to `host.docker.internal` (with `extra_hosts:
  host-gateway` on `viz-backend`), not `127.0.0.1`: inside the container,
  loopback is the container itself.
- Added `GET /api/search-frame` (proxies VSS `GET /manager/frames`) and a
  per-result `thumbnail_url`. Used as the `<video>` poster, and as the
  fallback when the browser can't decode a clip (MJPEG live segments).
- Results are also joined with `GET /manager/streams`, so live-stream hits
  show the stream name and capture time instead of a raw ID.
- UI lives in a separate `static/js/search.js`; `app.js` is unchanged.
- Validated end-to-end against a live VSS `--search` deployment (uploaded
  video + live `uav-mavlink-gpu` stream) with a headless browser.

## 1. Goal

Add a **"Search" button** to the existing "Search and Summarization" panel in
`uav-ground-station` (today a `Coming soon.` placeholder in
`backend/static/index.html`). Clicking it opens a **modal popup** containing:

1. A free-text prompt input ("describe what you're looking for").
2. A **Search** button that submits the prompt.
3. A results list rendered in the same popup: ranked clips with score,
   filename, time window, and an inline, seekable video preview.

Results are served by the **Video Search and Summarization (VSS)** sample
app's `search-ms` + `pipeline-manager` stack
(`sample-applications/video-search-and-summarization`), reached via its
`/manager/search/query` REST API. VSS's Docker Compose stack stays
**entirely separate** from `uav-ground-station/docker-compose.yml` — it is
its own compose project, started/stopped independently; `viz-backend` only
talks to it over HTTP, the same proxy pattern it already uses for the remote
UAV host and `gs-metrics-manager`.

## 2. What was inspected to ground this plan

### 2.1 `uav-ground-station` (the integration target)

- `docker-compose.yml` — one real service, `viz-backend` (FastAPI, port
  `8080`), plus `gs-metrics-manager`. `docker-compose-mediamtx.yml` is an
  optional second overlay for "H.264 Relay" mode (ports `8889`, `8189`).
  No port collides with VSS's defaults (`12345`, `7890`, `9777`).
- `backend/main.py` (FastAPI) — every existing feature is a **read-only HTTP
  proxy/bridge** to a remote host, configured via env vars with loopback-safe
  defaults (`DEFAULT_REMOTE_HOST`, `REMOTE_HTTPS_PORT`, etc.), e.g.:
  - `GET /api/pipeline-info` → `httpx` call to the remote Pipeline Server.
  - `GET /api/metrics-stream` → proxies metrics-manager's SSE stream.
  - `GET /api/captions-stream` → bridges MQTT → SSE for the browser.
  This is the exact pattern the new VSS search proxy should follow.
- `backend/static/index.html` — already has the target panel:
  ```html
  <section class="panel">
    <h2>Search and Summarization</h2>
    <p class="muted chart-subtitle">Coming soon.</p>
  </section>
  ```
- `backend/static/js/app.js` (648 lines, vanilla JS, no framework/build step)
  and `backend/static/css/styles.css` (304 lines). Existing "popup" precedent
  is the `#arch-panel` toggle (`classList.toggle("hidden")`), which is an
  inline panel, not a true overlay modal — a new overlay/dialog pattern is
  needed for the search popup.
- `.env.example` — all config is env-var driven with sane defaults; this is
  where new `VSS_*` variables belong.

### 2.2 VSS search API surface (the integration source)

Confirmed via the repo's own `vss-search-index` skill and source:

- Gateway prefix: `http://<vss-host>:<APP_HOST_PORT (default 12345)>/manager/...`
- Preflight: `GET /manager/health`, `GET /manager/app/features` (must report
  `search: "FEATURE_ON"` — only true for `--search`, `--dual`, `--unified`
  deployments).
- Query: `POST /manager/search/query`
  ```json
  { "query": "person wearing a hat", "tags": "indoor", "timeFilter": {"value": 7, "unit": "days"} }
  ```
  Response is **wrapped**: `{ "results": [ { "query_id", "results": [...] } ] }`.
  Each hit's `metadata` carries `relevance_score` (0..1), `video_id`,
  `video_url`, `segment_start`/`segment_end` (seconds), `seek_timestamp`,
  `tags`, `video_metadata`.
- Filenames are **not** in the search hit; join `metadata.video_id` against
  `GET /manager/videos` → `.videos[].dataStore.fileName`.
- Video playback: VSS's own React UI
  (`ui/react/src/redux/video/videoUrl.ts`) resolves a **directly playable,
  range-request-capable** URL as
  `GET /datastore/<bucket>/<video_id>/source.mp4`
  (nginx `location /datastore/` proxies to MinIO — confirmed in
  `config/nginx/nginx.conf`). `bucket_name` defaults to `video-summary`
  (`DEFAULT_BUCKET_NAME`) unless overridden. This is what the new modal's
  `<video>` tag should point at (through `viz-backend`, not directly, to
  avoid a third cross-origin host in the browser — see §4.2).
- Ingestion, for whichever videos should be searchable:
  - `POST /manager/videos` (multipart, field `video`) + `POST
    /manager/videos/search-embeddings/<id>` for pre-recorded clips, or
  - `POST /manager/streams` (`{"stream_url": "rtsp://...", "stream_name":
    "...", "tags": [...]}`) for **continuous** indexing of a live RTSP
    camera — available only in `--search`/`--dual` mode. This is directly
    relevant here: `uav-ground-station/backend/main.py` already knows the
    remote UAV host's live RTSP URL format,
    `rtsp://{host}:{REMOTE_RTSP_PORT}/{_frame_path(device)}` (see
    `relay_path()`, line ~534) — the same URL could, in principle, be
    registered with VSS so the live UAV feed itself becomes searchable
    (Phase 6 below — explicitly optional/stretch, not required for the core
    "button → prompt → popup" ask).
- Deployment: `setup.sh --search` plus the mode-specific env (`MINIO_*`,
  `POSTGRES_*`, `RABBITMQ_*`, `MULTIMODAL_EMBEDDING_MODEL`, etc. — see this
  session's earlier search bring-up). `docker/compose.search.yaml` shows the
  full service graph: `pipeline-manager`, `video-search` (search-ms),
  `multimodal-dataprep`, `vector-retriever`, `vdms-vector-db` (or Milvus),
  `minio-service`, `postgres-service`, `rabbitmq-service`,
  `multimodal-embedding-serving`, fronted by `nginx`.

### 2.3 Key design implication

VSS is a **multi-container stack with its own Compose lifecycle**
(`setup.sh`), not a single service. "Separate docker-compose" therefore means:
run VSS **unmodified, via its own `setup.sh --search`** (in its own checkout,
wherever that lives), and have `viz-backend` reach it purely over HTTP at
`http://<vss-host>:<APP_HOST_PORT>/manager/...` — exactly like it already
reaches the remote UAV host and `gs-metrics-manager`. **No compose file in
either repo should be merged**, and `uav-ground-station` does not need to
vendor or duplicate VSS's compose files at all; it only needs a documented
"bring up VSS search mode" step (pointing at the existing `vss-deploy` /
`vss-search-index` skills) plus a handful of env vars telling `viz-backend`
where that instance lives.

## 3. Scope decision needed before execution (ask the user)

Two reasonable scopes — recommend confirming with the user up front:

- **MVP (recommended first slice):** Search an **existing VSS video
  library** (whatever has already been uploaded/indexed into VSS,
  independent of the live UAV feed). Satisfies the literal ask ("click
  button → prompt → popup with results") with the least moving parts and no
  change to VSS itself.
- **Stretch (Phase 6, optional, separate follow-up):** Also register the
  **live UAV RTSP feed(s)** as a continuously-indexed VSS stream per device
  card, so operators can search footage from the drone session in near
  real time. This needs `--search` or `--dual` mode specifically (not
  `--unified`), network reachability from the VSS host to the UAV
  companion board's RTSP port, and a UI affordance per device card
  ("Enable search indexing").

This plan's steps below assume MVP first; Phase 6 is written as an optional
later phase so it can be deferred without blocking the main deliverable.

## 4. Architecture

```
Browser (ground-station dashboard)
   │  GET /api/search?query=...            (same-origin, proxied)
   │  GET /api/search-video?...            (range-request video proxy)
   ▼
viz-backend (FastAPI, uav-ground-station, port 8080)
   │  httpx → http://<VSS_HOST>:<VSS_PORT>/manager/search/query
   │  httpx → http://<VSS_HOST>:<VSS_PORT>/manager/videos
   │  httpx (streamed, Range-aware) → http://<VSS_HOST>:<VSS_PORT>/datastore/<bucket>/<video_id>/source.mp4
   ▼
VSS nginx gateway (separate Docker Compose project, its own setup.sh --search)
   → pipeline-manager (/manager) → search-ms / multimodal-dataprep / vector-retriever / vdms / minio
```

### 4.1 Why proxy through `viz-backend` rather than call VSS directly from the browser

- Consistent with every existing feature (pipeline-info, metrics, captions):
  the browser only ever talks to `viz-backend`; cross-host/cross-origin
  calls, TLS, and CORS are centralized in one place.
- VSS's gateway is HTTP by default (not behind self-signed TLS like the UAV
  host), so this is the simple case of the existing proxy pattern, no TLS
  edge cases.
- Keeps `VSS_HOST`/`VSS_PORT` as a single server-side config point instead of
  baking a second backend URL into client JS.

### 4.2 Video playback proxy

`GET /datastore/...` supports HTTP Range requests (needed for the `<video>`
scrubber). The new `viz-backend` route must forward the incoming `Range`
header to VSS and stream the response back with `Accept-Ranges`/
`Content-Range` preserved (`httpx.AsyncClient().stream(...)`, mirroring
existing SSE streaming code already in `main.py`) rather than buffering the
whole file.

## 5. Step-by-step breakdown

### Phase 1 — Confirm scope & environment (no code)

1.1. Confirm MVP vs. MVP+Phase 6 scope with the user (see §3).
1.2. Confirm where the VSS `--search` deployment will run: same host as
     `viz-backend`, or a separate host reachable by IP (mirrors the existing
     `VIZ_COMPANION_HOST` pattern for the UAV board). Determines whether
     `VSS_HOST` defaults to `127.0.0.1` or needs to be a required field.
1.3. Decide on authentication/network exposure: VSS's `/manager` API has no
   auth by default in this sample app — confirm this is acceptable for the
   intended network (trusted LAN), consistent with this repo's existing
   posture (self-signed TLS skip-verify, no auth on the remote proxy calls).

### Phase 2 — Backend: VSS search proxy endpoints (`viz-backend`)

New file `backend/vss_search.py` (keep `main.py` from growing further;
import and mount its router from `main.py`, consistent with the file's
existing single-module style — or add inline if the team prefers one file;
decide at implementation time) with:

2.1. **Config** (env vars, loopback-safe defaults, added next to the existing
     `DEFAULT_REMOTE_HOST`-style block in `main.py`):
   - `VSS_HOST` (default `127.0.0.1`)
   - `VSS_PORT` (default `12345`, i.e. `APP_HOST_PORT`)
   - `VSS_DATASTORE_BUCKET` (default `video-summary`, matches
     `DEFAULT_BUCKET_NAME`)
   - `VSS_SEARCH_TIMEOUT_SECONDS` (default e.g. `30`)

2.2. **`GET /api/search/status`** — calls `GET /manager/health` and
     `GET /manager/app/features`; returns `{reachable, search_enabled}` so
     the frontend can show a clear "VSS not reachable" / "search not
     enabled" state instead of a silent failure. Mirrors the skill's own
     preflight contract.

2.3. **`POST /api/search`** — body `{query, tags?, timeFilter?}`; forwards to
     `POST /manager/search/query`; on success, also calls `GET
     /manager/videos` and joins `video_id` → filename (per §2.2 contract)
     server-side so the frontend never has to make two calls or reimplement
     the join. Response shape returned to the browser (new, simplified
     contract — not a passthrough of VSS's wrapped shape):
     ```json
     {
       "results": [
         {
           "video_id": "...",
           "filename": "clip.mp4",
           "score": 0.83,
           "segment_start": 12.0,
           "segment_end": 22.0,
           "seek_timestamp": 14.5,
           "tags": ["indoor"],
           "video_proxy_url": "/api/search-video?video_id=...&bucket=..."
         }
       ]
     }
     ```
2.4. **`GET /api/search-video`** — query params `video_id`, optional
     `bucket` (default from config); streams
     `GET /datastore/<bucket>/<video_id>/source.mp4` from VSS, **forwarding
     the `Range` request header and the upstream's `Content-Range`/
     `Accept-Ranges`/`Content-Length`/`Content-Type` response headers** so
     the browser's native video scrubber works. Validate `video_id`/`bucket`
     against a safe-path-segment pattern before building the upstream URL
     (same defensive pattern as the VSS React UI's `SAFE_PATH_SEGMENT`
     regex) to avoid path-injection into the proxied request.
2.5. Error handling: VSS unreachable → `502` with a clear message (same style
     as existing `HTTPException` usage in `main.py`); empty result set is a
     normal `200` with `"results": []`, not an error (matches the skill's
     "empty result is valid" guidance).
2.6. Unit-test these routes the same way the project already tests `main.py`
     (check `backend/` for an existing test setup first — if none exists,
     keep this addition minimally testable with `httpx.MockTransport` rather
     than introducing a new test framework unprompted).

### Phase 3 — Frontend: Search button, modal, results

3.1. **`index.html`**: replace the `Coming soon.` placeholder with:
     ```html
     <section class="panel">
       <h2>Search and Summarization</h2>
       <p class="muted chart-subtitle">Search indexed video footage by natural-language description.</p>
       <button id="open-search-btn" class="btn btn-primary" type="button">Search</button>
     </section>
     ```
     and a new modal markup block (hidden by default), e.g.:
     ```html
     <div id="search-modal" class="modal-overlay hidden">
       <div class="modal-dialog">
         <header class="modal-header">
           <h2>Search Videos</h2>
           <button id="search-modal-close" class="btn btn-ghost" type="button">&times;</button>
         </header>
         <div class="modal-body">
           <form id="search-form">
             <input id="search-query-input" type="text" placeholder="e.g. person wearing a hat" autocomplete="off" />
             <button id="search-submit-btn" class="btn btn-primary" type="submit">Search</button>
           </form>
           <div id="search-status" class="muted"></div>
           <ul id="search-results-list" class="search-results-list"></ul>
         </div>
       </div>
     </div>
     ```
3.2. **`app.js`** additions (new, self-contained block, following the file's
     existing style of grabbing elements by id and wiring listeners near the
     top, e.g. alongside the `toggle-arch` wiring):
   - Open modal on `#open-search-btn` click; focus the input.
   - Close modal on `#search-modal-close`, backdrop click, and `Escape` key.
   - On form submit: disable the submit button, show a "Searching…" status,
     `fetch('/api/search', {method:'POST', body: JSON.stringify({query}), headers:{'Content-Type':'application/json'}})`.
   - Render each result as a list item: filename, score (rounded %), time
     window (`segment_start`–`segment_end`), a tags chip row, and a `<video
     controls>` whose `src` is the returned `video_proxy_url`, with
     `currentTime` set to `seek_timestamp` once metadata loads
     (`loadedmetadata` listener), matching how VSS's own UI seeks to the
     relevant moment rather than always playing from `0`.
   - Empty-results and VSS-unreachable (`502`/`network error`) states each
     get a distinct, user-visible message (no silent blank popup).
   - On open, call `GET /api/search/status` once; if `search_enabled` is
     false or VSS is unreachable, show a disabled/explained state in the
     modal (not just let the user submit into a guaranteed failure).

3.3. **`styles.css`** additions: `.modal-overlay` (fixed, full-viewport,
     semi-transparent backdrop, `display:flex` centering, toggled by
     `.hidden` — reuse the existing `.hidden { display: none; }` rule),
     `.modal-dialog` (card using the same panel/background/border variables
     already defined for `.panel`), `.search-results-list` (scrollable list,
     each item styled like existing `.chart-card`s), responsive video sizing
     inside list items. Keep to the existing dark theme's color variables
     rather than introducing a new palette.

### Phase 4 — Configuration wiring

4.1. **`backend/Dockerfile` / `requirements.txt`**: no new dependency
     expected — `httpx` is already a dependency (used throughout
     `main.py`); confirm and reuse.
4.2. **`docker-compose.yml`**: add `VSS_HOST`, `VSS_PORT`,
     `VSS_DATASTORE_BUCKET` to `viz-backend`'s `environment:` block, sourced
     from `.env`, same pattern as the existing `DEFAULT_REMOTE_HOST`/
     `REMOTE_HTTPS_PORT` vars. **No new service, network, or compose file is
     added here** — this keeps VSS's own Compose project fully separate, per
     the user's requirement.
4.3. **`.env.example`**: document the three new vars with the same
     commenting style as the existing file, explicitly noting "VSS is a
     separate Docker Compose stack (see
     `sample-applications/video-search-and-summarization`); these vars only
     tell viz-backend where to reach its already-running gateway."
4.4. **`README.md`**: add a "Search and Summarization" section (mirroring the
     existing "What it shows" bullets) describing the new panel, its
     prerequisites (a running VSS `--search`/`--dual`/`--unified`
     deployment, reachable at `VSS_HOST:VSS_PORT`, with search feature
     enabled and at least one indexed video), and a short manual
     verification recipe (`curl /api/search/status`, `curl -X POST
     /api/search -d '{"query":"..."}' `).

### Phase 5 — Testing & validation

5.1. Backend: unit tests for the new routes (success, empty results,
     VSS-down `502`, Range-header passthrough for the video proxy) — use
     whatever test runner/config already exists in `backend/`; if none,
     confirm with the user before introducing one.
5.2. Manual end-to-end: bring up VSS in `--search` mode (via the existing
     `vss-deploy` skill), upload/index at least one clip (via
     `vss-search-index` skill), bring up `uav-ground-station` pointed at
     that VSS instance, click **Search**, submit a prompt that matches the
     indexed clip, confirm the popup shows a ranked hit with a playable,
     seekable video.
5.3. Negative-path manual checks: VSS stopped (→ clear "unreachable"
     message, not a silent/broken popup), search-disabled VSS deployment
     (→ clear "search not enabled" message), empty-result query (→ "no
     results" message, not an error).
5.4. Confirm no change in behavior for any existing panel (`Architecture`
     toggle, companion-board/ground-station metrics charts, captions,
     video stream modes) — this feature is additive only.

### Phase 6 — Optional/stretch: live UAV feed as a searchable stream

Only pursue after Phases 1–5 are accepted and if the user wants near-real-time
search over the active drone feed, not just a pre-existing library:

6.1. Per device card, add an "Enable search indexing" toggle, visible only
     when the selected VSS deployment mode supports live streams
     (`--search`/`--dual` — reuse the existing `/api/pipeline-info` call's
     device/host context to build the stream URL
     `rtsp://{host}:{REMOTE_RTSP_PORT}/{frame_path}`, the same construction
     `relay_path()` already uses).
6.2. New `viz-backend` routes: `POST /api/search/streams` (wraps `POST
     /manager/streams`), `DELETE /api/search/streams/{id}`, `GET
     /api/search/streams` (list/poll state) — same proxy style as Phase 2.
6.3. Surface stream state (`pending`/`starting`/`running`/`reconnecting`/
     `error`) on the device card, and expose `last_error` on failure.
6.4. Explicitly document the caveat from the VSS skill: `stream_url` cannot
     be changed after creation (delete + recreate to repoint), and a `503`
     on create usually means the dataprep concurrency cap is reached.
6.5. This phase changes the "scope" of what's searchable but reuses the same
     `/api/search` and modal from Phase 2–3 unchanged — the modal does not
     need to know whether a given clip came from an upload or a live stream.

## 6. File-level change summary (for execution planning)

| File | Change |
|---|---|
| `backend/main.py` | Mount new search router (or add routes inline); add `VSS_*` config constants |
| `backend/vss_search.py` *(new)* | `/api/search/status`, `/api/search`, `/api/search-video` routes |
| `backend/static/index.html` | Replace "Coming soon." with Search button + modal markup |
| `backend/static/js/app.js` | Modal open/close, form submit, results rendering, video seek-on-load |
| `backend/static/css/styles.css` | `.modal-overlay`, `.modal-dialog`, `.search-results-list`, result-item styles |
| `docker-compose.yml` | Add `VSS_HOST`/`VSS_PORT`/`VSS_DATASTORE_BUCKET` to `viz-backend` env |
| `.env.example` | Document the three new vars |
| `README.md` | New "Search and Summarization" section + prerequisites |
| *(Phase 6 only)* `backend/main.py` / new module | Stream registration proxy routes + device-card toggle wiring in `app.js` |

No file under `sample-applications/video-search-and-summarization` is
modified by this plan — VSS is treated as an external, independently
deployed dependency reached purely over HTTP, satisfying the "separate
docker-compose" requirement by construction.

## 7. Open questions for the user (recommend resolving before execution)

1. MVP-only (search an existing library) or include Phase 6 (live-feed
   indexing) in the same pass?
2. Will the VSS `--search` deployment run on the same machine as
   `viz-backend`, or a separate reachable host (affects whether `VSS_HOST`
   needs a required, non-defaulted value and whether it should also be
   user-editable in the UI like `VIZ_COMPANION_HOST`)?
3. Any auth/network constraints beyond "trusted LAN, no auth" (matches this
   repo's current posture for the remote UAV host)?
4. Should search support the optional `tags`/`timeFilter` filters in the
   MVP modal, or keep the first pass to a single free-text prompt and add
   filters later?

## 8. Explicit non-goals

- Not modifying VSS's own UI, API, or Compose files.
- Not adding authentication to either stack (matches existing project
  posture; flag separately if this needs to change).
- Not building a generic VSS admin UI — only the single "search and view
  results" flow requested.
