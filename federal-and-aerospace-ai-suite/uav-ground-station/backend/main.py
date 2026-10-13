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
  * Also streams this ground-station machine's *own* CPU/RAM utilization
    (via /api/local-metrics-stream, read from /proc) so the dashboard can
    show how much local compute "H.264 Relay" mode's mediamtx remux costs
    on this box -- see the "UAV Ground Station Utilization" panel.
  * Bridges MQTT scene captions (topic uav/caption/<device>) to the browser
    over SSE.
  * "H.264 Relay" video -- the only video mode: whenever the running remote
    pipeline's RTSP destination uses "encoding": "h264" (see
    pipeline_info's "rtsp_encoding" and uav-vision-analytics's --h264
    flag), the local viz-backend configures a mediamtx sidecar
    (docker-compose-mediamtx.yml, always brought up alongside this
    service -- see `make up`) to pull that already-H.264 RTSP stream on
    demand and re-serve it as WebRTC -- pure remux, no transcoding: see
    /api/relay-path, and the dashboard iframes that local mediamtx's own
    WHEP viewer page. The remote stack's own native WebRTC destination
    (nginx/mediamtx/coturn, available on *_caption pipeline variants) is
    intentionally never consumed directly here -- RTSP is always ingested
    and relayed locally instead, so this stack's own compute/network
    footprint is visible and consistent regardless of how the remote
    pipeline was started.
  * Otherwise (default MJPEG RTSP) the dashboard shows a placeholder
    message instead of video -- start the remote pipeline with --h264 to
    enable relay video.
  * Natural-language video search via a separately deployed Video Search
    and Summarization (VSS) stack -- see vss_search.py.

Only ever makes outbound, read-only requests to the user-supplied host; it
never starts/stops pipelines on the remote stack.
"""

import asyncio
import json
import logging
import os
from typing import Optional

import httpx
import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

import vss_live_sync
from vss_search import router as vss_search_router

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("viz-backend")

DEFAULT_REMOTE_HOST = os.environ.get("DEFAULT_REMOTE_HOST", "127.0.0.1")
REMOTE_HTTPS_PORT = int(os.environ.get("REMOTE_HTTPS_PORT", "443"))
REMOTE_MQTT_PORT = int(os.environ.get("REMOTE_MQTT_PORT", "1883"))
REMOTE_RTSP_PORT = int(os.environ.get("REMOTE_RTSP_PORT", "8555"))

# Local mediamtx sidecar used only for "H.264 Relay" mode (see module
# docstring) -- MEDIAMTX_API_BASE is the internal docker-network address this
# backend uses to (re)configure the relay path; the other two are what the
# *browser* needs to reach mediamtx's own WHEP viewer page directly (so they
# default to loopback but should be set to the ground-station box's LAN IP
# in .env if the browser runs on a different machine). None of these matter
# unless docker-compose-mediamtx.yml is also brought up.
MEDIAMTX_API_BASE = os.environ.get("MEDIAMTX_API_BASE", "http://mediamtx:9997")
MEDIAMTX_WEBRTC_HOST = os.environ.get("MEDIAMTX_WEBRTC_HOST", "127.0.0.1")
MEDIAMTX_WEBRTC_PORT = int(os.environ.get("MEDIAMTX_WEBRTC_PORT", "8889"))

# This ground station's own metrics-manager (intel/metrics-manager, same
# image/metric set as the remote host's), container name "gs-metrics-manager"
# -- deliberately distinct from the remote stack's own "metrics-manager"
# container so the two can coexist if someone ever runs both stacks on the
# same machine (same reasoning for the "gs-" prefix wherever it shows up
# below). Internal-only, no host port published; this backend proxies its
# SSE stream over the docker network, same pattern as /api/metrics-stream.
LOCAL_METRICS_MANAGER_BASE = os.environ.get("LOCAL_METRICS_MANAGER_BASE", "http://gs-metrics-manager:9090")


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


app = FastAPI(title="UAV Vision Analytics - Remote Viewer")


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
    return {"status": "ok"}


@app.get("/api/runtime-config")
async def runtime_config():
    return {
        "defaultHost": DEFAULT_REMOTE_HOST,
        "devices": ["cpu", "gpu", "npu"],
        # Where the browser should reach the local mediamtx sidecar's own
        # WHEP viewer page for "H.264 Relay" mode -- see module docstring.
        # Harmless to send even if docker-compose-mediamtx.yml isn't up;
        # the dashboard only uses these once /api/relay-path succeeds.
        "mediamtxWebrtcHost": MEDIAMTX_WEBRTC_HOST,
        "mediamtxWebrtcPort": MEDIAMTX_WEBRTC_PORT,
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
        # The RTSP destination's encoding, as set by uav-vision-analytics's
        # --h264 flag ("h264" or "mjpeg", see configs/config-pymavlink.json)
        # -- "h264" means "H.264 Relay" mode is usable; anything else leaves
        # the dashboard showing a placeholder instead of video.
        "rtsp_encoding": None,
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

                    info["rtsp_encoding"] = frame_dest.get("encoding", "mjpeg")

                    pipeline = request.get("pipeline") or {}
                    params = request.get("parameters") or {}
                    name = _pipeline_identity(pipeline)
                    captioning = bool(name) and "caption" in name

                    info["pipeline_name"] = name
                    info["state"] = inst.get("state")
                    info["captioning"] = captioning
                    info["frame_rate"] = params.get("frame_rate")

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
    return await _detect_running_devices(host)


async def _detect_running_devices(host: str) -> dict:
    """Shared by /api/detect-pipelines and the VSS live-stream sync."""
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


_prev_cpu_times: Optional[tuple] = None


@app.get("/api/local-metrics-stream")
async def local_metrics_stream():
    """Proxy this ground station's own gs-metrics-manager SSE stream to the
    browser -- same metric set (CPU/RAM/GPU/NPU) and wire format as
    /api/metrics-stream, just reached directly over the docker network
    (internal-only, no host port) instead of via a remote host's nginx."""
    url = f"{LOCAL_METRICS_MANAGER_BASE}/metrics/stream"

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        yield f"event: error\ndata: {json.dumps({'status': resp.status_code})}\n\n"
                        return
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            yield chunk
        except httpx.HTTPError as exc:
            logger.warning("local-metrics-stream proxy failed: %s", exc)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream")


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


@app.post("/api/relay-path")
async def relay_path(host: str = Query(...), device: str = Query(..., pattern=DEVICE_PATTERN)):
    """(Re)configure the local mediamtx sidecar's "relay-<device>" path to
    pull the remote host's H.264 RTSP stream on demand, for "H.264 Relay"
    video mode (see module docstring). Pure remux -- mediamtx does not
    transcode, the remote stream is already H.264.

    Idempotent/cheap to call on every connect (e.g. the user switching the
    host field, or re-polling pipeline-info) -- mediamtx's config API
    accepts repeat PATCHes to the same path name. Returns the path name for
    the dashboard to build its local WHEP viewer iframe URL; raises 502 if
    the mediamtx sidecar isn't reachable (e.g. docker-compose-mediamtx.yml
    wasn't brought up).
    """
    path_name = f"relay-{device}"
    source = f"rtsp://{host}:{REMOTE_RTSP_PORT}/{_frame_path(device)}"
    body = {"source": source, "sourceOnDemand": True}
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.patch(f"{MEDIAMTX_API_BASE}/v3/config/paths/patch/{path_name}", json=body)
            if resp.status_code == 404:
                # Path doesn't exist yet -- create it instead of patching.
                resp = await client.post(f"{MEDIAMTX_API_BASE}/v3/config/paths/add/{path_name}", json=body)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("relay-path config failed for host=%s device=%s: %s", host, device, exc)
        raise HTTPException(status_code=502, detail=f"Cannot configure local mediamtx relay: {exc}")
    return {"path": path_name}


# VSS natural-language search proxy (separate VSS Compose stack) -- see vss_search.py.
vss_live_sync.configure(_detect_running_devices, REMOTE_RTSP_PORT, _frame_path)
app.include_router(vss_search_router)
app.include_router(vss_live_sync.router)


class RevalidateStaticAssets:
    """Make browsers revalidate the dashboard's HTML/CSS/JS on every load
    (cheap 304s via ETag) so a rebuilt image is never paired with stale
    cached assets. Pure ASGI so /api/* streams (SSE, video) are untouched."""

    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_with_cache_header(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache_header)


app.add_middleware(RevalidateStaticAssets)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
