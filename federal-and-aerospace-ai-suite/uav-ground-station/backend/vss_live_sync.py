# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Keep VSS live-stream indexing in step with the UAV's running pipelines.

When the dashboard connects to a UAV host, every device (cpu/gpu/npu) with a
RUNNING pipeline is registered with VSS as a live RTSP stream, so its footage
becomes searchable while the drone flies. A background loop then reconciles
every ``VSS_LIVE_SYNC_INTERVAL_SECONDS``, independent of any open browser:

  * pipeline running, no VSS stream     -> register it (``POST /streams``)
  * pipeline running, VSS stream paused -> resume it (``PATCH state=running``)
  * pipeline running, stream URL differs (host changed) or stream gave up
    (``error``/``stopped``) -> deregister and register again (the URL of a
    VSS stream cannot be changed in place)
  * pipeline stopped, or dashboard disconnected -> pause the VSS stream

Streams are matched by name, ``uav-mavlink-<device>`` (the RTSP path), so a
stream registered by hand under that name is adopted instead of duplicated.
Other VSS streams are never touched. Deregistering keeps the already indexed
footage and embeddings (no purge).

Routes:
  * ``GET    /api/search/live-sync`` -- current host, last sync and streams.
  * ``POST   /api/search/live-sync`` -- ``{"host": ...}`` start/retarget sync.
  * ``DELETE /api/search/live-sync`` -- stop sync and pause the UAV streams.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import vss_search

logger = logging.getLogger("viz-backend.vss-live-sync")

SYNC_INTERVAL_SECONDS = max(5.0, float(os.environ.get("VSS_LIVE_SYNC_INTERVAL_SECONDS", "15")))
# Minimum gap before re-registering a stream that VSS reported as failed, so an
# unreachable RTSP source isn't re-created on every pass.
RECREATE_BACKOFF_SECONDS = 60.0
MANAGED_NAME = re.compile(r"^uav-mavlink-(cpu|gpu|npu)$")
# IPv4 address or DNS hostname; the value ends up inside an RTSP URL.
HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"

Detector = Callable[[str], Awaitable[dict]]

router = APIRouter()

_detector: Optional[Detector] = None
_rtsp_port: int = 8555
_path_for_device: Callable[[str], str] = lambda device: f"uav-mavlink-{device}"

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_recreated_at: dict = {}
_state: dict = {"host": None, "last_sync": None, "last_error": None, "streams": []}


def configure(detector: Detector, rtsp_port: int, path_for_device: Callable[[str], str]) -> None:
    """Called once by main.py with its pipeline detection helpers."""
    global _detector, _rtsp_port, _path_for_device
    _detector = detector
    _rtsp_port = rtsp_port
    _path_for_device = path_for_device


class LiveSyncRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=253, pattern=HOST_PATTERN)


def _status() -> dict:
    return {
        "enabled": vss_search.VSS_ENABLED,
        "host": _state["host"],
        "interval_seconds": SYNC_INTERVAL_SECONDS,
        "last_sync": _state["last_sync"],
        "last_error": _state["last_error"],
        "streams": _state["streams"],
    }


def _summary(stream: dict) -> dict:
    name = stream.get("stream_name")
    match = MANAGED_NAME.fullmatch(str(name or ""))
    stats = stream.get("stats") if isinstance(stream.get("stats"), dict) else {}
    return {
        "device": match.group(1) if match else None,
        "stream_name": name,
        "stream_id": stream.get("stream_id"),
        "state": stream.get("state"),
        "last_error": stream.get("last_error"),
        "embeddings_created": stats.get("embeddings_created"),
    }


async def _call(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> Any:
    resp = await client.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {url} -> {resp.status_code}: {vss_search._upstream_message(resp)}")
    try:
        return resp.json()
    except ValueError:
        return None


async def _reconcile() -> None:
    """One reconciliation pass against VSS; see the module docstring."""
    host = _state["host"]
    async with _lock, vss_search._client(timeout=20.0) as client:
        if host:
            if _detector is None:
                raise RuntimeError("live sync is not configured")
            detected = await _detector(host)
            if not detected.get("reachable"):
                # Same policy as the dashboard: a transient network blip must not
                # pause indexing, so leave VSS untouched until the host answers.
                _state["last_error"] = f"UAV host {host} is not reachable"
                return
            active = {d["device"] for d in detected.get("devices", []) if isinstance(d, dict) and d.get("device")}
        else:
            active = set()

        listing = await _call(client, "GET", "/streams")
        managed: dict = {}
        for stream in (listing or {}).get("streams", []):
            name = stream.get("stream_name") if isinstance(stream, dict) else None
            if name and MANAGED_NAME.fullmatch(name):
                managed.setdefault(name, stream)

        errors = []
        wanted = {_path_for_device(device): device for device in active}
        for name, device in wanted.items():
            url = f"rtsp://{host}:{_rtsp_port}/{name}"
            stream = managed.get(name)
            try:
                if stream:
                    failed = stream.get("state") in ("error", "stopped")
                    moved = str(stream.get("stream_url", "")).rstrip("/") != url
                    if moved or failed:
                        if failed and time.monotonic() - _recreated_at.get(name, 0.0) < RECREATE_BACKOFF_SECONDS:
                            continue
                        await _call(client, "DELETE", f"/streams/{stream['stream_id']}")
                        logger.info("VSS live stream %s deregistered (%s)", name, "host changed" if moved else stream.get("state"))
                        stream = None
                    elif stream.get("state") == "paused":
                        await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "running"})
                        logger.info("VSS live stream %s resumed", name)
                        continue
                if not stream:
                    await _call(client, "POST", "/streams", json={
                        "stream_url": url,
                        "stream_name": name,
                        "sensor_id": name,
                        "description": f"UAV {device.upper()} pipeline feed (registered by uav-ground-station)",
                        "tags": ["uav", device],
                    })
                    _recreated_at[name] = time.monotonic()
                    logger.info("VSS live stream %s registered for %s", name, url)
            except (RuntimeError, httpx.HTTPError) as exc:
                errors.append(str(exc))

        for name, stream in managed.items():
            if name in wanted or stream.get("state") in ("paused", "error", "stopped"):
                continue
            try:
                await _call(client, "PATCH", f"/streams/{stream['stream_id']}", json={"state": "paused"})
                logger.info("VSS live stream %s paused (pipeline not running)", name)
            except (RuntimeError, httpx.HTTPError) as exc:
                errors.append(str(exc))

        listing = await _call(client, "GET", "/streams")
        _state["streams"] = sorted(
            (_summary(s) for s in (listing or {}).get("streams", [])
             if isinstance(s, dict) and MANAGED_NAME.fullmatch(str(s.get("stream_name") or ""))),
            key=lambda s: s["stream_name"],
        )
        _state["last_sync"] = datetime.now(timezone.utc).isoformat()
        _state["last_error"] = "; ".join(errors)[:500] or None


async def _run_once() -> None:
    try:
        await _reconcile()
    except (RuntimeError, httpx.HTTPError, ValueError) as exc:
        _state["last_error"] = str(exc)[:500]
        logger.warning("VSS live sync failed: %s", exc)


async def _loop() -> None:
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        if _state["host"]:
            await _run_once()


def _ensure_loop() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def _require_enabled() -> None:
    if not vss_search.VSS_ENABLED:
        raise HTTPException(status_code=404, detail="VSS search is not enabled on this ground station.")


@router.get("/api/search/live-sync")
async def live_sync_status():
    _require_enabled()
    return _status()


@router.post("/api/search/live-sync")
async def live_sync_start(req: LiveSyncRequest):
    _require_enabled()
    _state["host"] = req.host
    _ensure_loop()
    await _run_once()
    return _status()


@router.delete("/api/search/live-sync")
async def live_sync_stop():
    _require_enabled()
    _state["host"] = None
    await _run_once()
    return _status()
