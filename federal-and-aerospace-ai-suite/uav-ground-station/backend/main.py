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
  * Relays the remote host's RTSP output (M-JPEG) to the browser as an
    in-browser-native MJPEG stream (multipart/x-mixed-replace over plain
    HTTP) via a short-lived, per-viewer ffmpeg subprocess — no WebRTC/ICE,
    no extra ports, no codec transcode (see VIDEO_DELIVERY_COMPARISON.md for
    why this replaced an earlier mediamtx/WebRTC relay).

Only ever makes outbound, read-only requests to the user-supplied host; it
never starts/stops pipelines on the remote stack.
"""

import asyncio
import json
import logging
import os
import shutil
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
                    dest_path = None
                    if isinstance(destination, dict):
                        dest_path = (destination.get("frame") or {}).get("path")
                    if dest_path != frame_path:
                        continue

                    pipeline = request.get("pipeline") or {}
                    params = request.get("parameters") or {}
                    name = pipeline.get("name")
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
                    dest_path = (destination.get("frame") or {}).get("path") if isinstance(destination, dict) else None
                    device = _FRAME_PATH_TO_DEVICE.get(dest_path)
                    if device is None:
                        continue
                    pipeline = request.get("pipeline") or {}
                    result["device"] = device
                    result["pipeline_name"] = pipeline.get("name")
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
                    dest_path = (destination.get("frame") or {}).get("path") if isinstance(destination, dict) else None
                    device = _FRAME_PATH_TO_DEVICE.get(dest_path)
                    if device is None or device in seen_devices:
                        continue
                    seen_devices.add(device)
                    pipeline = request.get("pipeline") or {}
                    result["devices"].append({
                        "device": device,
                        "pipeline_name": pipeline.get("name"),
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
