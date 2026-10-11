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
  * "WebRTC - Direct" video (the only stream mode -- available whenever the
    running remote pipeline attached a native WebRTC destination, i.e. a
    *_caption variant -- see _dest_webrtc_entry / pipeline_info's
    "webrtc_native"): the dashboard simply iframes the remote UAV host's own
    https://<host>/webrtc/<frame_path>/ WHEP viewer page directly. No
    transcoding, no local relay/sidecar, no extra backend code at all -- the
    remote stack already serves real, low-latency WebRTC on its own
    (see uav-vision-analytics's nginx/mediamtx/coturn setup). If the running
    instance has no native WebRTC destination, the dashboard shows a
    placeholder message instead of video (see LEGACY_STREAM_MODES.md for
    the MJPEG/WebRTC-Relay fallbacks this project used to have instead).

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

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("viz-backend")

DEFAULT_REMOTE_HOST = os.environ.get("DEFAULT_REMOTE_HOST", "127.0.0.1")
REMOTE_HTTPS_PORT = int(os.environ.get("REMOTE_HTTPS_PORT", "443"))
REMOTE_MQTT_PORT = int(os.environ.get("REMOTE_MQTT_PORT", "1883"))

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
    return {"status": "ok"}


@app.get("/api/runtime-config")
async def runtime_config():
    return {
        "defaultHost": DEFAULT_REMOTE_HOST,
        "devices": ["cpu", "gpu", "npu"],
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
        # True when the running instance attached a native WebRTC
        # AppDestination (peer-id=frame_path) on the remote pipeline server
        # -- only the *_caption pipeline variants do this (see
        # _dest_webrtc_entry above). Used by the dashboard to show a
        # placeholder message instead of video when it isn't available.
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
