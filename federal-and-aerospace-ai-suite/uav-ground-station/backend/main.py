# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Remote viewer backend for the UAV Vision Analytics stack.

Runs on a machine *separate* from the UAV stack. Given the IP/hostname of a
host already running the uav-vision-analytics stack (docker-compose-pymavlink
or docker-compose-uavsdk, with nginx publishing its usual ports), this
service:

  * Looks up which DL Streamer pipeline/model is currently serving a given
    device's RTSP path, via the Pipeline Server's REST API (through nginx).
  * Proxies the metrics-manager SSE stream so the browser never needs direct,
    cross-origin, self-signed-TLS access to the remote host.
  * Bridges MQTT scene captions (topic uav/caption/<device>) to the browser
    over SSE.
  * Relays the remote host's RTSP output (M-JPEG, the default) to the
    browser as an in-browser-native MJPEG stream (multipart/x-mixed-replace
    over plain HTTP) via a short-lived, per-viewer ffmpeg subprocess — no
    WebRTC/ICE, no extra ports, no codec transcode (see
    VIDEO_DELIVERY_COMPARISON.md for why this replaced an earlier
    mediamtx/WebRTC relay).
  * "WebRTC - Direct" stream mode (default whenever the running remote
    pipeline attached a native WebRTC destination, i.e. a *_caption variant
    -- see _dest_webrtc_entry / pipeline_info's "webrtc_native"): the
    dashboard simply iframes the remote UAV host's own
    https://<host>/webrtc/<frame_path>/ WHEP viewer page directly. No
    transcoding, no local mediamtx, no extra backend code at all -- the
    remote stack already serves real, low-latency WebRTC on its own
    (see uav-vision-analytics's nginx/mediamtx/coturn setup).
  * "WebRTC - Relay" stream mode (legacy, opt-in -- requires running with
    docker-compose-mediamtx.yml as an extra -f overlay): spawns a per-
    (host,device) ffmpeg relay that pulls the remote RTSP source and
    *transcodes* it to H.264 (WebRTC has no MJPEG codec support at all --
    confirmed via a real WHEP handshake against mediamtx: it rejects MJPEG
    sources outright with "the stream doesn't contain any supported codec"),
    publishing the result into the `mediamtx` sidecar container via an
    internal RTSP push; mediamtx then serves it to the browser over
    WebRTC/WHEP. Works regardless of the remote pipeline's own RTSP
    encoding -- useful as a fallback when the remote pipeline has no native
    WebRTC destination (e.g. a base, non-caption pipeline). See
    /api/stream/webrtc-path and _WebrtcRelay below.

Only ever makes outbound, read-only requests to the user-supplied host; it
never starts/stops pipelines on the remote stack.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("viz-backend")

DEFAULT_REMOTE_HOST = os.environ.get("DEFAULT_REMOTE_HOST", "127.0.0.1")
REMOTE_HTTPS_PORT = int(os.environ.get("REMOTE_HTTPS_PORT", "443"))
REMOTE_RTSP_PORT = int(os.environ.get("REMOTE_RTSP_PORT", "8555"))
REMOTE_MQTT_PORT = int(os.environ.get("REMOTE_MQTT_PORT", "1883"))

# MJPEG relay tuning (see /api/stream/mjpeg below). Lower MJPEG_FRAMERATE /
# raise MJPEG_QUALITY (1=best..31=worst, ffmpeg -q:v scale) to trade latency
# and bandwidth for image quality, or vice versa.
MJPEG_FRAMERATE = os.environ.get("MJPEG_FRAMERATE", "10")
MJPEG_QUALITY = os.environ.get("MJPEG_QUALITY", "5")
MJPEG_READY_RETRIES = int(os.environ.get("MJPEG_READY_RETRIES", "10"))
MJPEG_READY_RETRY_INTERVAL = float(os.environ.get("MJPEG_READY_RETRY_INTERVAL", "1.2"))

# Opt-in WebRTC relay (see /api/stream/webrtc-path and _WebrtcRelay below).
# MEDIAMTX_API_BASE / MEDIAMTX_RTSP_BASE are only ever reached over the
# internal docker-compose network (never published to the host);
# MEDIAMTX_WEBRTC_PORT is the published port the *browser* uses directly for
# the WHEP/WebRTC handshake.
MEDIAMTX_API_BASE = os.environ.get("MEDIAMTX_API_BASE", "http://mediamtx:9997")
MEDIAMTX_RTSP_BASE = os.environ.get("MEDIAMTX_RTSP_BASE", "rtsp://mediamtx:8554")
MEDIAMTX_WEBRTC_PORT = int(os.environ.get("MEDIAMTX_WEBRTC_PORT", "8889"))
# How long (seconds) a WebRTC relay is kept alive after mediamtx last
# reported zero readers on its path, before the ffmpeg transcoder is killed
# and the path torn down. Checked by _webrtc_reaper_loop() below.
WEBRTC_RELAY_IDLE_TIMEOUT = float(os.environ.get("WEBRTC_RELAY_IDLE_TIMEOUT", "30"))
WEBRTC_RELAY_REAP_INTERVAL = float(os.environ.get("WEBRTC_RELAY_REAP_INTERVAL", "10"))
WEBRTC_RELAY_READY_RETRIES = int(os.environ.get("WEBRTC_RELAY_READY_RETRIES", "15"))
WEBRTC_RELAY_READY_RETRY_INTERVAL = float(os.environ.get("WEBRTC_RELAY_READY_RETRY_INTERVAL", "1.0"))

DEVICE_PATTERN = "^(cpu|gpu|npu)$"

# Fallback display values when a running instance didn't echo "parameters" in
# its REST request (i.e. it relied on the pipeline's own schema defaults) —
# mirrors configs/config-pymavlink.json on the remote host.
DEFAULT_CAPTION_PARAMS = {
    "cpu": {
        "model_path": "/home/pipeline-server/resources/ov_models/cpu/InternVL2-1B",
        "model_device": "CPU",
        "max_new_tokens": 50,
    },
    "gpu": {
        "model_path": "/home/pipeline-server/resources/ov_models/gpu/InternVL2-1B",
        "model_device": "GPU",
        "max_new_tokens": 50,
    },
    "npu": {
        "model_path": "/home/pipeline-server/resources/ov_models/cpu/InternVL2-1B",
        "model_device": "CPU",
        "max_new_tokens": 50,
    },
}


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


def _remote_base(host: str) -> str:
    return f"https://{host}:{REMOTE_HTTPS_PORT}"


def _frame_path(device: str) -> str:
    return f"uav-mavlink-{device}"


def _dest_frame_for_path(destination: dict, frame_path: str) -> Optional[dict]:
    """Return the destination.frame entry matching frame_path.

    destination.frame is a single dict for RTSP-only pipelines, but a *list*
    of dicts (one per transport) for the combined pipeline's WebRTC-enabled
    variant (see uav-vision-analytics's _build_rtsp_caption_payload) -- this
    normalizes both shapes so callers don't need to know which one a given
    running instance used.
    """
    if not isinstance(destination, dict):
        return None
    frame = destination.get("frame")
    if isinstance(frame, dict):
        return frame if frame.get("path") == frame_path else None
    if isinstance(frame, list):
        for entry in frame:
            if isinstance(entry, dict) and entry.get("path") == frame_path:
                return entry
    return None


def _dest_frame_paths(destination: dict) -> list:
    """All destination paths present in destination.frame, regardless of
    whether it's a single dict (RTSP-only) or a list (RTSP+WebRTC combined
    variant) -- see _dest_frame_for_path for the shape rationale."""
    if not isinstance(destination, dict):
        return []
    frame = destination.get("frame")
    if isinstance(frame, dict):
        return [frame.get("path")]
    if isinstance(frame, list):
        return [entry.get("path") for entry in frame if isinstance(entry, dict)]
    return []


def _dest_webrtc_entry(destination: dict, frame_path: str) -> Optional[dict]:
    """Return the destination.frame entry (if any) for the remote pipeline
    server's *native* WebRTC AppDestination matching frame_path.

    Unlike the RTSP entry, a WebRTC entry is keyed by 'peer-id' (not 'path')
    -- see uav-vision-analytics's uav_pipeline_control.sh /
    mavlink_pipeline_manager.py, which attach it as:
        {"type": "webrtc", "peer-id": "<frame_path>"}
    only alongside the *_caption pipeline variants (the base cpu/gpu/npu
    pipelines only attach an RTSP destination). Used to tell the dashboard
    whether "WebRTC - Direct" mode (consuming the remote nginx's own
    /webrtc/<frame_path>/ WHEP endpoint directly, no local relay) is
    actually available for the currently running instance.
    """
    if not isinstance(destination, dict):
        return None
    frame = destination.get("frame")
    entries = frame if isinstance(frame, list) else ([frame] if isinstance(frame, dict) else [])
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "webrtc" and entry.get("peer-id") == frame_path:
            return entry
    return None


def _pipeline_identity(pipeline: dict) -> Optional[str]:
    """The REST API's request.pipeline.name is always the generic grouping
    name ("user_defined_pipelines"), not the actual pipeline that was
    started -- that's request.pipeline.version instead. Prefer version,
    falling back to name for older/unexpected response shapes.
    """
    return pipeline.get("version") or pipeline.get("name")


KNOWN_DEVICES = ["cpu", "gpu", "npu"]
_FRAME_PATH_TO_DEVICE = {_frame_path(d): d for d in KNOWN_DEVICES}


@app.get("/api/health")
async def health():
    return {"status": "ok", "ffmpeg_available": shutil.which("ffmpeg") is not None}


@app.get("/api/runtime-config")
async def runtime_config():
    return {
        "defaultHost": DEFAULT_REMOTE_HOST,
        "devices": ["cpu", "gpu", "npu"],
        "webrtcPort": MEDIAMTX_WEBRTC_PORT,
        # Port the remote UAV host's nginx publishes HTTPS on -- used by the
        # dashboard to build the direct WebRTC viewer URL
        # (https://<host>:<remoteHttpsPort>/webrtc/<frame_path>/), i.e. the
        # remote stack's own native WHEP endpoint, with no local relay.
        "remoteHttpsPort": REMOTE_HTTPS_PORT,
    }


@app.get("/api/pipeline-info")
async def pipeline_info(host: str = Query(...), device: str = Query(..., pattern=DEVICE_PATTERN)):
    """Report which pipeline/model is currently serving the given device's RTSP path."""
    frame_path = _frame_path(device)
    base = _remote_base(host)
    info = {
        "reachable": False,
        "device": device,
        "rtsp_path": frame_path,
        "caption_topic": f"uav/caption/{device}",
        "pipeline_name": None,
        "state": None,
        "captioning": False,
        "model_path": None,
        "model_device": None,
        "max_new_tokens": None,
        "frame_rate": None,
        "source_of_truth": None,
        # "mjpeg" (default) or "h264" -- read from the running instance's own
        # request body (destination.frame.encoding), same default the remote
        # pipeline server itself applies when the field is omitted. Used by
        # the dashboard to grey out the WebRTC stream-mode toggle, since
        # WebRTC has no MJPEG codec support (see /api/stream/webrtc-path).
        "encoding": "mjpeg",
        # True when the running instance attached a native WebRTC
        # AppDestination (peer-id=frame_path) on the remote pipeline server
        # -- only the *_caption pipeline variants do this (see
        # _dest_webrtc_entry above). Used by the dashboard to grey out
        # "WebRTC - Direct" mode when it isn't actually available.
        "webrtc_native": False,
    }
    try:
        # Self-signed TLS on the remote nginx (see configs/nginx/ssl on that
        # host) — trusted-LAN tool, same posture as the -k curl examples in
        # the main README.
        async with httpx.AsyncClient(verify=False, timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"{base}/pipelines/status")
            resp.raise_for_status()
            instances = resp.json()
            info["reachable"] = True
            if isinstance(instances, list):
                # Multiple instances can target the same destination path
                # (e.g. a stopped/aborted run plus a newer running one).
                # Prefer RUNNING instances, then the most recently started,
                # so we don't report stale/dead pipeline info.
                instances = sorted(
                    instances,
                    key=lambda inst: (
                        0 if inst.get("state") == "RUNNING" else 1,
                        -(inst.get("start_time") or 0),
                    ),
                )
                for inst in instances:
                    inst_id = inst.get("id")
                    if inst_id is None:
                        continue
                    detail_resp = await client.get(f"{base}/pipelines/{inst_id}")
                    if detail_resp.status_code != 200:
                        continue
                    detail = detail_resp.json()
                    request = detail.get("request") or {}
                    destination = request.get("destination") or {}
                    frame_dest = _dest_frame_for_path(destination, frame_path)
                    if frame_dest is None:
                        continue

                    info["webrtc_native"] = _dest_webrtc_entry(destination, frame_path) is not None

                    pipeline = request.get("pipeline") or {}
                    params = request.get("parameters") or {}
                    name = _pipeline_identity(pipeline)
                    captioning = bool(name) and "caption" in name

                    info["pipeline_name"] = name
                    info["state"] = inst.get("state")
                    info["captioning"] = captioning
                    info["frame_rate"] = params.get("frame_rate")
                    info["encoding"] = str(frame_dest.get("encoding", "mjpeg")).lower()

                    if captioning:
                        defaults = DEFAULT_CAPTION_PARAMS.get(device, {})
                        info["model_path"] = params.get("captioner_model_path") or defaults.get("model_path")
                        info["model_device"] = params.get("captioner_device") or defaults.get("model_device")
                        info["max_new_tokens"] = (
                            params.get("captioner_max_new_tokens") or defaults.get("max_new_tokens")
                        )
                        info["source_of_truth"] = (
                            "overridden" if "captioner_model_path" in params else "default"
                        )
                    break
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("pipeline-info lookup failed for host=%s device=%s: %s", host, device, exc)
    return info


@app.get("/api/detect-pipeline")
async def detect_pipeline(host: str = Query(...)):
    """Auto-detect which device (cpu/gpu/npu) currently has a RUNNING pipeline.

    Used so the browser doesn't need the user to pick a device manually, and
    so it can notice (by re-polling) when the running pipeline changes --
    e.g. an operator stops one device's pipeline and starts another's.
    """
    base = _remote_base(host)
    result = {"reachable": False, "device": None, "pipeline_name": None, "state": None}
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"{base}/pipelines/status")
            resp.raise_for_status()
            instances = resp.json()
            result["reachable"] = True
            if isinstance(instances, list):
                instances = sorted(
                    instances,
                    key=lambda inst: (
                        0 if inst.get("state") == "RUNNING" else 1,
                        -(inst.get("start_time") or 0),
                    ),
                )
                for inst in instances:
                    if inst.get("state") != "RUNNING":
                        continue
                    inst_id = inst.get("id")
                    if inst_id is None:
                        continue
                    detail_resp = await client.get(f"{base}/pipelines/{inst_id}")
                    if detail_resp.status_code != 200:
                        continue
                    detail = detail_resp.json()
                    request = detail.get("request") or {}
                    destination = request.get("destination") or {}
                    device = None
                    for dest_path in _dest_frame_paths(destination):
                        device = _FRAME_PATH_TO_DEVICE.get(dest_path)
                        if device is not None:
                            break
                    if device is None:
                        continue
                    pipeline = request.get("pipeline") or {}
                    result["device"] = device
                    result["pipeline_name"] = _pipeline_identity(pipeline)
                    result["state"] = inst.get("state")
                    break
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("detect-pipeline lookup failed for host=%s: %s", host, exc)
    return result


@app.get("/api/detect-pipelines")
async def detect_pipelines(host: str = Query(...)):
    """Auto-detect every device (cpu/gpu/npu) that currently has a RUNNING
    pipeline -- same matching as /api/detect-pipeline, but collects *all*
    matches instead of stopping at the first, so the UI can show one card
    per concurrently-running device (e.g. CPU and GPU both active).
    """
    base = _remote_base(host)
    result = {"reachable": False, "devices": []}
    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"{base}/pipelines/status")
            resp.raise_for_status()
            instances = resp.json()
            result["reachable"] = True
            if isinstance(instances, list):
                instances = sorted(
                    instances,
                    key=lambda inst: (
                        0 if inst.get("state") == "RUNNING" else 1,
                        -(inst.get("start_time") or 0),
                    ),
                )
                seen_devices = set()
                for inst in instances:
                    if inst.get("state") != "RUNNING":
                        continue
                    inst_id = inst.get("id")
                    if inst_id is None:
                        continue
                    detail_resp = await client.get(f"{base}/pipelines/{inst_id}")
                    if detail_resp.status_code != 200:
                        continue
                    detail = detail_resp.json()
                    request = detail.get("request") or {}
                    destination = request.get("destination") or {}
                    device = None
                    for dest_path in _dest_frame_paths(destination):
                        device = _FRAME_PATH_TO_DEVICE.get(dest_path)
                        if device is not None:
                            break
                    if device is None or device in seen_devices:
                        continue
                    seen_devices.add(device)
                    pipeline = request.get("pipeline") or {}
                    result["devices"].append({
                        "device": device,
                        "pipeline_name": _pipeline_identity(pipeline),
                        "state": inst.get("state"),
                    })
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("detect-pipelines lookup failed for host=%s: %s", host, exc)
    return result


@app.get("/api/metrics-stream")
async def metrics_stream(host: str = Query(...)):
    """Proxy metrics-manager's SSE stream (via the remote nginx) to the browser."""
    base = _remote_base(host)
    url = f"{base}/metrics/stream"

    async def relay():
        try:
            async with httpx.AsyncClient(verify=False, timeout=None, trust_env=False) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        yield f"event: error\ndata: {json.dumps({'status': resp.status_code})}\n\n"
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            logger.warning("metrics-stream proxy failed for host=%s: %s", host, exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/api/captions-stream")
async def captions_stream(host: str = Query(...), device: str = Query(..., pattern=DEVICE_PATTERN)):
    """Bridge the remote MQTT broker's uav/caption/<device> topic to the browser over SSE."""
    topic = f"uav/caption/{device}"
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)

    def on_message(_client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        loop.call_soon_threadsafe(queue.put_nowait, payload)

    def on_connect(client, _userdata, _flags, reason_code, _properties=None):
        if reason_code == 0:
            client.subscribe(topic)
            logger.info("captions-stream: subscribed to %s on %s", topic, host)
        else:
            logger.warning("captions-stream: MQTT connect failed (%s) for %s", reason_code, host)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(host, REMOTE_MQTT_PORT, keepalive=30)
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach MQTT broker at {host}:{REMOTE_MQTT_PORT}: {exc}")

    client.loop_start()

    async def relay():
        try:
            yield f"event: connected\ndata: {json.dumps({'topic': topic})}\n\n"
            while True:
                payload = await queue.get()
                yield f"data: {json.dumps(payload)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            client.loop_stop()
            client.disconnect()

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/api/stream/webrtc-path")
async def stream_webrtc_path(host: str = Query(...), device: str = Query(..., pattern=DEVICE_PATTERN)):
    """"WebRTC - Relay" mode (legacy, opt-in -- requires the
    docker-compose-mediamtx.yml overlay; prefer "WebRTC - Direct" when the
    remote pipeline has a native WebRTC destination, see "webrtc_native" in
    /api/pipeline-info). Ensures a per-(host,device) ffmpeg relay is
    running -- pulling the remote host's RTSP output and transcoding it to
    H.264 into a local mediamtx path -- so the browser can view it over
    WebRTC/WHEP instead of the default MJPEG-over-HTTP relay. Works
    regardless of the remote pipeline's own RTSP encoding (MJPEG or H.264);
    see _WebrtcRelay / _ensure_webrtc_relay above.

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
        # ffmpeg ships inside the viz-backend image (see Dockerfile); this
        # only happens if the image was built/modified without it.
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
