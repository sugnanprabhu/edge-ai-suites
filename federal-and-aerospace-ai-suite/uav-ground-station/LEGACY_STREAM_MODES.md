<!--
SPDX-FileCopyrightText: (C) 2026 Intel Corporation
SPDX-License-Identifier: Apache-2.0
-->

# Legacy stream modes: MJPEG relay and WebRTC - Relay

**Status: removed.** As of the change that added this document,
`uav-ground-station` only supports **WebRTC - Direct** (the dashboard iframes
the remote UAV host's own native WHEP viewer page,
`https://<host>/webrtc/<path>/`, directly — no local relay, no transcoding,
no extra backend code). This file is a point-in-time reference snapshot of
how the two modes that were removed used to work, kept in case similar
functionality is ever needed again (e.g. to support a remote pipeline that
has no native WebRTC destination). It is **not** maintained going forward —
treat it as historical documentation, not living code.

See `VIDEO_DELIVERY_COMPARISON.md` for the original rationale comparing
MQTT-frames vs. WebRTC-relay vs. MJPEG (written when MJPEG replaced an
earlier WebRTC/mediamtx relay as the default; WebRTC - Relay was kept around
afterwards as an opt-in fallback until this removal).

## Why they existed

- **MJPEG** was the original default/fallback stream mode: the remote UAV
  host's pipeline server already outputs an RTSP M-JPEG stream by default
  (no VLM/caption pipeline attaches a WebRTC destination unless explicitly
  started in `_caption` mode). MJPEG-over-HTTP needed no extra container,
  no ICE/STUN/TURN, and no codec transcode — just an `<img>` tag.
- **WebRTC - Relay** was an opt-in fallback for when lower latency than
  MJPEG was wanted but the remote pipeline had no native WebRTC destination
  (e.g. a base/non-caption pipeline). It worked regardless of the remote
  pipeline's own RTSP encoding (MJPEG or H.264) because the relay's own
  ffmpeg process always transcoded to H.264 before publishing into the local
  `mediamtx` sidecar.

Once every supported pipeline variant started reliably attaching a native
WebRTC destination (the `*_caption` variants, see `_dest_webrtc_entry` in
`backend/main.py`), and the project settled on *only* running `_caption`
variants, both fallbacks stopped being needed — "WebRTC - Direct" alone
covers every case this dashboard is actually used for, and removing the
other two significantly shrinks the amount of code to maintain (no ffmpeg
subprocess management, no mediamtx sidecar, no per-(host,device) relay
lifecycle/reaper logic).

## MJPEG relay (`/api/stream/mjpeg`)

Relayed the remote host's RTSP (M-JPEG) output to the browser as a plain
HTTP `multipart/x-mixed-replace` stream — a native `<img src=...>` renders
this directly, no WebRTC/ICE/extra ports required. One ffmpeg subprocess was
spawned per browser request and torn down when the client disconnected.

### Env vars

| Variable | Default | Purpose |
|---|---|---|
| `MJPEG_FRAMERATE` | `10` | ffmpeg `-r` (fps) — trade latency/bandwidth for smoothness |
| `MJPEG_QUALITY` | `5` | ffmpeg `-q:v` (1=best..31=worst) |
| `MJPEG_READY_RETRIES` | `10` | retries waiting for the RTSP mount point to begin serving media |
| `MJPEG_READY_RETRY_INTERVAL` | `1.2` | seconds between retries |

### Backend (`backend/main.py`)

```python
async def _spawn_mjpeg_ffmpeg(source: str) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", source,
            # Source is already MJPEG -- this just re-rates/re-quality's it
            # (no format transcode), unlike the H264 relay this replaced.
            "-f", "mjpeg", "-q:v", MJPEG_QUALITY, "-r", MJPEG_FRAMERATE, "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg is not installed in the viz-backend container. "
            "Rebuild the image (e.g. 'make up' / 'docker compose up --build') "
            "so the Dockerfile's ffmpeg install step runs.",
        )


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


@app.get("/api/stream/mjpeg")
async def stream_mjpeg(host: str = Query(...), device: str = Query(..., pattern=DEVICE_PATTERN)):
    """Relay the remote host's RTSP (M-JPEG) output to the browser as a plain
    HTTP multipart/x-mixed-replace stream -- a native <img src=...> can render
    this directly, no WebRTC/ICE/extra ports required.

    One ffmpeg subprocess is spawned per browser request and torn down when
    the client disconnects (see the `finally` block in `generate()` below).
    """
    path_name = _frame_path(device)
    source = f"rtsp://{host}:{REMOTE_RTSP_PORT}/{path_name}"

    # Right after a pipeline (re)starts, the RTSP mount point can take 1-2s
    # to begin serving media. Retry a few times before giving up, so the
    # browser doesn't just see a single broken image.
    process: Optional[asyncio.subprocess.Process] = None
    first_chunk = b""
    for attempt in range(MJPEG_READY_RETRIES):
        process = await _spawn_mjpeg_ffmpeg(source)
        try:
            first_chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=MJPEG_READY_RETRY_INTERVAL)
        except asyncio.TimeoutError:
            first_chunk = b""
        if first_chunk:
            break
        await _terminate(process)
        if attempt < MJPEG_READY_RETRIES - 1:
            await asyncio.sleep(MJPEG_READY_RETRY_INTERVAL)
    else:
        raise HTTPException(
            status_code=504,
            detail=f"Timed out waiting for RTSP source {source} to become ready",
        )

    boundary = b"frame"

    async def generate():
        assert process is not None
        buf = bytearray(first_chunk)
        try:
            while True:
                # Flush any complete JPEG frames already buffered (each JPEG
                # starts with an SOI marker 0xFFD8 and ends with an EOI
                # marker 0xFFD9; ffmpeg's raw "-f mjpeg" output is just these
                # back-to-back with no framing of its own).
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start == -1:
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        break
                    end += 2
                    frame = bytes(buf[start:end])
                    del buf[:end]
                    yield (
                        b"--" + boundary + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        + frame + b"\r\n"
                    )
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                buf.extend(chunk)
        finally:
            await _terminate(process)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary.decode()}",
    )
```

### Frontend (`backend/static/js/app.js`)

```javascript
function startMjpegStream(host, card) {
  card.videoContainer.innerHTML = "";
  card.videoContainer.appendChild(card.video);
  card.video.src = "";
  card.video.onload = () => setStatus("connected");
  card.video.onerror = () => {
    card.captionStatus.textContent = "Connection failed: video stream could not be reached.";
  };
  const url = `/api/stream/mjpeg?host=${encodeURIComponent(host)}&device=${card.device}&_=${Date.now()}`;
  card.video.src = url;
}
```

The card's video element was a persistent `<img class="device-video">`
(swapped against an `<iframe>` only for the WebRTC modes).

## WebRTC - Relay (`/api/stream/webrtc-path`, `docker-compose-mediamtx.yml`)

Spawned a per-`(host, device)` ffmpeg subprocess that pulled the remote
host's RTSP source and *transcoded* it to H.264 (WebRTC has no MJPEG codec
support at all — confirmed via a real WHEP handshake against mediamtx: it
rejected MJPEG sources outright with `"the stream doesn't contain any
supported codec"`), publishing the result into a `mediamtx` sidecar
container via an internal RTSP push. mediamtx then served it to the browser
over WebRTC/WHEP. An idle-reaper background task killed relays that
mediamtx reported zero readers for too long (e.g. the browser tab was
closed or the stream mode was switched back to MJPEG).

### Env vars

| Variable | Default | Purpose |
|---|---|---|
| `MEDIAMTX_API_BASE` | `http://mediamtx:9997` | mediamtx's control API (internal docker network only) |
| `MEDIAMTX_RTSP_BASE` | `rtsp://mediamtx:8554` | internal RTSP push target for the ffmpeg relay |
| `MEDIAMTX_WEBRTC_PORT` | `8889` | published port the *browser* used for the WHEP/WebRTC handshake |
| `WEBRTC_RELAY_IDLE_TIMEOUT` | `30` | seconds with zero readers before a relay is torn down |
| `WEBRTC_RELAY_REAP_INTERVAL` | `10` | how often the reaper loop polled mediamtx's reader counts |
| `WEBRTC_RELAY_READY_RETRIES` | `15` | retries waiting for mediamtx to report the path ready after starting ffmpeg |
| `WEBRTC_RELAY_READY_RETRY_INTERVAL` | `1.0` | seconds between those retries |

### `docker-compose-mediamtx.yml` (opt-in overlay)

```yaml
services:
  mediamtx:
    image: bluenviron/mediamtx:1.21.2
    volumes:
      - ./configs/mediamtx.yml:/mediamtx.yml:ro
    environment:
      - MTX_WEBRTCADDITIONALHOSTS=${VIZ_PUBLIC_HOST:-}
    ports:
      - "${VIZ_HOST_IP:-127.0.0.1}:${MEDIAMTX_WEBRTC_PORT:-8889}:8889"
      - "${VIZ_HOST_IP:-127.0.0.1}:${MEDIAMTX_WEBRTC_ICE_UDP_PORT:-8189}:8189/udp"
    restart: on-failure:5

  viz-backend:
    depends_on:
      - mediamtx
```

Started with `docker compose -f docker-compose.yml -f docker-compose-mediamtx.yml up -d --build`
(`make up-relay`).

### `configs/mediamtx.yml`

```yaml
logLevel: info

authInternalUsers:
  - user: any
    pass:
    ips: []
    permissions:
      - action: api
      - action: read
      - action: publish
      - action: playback

api: yes
apiAddress: :9997

rtsp: yes
rtspAddress: :8554
rtspTransports: [tcp]

webrtc: yes
webrtcAddress: :8889
webrtcLocalUDPAddress: :8189
webrtcLocalTCPAddress: ""

rtmp: no
hls: no
srt: no

paths:
  all_others:
```

(`all_others` accepted a push-publish for any path name the ffmpeg relay
picked at runtime — one per `host+device`, sanitized, since the remote host
was only known once a user entered it in the dashboard.)

### Backend (`backend/main.py`)

```python
class _WebrtcRelay:
    """One per (host, device): an ffmpeg subprocess that pulls the remote
    RTSP source and republishes it (transcoded to H.264) into mediamtx via
    an internal RTSP push, so mediamtx can serve it to the browser over
    WebRTC/WHEP regardless of the remote pipeline's own RTSP encoding.
    """

    def __init__(self, path_name: str, source: str, process: asyncio.subprocess.Process):
        self.path_name = path_name
        self.source = source
        self.process = process
        self.last_had_reader = time.monotonic()


# Keyed by f"{host}:{device}"; guarded by _webrtc_lock since both the
# request handler and the background reaper task touch it.
_webrtc_relays: dict[str, _WebrtcRelay] = {}
_webrtc_lock = asyncio.Lock()


async def _mediamtx_path_ready(client: httpx.AsyncClient, path_name: str) -> bool:
    try:
        resp = await client.get(f"{MEDIAMTX_API_BASE}/v3/paths/get/{path_name}")
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        return bool(resp.json().get("ready"))
    except ValueError:
        return False


async def _mediamtx_reader_count(client: httpx.AsyncClient, path_name: str) -> Optional[int]:
    try:
        resp = await client.get(f"{MEDIAMTX_API_BASE}/v3/paths/get/{path_name}")
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return len(resp.json().get("readers") or [])
    except ValueError:
        return None


async def _spawn_webrtc_relay_ffmpeg(source: str, path_name: str) -> asyncio.subprocess.Process:
    try:
        return await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", source,
            # Transcode to H.264 regardless of the source codec (MJPEG or
            # already H.264) -- WebRTC has no MJPEG codec support at all, so
            # this step is what actually makes WebRTC mode work for the
            # MJPEG pipelines this dashboard defaults to.
            "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-g", "30",
            "-f", "rtsp", "-rtsp_transport", "tcp", f"{MEDIAMTX_RTSP_BASE}/{path_name}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="ffmpeg is not installed in the viz-backend container. "
            "Rebuild the image (e.g. 'make up' / 'docker compose up --build') "
            "so the Dockerfile's ffmpeg install step runs.",
        )


async def _ensure_webrtc_relay(host: str, device: str, source: str, path_name: str) -> None:
    """Start the ffmpeg relay for (host, device) if it isn't already running
    with the same source, then wait for mediamtx to report the path ready.
    """
    key = f"{host}:{device}"
    async with _webrtc_lock:
        existing = _webrtc_relays.get(key)
        if existing and existing.source == source and existing.process.returncode is None:
            existing.last_had_reader = time.monotonic()
            needs_start = False
        else:
            if existing:
                await _terminate(existing.process)
            process = await _spawn_webrtc_relay_ffmpeg(source, path_name)
            _webrtc_relays[key] = _WebrtcRelay(path_name, source, process)
            needs_start = True

    if not needs_start:
        return

    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        for attempt in range(WEBRTC_RELAY_READY_RETRIES):
            if await _mediamtx_path_ready(client, path_name):
                return
            await asyncio.sleep(WEBRTC_RELAY_READY_RETRY_INTERVAL)
    raise HTTPException(
        status_code=504,
        detail=f"Timed out waiting for WebRTC relay path '{path_name}' to become ready "
        f"(source={source}); check that the remote RTSP source is reachable.",
    )


async def _webrtc_reaper_loop() -> None:
    """Background task: stops ffmpeg relays that mediamtx has reported zero
    readers for longer than WEBRTC_RELAY_IDLE_TIMEOUT (e.g. the browser tab
    was closed or stream mode was switched back to MJPEG).
    """
    while True:
        await asyncio.sleep(WEBRTC_RELAY_REAP_INTERVAL)
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            async with _webrtc_lock:
                stale_keys = []
                for key, relay in _webrtc_relays.items():
                    count = await _mediamtx_reader_count(client, relay.path_name)
                    now = time.monotonic()
                    if count:
                        relay.last_had_reader = now
                    elif now - relay.last_had_reader > WEBRTC_RELAY_IDLE_TIMEOUT:
                        stale_keys.append(key)
                for key in stale_keys:
                    relay = _webrtc_relays.pop(key)
                    logger.info("webrtc-relay: reaping idle relay for %s (path=%s)", key, relay.path_name)
                    await _terminate(relay.process)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    reaper = asyncio.create_task(_webrtc_reaper_loop())
    try:
        yield
    finally:
        reaper.cancel()
        async with _webrtc_lock:
            for relay in _webrtc_relays.values():
                await _terminate(relay.process)
            _webrtc_relays.clear()


app = FastAPI(title="UAV Vision Analytics - Remote Viewer", lifespan=_lifespan)


@app.get("/api/stream/webrtc-path")
async def stream_webrtc_path(host: str = Query(...), device: str = Query(..., pattern=DEVICE_PATTERN)):
    """Ensures a per-(host,device) ffmpeg relay is running -- pulling the
    remote host's RTSP output and transcoding it to H.264 into a local
    mediamtx path -- so the browser can view it over WebRTC/WHEP instead of
    the default MJPEG-over-HTTP relay. Works regardless of the remote
    pipeline's own RTSP encoding (MJPEG or H.264).

    Returns the mediamtx path name and WebRTC port; the browser performs the
    actual WHEP handshake directly against mediamtx (not proxied through
    this backend), at http://<this-dashboard's-host>:<webrtc_port>/<path>/whep.
    """
    frame_path = _frame_path(device)
    source = f"rtsp://{host}:{REMOTE_RTSP_PORT}/{frame_path}"
    # One mediamtx path per (host, device) -- sanitized so the host's dots
    # don't collide with mediamtx's own path-name syntax.
    path_name = f"relay-{device}-{host.replace('.', '-')}"

    await _ensure_webrtc_relay(host, device, source, path_name)

    return {
        "path": path_name,
        "webrtc_port": MEDIAMTX_WEBRTC_PORT,
        "source": source,
    }
```

### Frontend (`backend/static/js/app.js`)

```javascript
async function startWebrtcRelayStream(host, card) {
  card.videoContainer.innerHTML = "";
  const placeholder = document.createElement("div");
  placeholder.className = "device-video-placeholder";
  placeholder.textContent = "Connecting (WebRTC - Relay)…";
  card.videoContainer.appendChild(placeholder);
  try {
    const res = await fetch(
      `/api/stream/webrtc-path?host=${encodeURIComponent(host)}&device=${card.device}`
    );
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      placeholder.textContent = `WebRTC - Relay setup failed: ${body.detail || res.status} ` +
        "(requires starting this stack with the docker-compose-mediamtx.yml overlay)";
      return;
    }
    const { path } = await res.json();
    const iframe = document.createElement("iframe");
    iframe.className = "device-video device-video-webrtc";
    iframe.allow = "autoplay";
    iframe.src = `http://${window.location.hostname}:${webrtcPort}/${path}/`;
    card.videoContainer.innerHTML = "";
    card.videoContainer.appendChild(iframe);
    setStatus("connected");
  } catch (err) {
    placeholder.textContent = `WebRTC - Relay setup failed: ${err}`;
  }
}
```

`webrtcPort` was populated from `/api/runtime-config`'s `webrtcPort` field
(`MEDIAMTX_WEBRTC_PORT`).

## Dashboard wiring (stream-mode selector)

`backend/static/index.html` had a 3-option `<select id="stream-mode-select">`
(`webrtc-direct` / `mjpeg` / `webrtc-relay`), and `app.js`'s `syncVideoMode()`
dispatched to whichever `start*Stream()` function matched the selection,
falling back from `webrtc-direct` to `mjpeg` automatically whenever the
running remote instance had no native WebRTC destination
(`info.webrtc_native === false`).

## Dockerfile / dependency footprint

Both modes required `ffmpeg` in the `viz-backend` image
(`apt-get install -y ffmpeg`); removing them also removed that dependency
(only `curl`, used to vendor Chart.js at build time, remains).

## Re-adding this in the future

If a pipeline variant without a native WebRTC destination needs to be
viewed again, the simplest path is likely to re-add just the MJPEG relay
(self-contained, no sidecar container) rather than the WebRTC - Relay mode
(which pulls in the `mediamtx` sidecar, an extra compose overlay, and
idle-reaper lifecycle management) — see the code above.
