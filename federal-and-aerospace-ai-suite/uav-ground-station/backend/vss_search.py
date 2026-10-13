# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Proxy routes for the Video Search and Summarization (VSS) search feature.

VSS runs as its own containers, normally the minimal search-only set in
``docker-compose-vss.yml`` (which also sets ``VSS_ENABLED=true``). It can also
be a full VSS deployment started with VSS's own ``setup.sh``. This module only
talks to VSS's Pipeline Manager API and its object store over HTTP, the same
read-only proxy pattern the rest of viz-backend uses for the remote UAV host.
The browser never reaches VSS directly.

When ``VSS_ENABLED`` is not true, the dashboard hides the search UI and every
route below except ``/api/search/status`` returns 404:

  * ``GET  /api/search/status`` -- whether search is configured, VSS
    reachability and the VSS search feature flag.
  * ``POST /api/search``        -- natural-language query; results are
    flattened and joined with filenames from Pipeline Manager's ``GET /videos``.
  * ``GET  /api/search-video``  -- Range-aware stream of a result's video
    object from VSS's object store (MinIO), so <video> can seek.
  * ``GET  /api/search-frame``  -- best-matching frame as JPEG via
    Pipeline Manager's ``GET /frames``; used as the result thumbnail and as the fallback
    when the browser can't decode the clip (e.g. MJPEG live-stream segments).
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

logger = logging.getLogger("viz-backend.vss-search")

VSS_ENABLED = os.environ.get("VSS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
# docker-compose-vss.yml points these at the pipeline-manager and minio-service
# containers. The defaults reach a full VSS deployment's nginx gateway
# (APP_HOST_PORT 12345) published on this same machine.
VSS_MANAGER_URL = os.environ.get("VSS_MANAGER_URL", "http://host.docker.internal:12345/manager").rstrip("/")
VSS_DATASTORE_URL = os.environ.get("VSS_DATASTORE_URL", "http://host.docker.internal:12345/datastore").rstrip("/")
VSS_DATASTORE_BUCKET = os.environ.get("VSS_DATASTORE_BUCKET", "video-summary")
VSS_SEARCH_TIMEOUT_SECONDS = float(os.environ.get("VSS_SEARCH_TIMEOUT_SECONDS", "60"))

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_QUERY_LENGTH = 500
TIME_UNITS = ("minutes", "hours", "days", "weeks")

# Upstream headers forwarded to the browser for video playback/seeking.
_PASSTHROUGH_VIDEO_HEADERS = ("content-length", "content-range", "accept-ranges", "etag", "last-modified")
_VIDEO_CONTENT_TYPES = {"mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "mkv": "video/x-matroska"}

router = APIRouter()


def _require_enabled() -> None:
    if not VSS_ENABLED:
        raise HTTPException(status_code=404, detail="VSS search is not enabled on this ground station.")


def _client(base_url: str = VSS_MANAGER_URL, timeout: Optional[float] = VSS_SEARCH_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False)


def _is_safe_object_path(path: str) -> bool:
    parts = path.split("/")
    return bool(parts) and all(SAFE_SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts)


def _object_path_from_url(value: Any) -> Optional[str]:
    """Return '<bucket>/<...>' if value is already a datastore object path.

    Mirrors VSS's own UI (ui/react/src/redux/video/videoUrl.ts): dataprep
    download-API URLs (absolute internal hostnames, or anything with a query
    string) are not playable object paths and are rejected.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value.startswith("/") or "?" in value or "/download" in value:
        return None
    path = value.strip("/")
    return path if _is_safe_object_path(path) else None


def _resolve_object_path(metadata: dict, video: Optional[dict]) -> Optional[str]:
    for key in ("video_rel_url", "video_url"):
        path = _object_path_from_url(metadata.get(key))
        if path:
            return path
    if video:
        bucket = (video.get("dataStore") or {}).get("bucket")
        url = video.get("url")
        if bucket and url:
            path = f"{bucket}/{str(url).strip('/')}"
            if _is_safe_object_path(path):
                return path
    bucket = metadata.get("bucket_name") or VSS_DATASTORE_BUCKET
    video_id = metadata.get("video_id")
    if bucket and video_id:
        path = f"{bucket}/{video_id}/source.mp4"
        if _is_safe_object_path(path):
            return path
    return None


def _as_tag_list(tags: Any) -> list:
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    if isinstance(tags, str):
        return [t.strip() for t in tags.split(",") if t.strip()]
    return []


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _frame_params(metadata: dict) -> Optional[dict]:
    """Query params for /api/search-frame, preferring the hit's best_frame_info."""
    info = metadata.get("best_frame_info") if isinstance(metadata.get("best_frame_info"), dict) else {}
    video_id = info.get("video_id") or metadata.get("video_id")
    timestamp = _as_float(info.get("timestamp"))
    if timestamp is None:
        timestamp = _as_float(metadata.get("seek_timestamp"))
    if not video_id or timestamp is None or not SAFE_SEGMENT.fullmatch(str(video_id)):
        return None
    params = {"video_id": video_id, "timestamp": round(max(timestamp, 0.0), 3)}
    bucket = info.get("bucket_name") or metadata.get("bucket_name")
    if bucket and SAFE_SEGMENT.fullmatch(str(bucket)):
        params["bucket_name"] = bucket
    media_path = info.get("media_path") or metadata.get("media_path")
    if media_path and _is_safe_object_path(str(media_path)):
        params["media_path"] = media_path
    return params


def _live_captured_at(media_path: Any, offset: Optional[float]) -> Optional[str]:
    """Live segments are named segments/<unix-epoch>.mp4; return an ISO time."""
    match = re.fullmatch(r"segments/(\d{9,11})\.[A-Za-z0-9]+", str(media_path or ""))
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)) + (offset or 0.0), tz=timezone.utc).isoformat()


def _flatten_results(payload: Any) -> list:
    """VSS wraps hits as {"results": [{"query_id", "results": [...]}]}."""
    groups = payload.get("results", []) if isinstance(payload, dict) else payload
    hits = []
    for group in groups or []:
        if isinstance(group, dict) and isinstance(group.get("results"), list):
            hits.extend(group["results"])
    return hits


class TimeFilter(BaseModel):
    value: int = Field(..., ge=1, le=10000)
    unit: str = Field(..., pattern="^(" + "|".join(TIME_UNITS) + ")$")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    tags: Optional[str] = Field(None, max_length=500)
    timeFilter: Optional[TimeFilter] = None


@router.get("/api/search/status")
async def search_status():
    status = {"enabled": VSS_ENABLED, "reachable": False, "search_enabled": False}
    if not VSS_ENABLED:
        return status
    # Informational only (architecture view); set by docker-compose-vss.yml.
    status["devices"] = {
        key: os.environ[env][:8] for key, env in (
            ("query_embedding", "VSS_QUERY_EMBEDDING_DEVICE"),
            ("ingest_embedding", "VSS_INGEST_EMBEDDING_DEVICE"),
            ("detection", "VSS_DETECTION_DEVICE"),
        ) if os.environ.get(env)
    }
    try:
        async with _client(timeout=5.0) as client:
            health = await client.get("/health")
            status["reachable"] = health.status_code == 200
            if status["reachable"]:
                features = await client.get("/app/features")
                if features.status_code == 200:
                    status["search_enabled"] = features.json().get("search") == "FEATURE_ON"
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("VSS status check failed (%s): %s", VSS_MANAGER_URL, exc)
        status["error"] = "VSS is not reachable"
    return status


@router.post("/api/search")
async def search(req: SearchRequest):
    _require_enabled()
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")
    body: dict = {"query": query}
    if req.tags and req.tags.strip():
        body["tags"] = req.tags.strip()
    if req.timeFilter:
        body["timeFilter"] = req.timeFilter.model_dump()

    try:
        async with _client() as client:
            resp = await client.post("/search/query", json=body)
            if resp.status_code >= 400:
                detail = _upstream_message(resp)
                logger.warning("VSS search failed (%s): %s", resp.status_code, detail)
                code = 400 if resp.status_code == 400 else 502
                raise HTTPException(status_code=code, detail=f"VSS search failed: {detail}")
            hits = _flatten_results(resp.json())

            videos: dict = {}
            try:
                vresp = await client.get("/videos")
                if vresp.status_code == 200:
                    videos = {v.get("videoId"): v for v in vresp.json().get("videos", []) if isinstance(v, dict)}
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("VSS video list lookup failed: %s", exc)

            streams: dict = {}
            try:
                sresp = await client.get("/streams")
                if sresp.status_code == 200:
                    streams = {s.get("stream_id"): s for s in sresp.json().get("streams", []) if isinstance(s, dict)}
            except (httpx.HTTPError, ValueError) as exc:
                logger.debug("VSS stream list lookup failed: %s", exc)
    except httpx.HTTPError as exc:
        logger.warning("VSS search request failed (%s): %s", VSS_MANAGER_URL, exc)
        raise HTTPException(status_code=502, detail="Cannot reach the VSS search service.")
    except ValueError:
        raise HTTPException(status_code=502, detail="VSS returned an invalid response")

    results = []
    for hit in hits:
        metadata = hit.get("metadata") if isinstance(hit, dict) else None
        if not isinstance(metadata, dict):
            continue
        video_id = metadata.get("video_id")
        video = videos.get(video_id)
        stream = streams.get(video_id)
        object_path = _resolve_object_path(metadata, video)
        frame = _frame_params(metadata)
        best = metadata.get("best_frame_info") if isinstance(metadata.get("best_frame_info"), dict) else {}
        is_live = bool(best.get("is_live")) or stream is not None
        results.append({
            "video_id": video_id,
            "source": "live" if is_live else "upload",
            "filename": ((video or {}).get("dataStore") or {}).get("fileName") or (video or {}).get("name"),
            "stream_name": (stream or {}).get("stream_name"),
            "captured_at": _live_captured_at(frame.get("media_path"), frame.get("timestamp")) if frame else None,
            "score": _as_float(metadata.get("relevance_score")),
            "segment_start": _as_float(metadata.get("segment_start")),
            "segment_end": _as_float(metadata.get("segment_end")),
            "seek_timestamp": _as_float(metadata.get("seek_timestamp")),
            "tags": _as_tag_list(metadata.get("tags")),
            "video_proxy_url": f"/api/search-video?path={quote(object_path, safe='/')}" if object_path else None,
            "thumbnail_url": f"/api/search-frame?{urlencode(frame)}" if frame else None,
        })
    return {"query": query, "results": results}


@router.get("/api/search-video")
async def search_video(request: Request, path: str = Query(..., min_length=3, max_length=512)):
    _require_enabled()
    path = path.strip("/")
    if not _is_safe_object_path(path) or "/" not in path:
        raise HTTPException(status_code=400, detail="Invalid video path")

    headers = {}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = _client(VSS_DATASTORE_URL, timeout=httpx.Timeout(10.0, read=None))
    try:
        upstream = await client.send(client.build_request("GET", f"/{path}", headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("VSS video fetch failed for %s: %s", path, exc)
        raise HTTPException(status_code=502, detail="Cannot reach the VSS object store.")

    if upstream.status_code not in (200, 206):
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=404 if status in (403, 404) else 502, detail="Video not available")

    out_headers = {k: upstream.headers[k] for k in _PASSTHROUGH_VIDEO_HEADERS if k in upstream.headers}
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    media_type = _VIDEO_CONTENT_TYPES.get(ext) or upstream.headers.get("content-type", "application/octet-stream")

    async def close():
        await upstream.aclose()
        await client.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=out_headers,
        media_type=media_type,
        background=BackgroundTask(close),
    )


@router.get("/api/search-frame")
async def search_frame(
    video_id: str = Query(..., min_length=1, max_length=128, pattern=SAFE_SEGMENT.pattern),
    timestamp: float = Query(..., ge=0, le=10_000_000),
    bucket_name: Optional[str] = Query(None, max_length=128, pattern=SAFE_SEGMENT.pattern),
    media_path: Optional[str] = Query(None, max_length=256),
):
    _require_enabled()
    params: dict = {"video_id": video_id, "timestamp": timestamp}
    if bucket_name:
        params["bucket_name"] = bucket_name
    if media_path:
        if not _is_safe_object_path(media_path):
            raise HTTPException(status_code=400, detail="Invalid media path")
        params["media_path"] = media_path
    try:
        async with _client(timeout=30.0) as client:
            resp = await client.get("/frames", params=params)
    except httpx.HTTPError as exc:
        logger.warning("VSS frame fetch failed for %s: %s", video_id, exc)
        raise HTTPException(status_code=502, detail="Cannot reach the VSS search service.")
    if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(status_code=404 if resp.status_code in (400, 404) else 502, detail="Frame not available")
    return Response(
        content=resp.content,
        media_type=resp.headers["content-type"],
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _upstream_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        msg = data.get("message") or data.get("detail") or data.get("error")
        if isinstance(msg, list):
            msg = "; ".join(str(m) for m in msg)
        return str(msg)[:300] if msg else f"HTTP {resp.status_code}"
    except ValueError:
        return f"HTTP {resp.status_code}"
