// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Interactive architecture dialog: services grouped by machine, coloured by
// origin, with selectable data flows and a details card.
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const modal = document.getElementById("arch-modal");
  const openBtn = document.getElementById("toggle-arch");
  const closeBtn = document.getElementById("arch-modal-close");
  const svg = document.getElementById("arch-svg");
  const card = document.getElementById("arch-card");
  const devicesEl = document.getElementById("arch-devices");
  const scenarioBar = modal.querySelector(".arch-scenarios");
  if (!modal || !svg) return;

  const ICONS = {
    camera: "M3 8h4l2-3h6l2 3h4v12H3z M12 17a3.5 3.5 0 1 0 0-7a3.5 3.5 0 0 0 0 7z",
    relay: "M3 12h7 M14 12h7 M10 8l4 4-4 4 M3 6v12 M21 6v12",
    chip: "M7 7h10v10H7z M9 3v4 M15 3v4 M9 17v4 M15 17v4 M3 9h4 M3 15h4 M17 9h4 M17 15h4",
    turn: "M4 13a8 8 0 0 1 16 0 M8 13a4 4 0 0 1 8 0 M12 13v8",
    broker: "M4 4a16 16 0 0 1 16 16 M4 10a10 10 0 0 1 10 10 M4 16a4 4 0 0 1 4 4",
    monitor: "M3 4h18v12H3z M8 20h8 M12 16v4",
    shield: "M12 3l8 3v6c0 5-4 8-8 9-4-1-8-4-8-9V6z M9 12l2 2 4-4",
    server: "M4 4h16v6H4z M4 14h16v6H4z M8 7h.01 M8 17h.01",
    gauge: "M4 18a8 8 0 1 1 16 0 M12 18l4-6",
    search: "M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14z M20 20l-4-4",
    db: "M4 6c0-2.7 16-2.7 16 0v12c0 2.7-16 2.7-16 0z M4 6c0 2.7 16 2.7 16 0 M4 12c0 2.7 16 2.7 16 0",
  };

  const PROVIDERS = {
    oep: { name: "Open Edge Platform", title: "Open Edge Platform microservices",
      text: "Intel stock microservices run from their published images, unmodified. The ground station only configures them and calls their own APIs." },
    oss: { name: "Open source", title: "Open-source infrastructure",
      text: "Standard media relay, TURN, MQTT, TLS proxy and storage components, configured only." },
    app: { name: "Application", title: "Application code",
      text: "viz-backend is the only custom service on the ground station. It serves the dashboard and proxies everything else, so the browser never talks to the UAV host's REST, MQTT or the VSS stack directly." },
  };

  const LANES = [
    { id: "uav", x: 16, y: 16, w: 420, h: 628, t: "UAV HOST", s: "uav-vision-analytics" },
    { id: "gs", x: 452, y: 16, w: 352, h: 628, t: "GROUND STATION", s: "this machine · make up" },
    { id: "vss", x: 820, y: 16, w: 364, h: 628, t: "VSS SEARCH", s: "this machine · optional overlay · make up-vss" },
  ];

  // p = origin, t = title, tag = short suffix, s = lines, ep = endpoints in the
  // box, chips = [deviceKey, label], vss = part of the optional VSS overlay.
  const NODES = {
    camera: { x: 36, y: 52, w: 250, h: 64, icon: "camera", t: "PX4 SITL · camera", s: ["drone video + MAVLink telemetry"],
      kind: "Video source", role: "Simulated (or real) drone camera feeding the vision pipelines; MAVLink telemetry is burned into the overlay." },
    dlsps: { p: "oep", x: 36, y: 146, w: 250, h: 146, icon: "chip", t: "DL Streamer", s: ["Pipeline Server", "detect · overlay · caption"],
      ep: ["REST /pipelines", "RTSP :8555 uav-mavlink-<dev>"], chips: [["pipe", "YOLO11s"], ["pipe", "InternVL2-1B"]],
      kind: "Vision pipeline service", role: "One pipeline per device (cpu/gpu/npu): decode, YOLO11s detection, telemetry overlay and, for *_caption variants, VLM scene captions.",
      runs: "YOLO11s and InternVL2-1B (gvagenai) on the device the pipeline variant targets",
      image: "intel/dlstreamer-pipeline-server (+ pymavlink / uavsdk)",
      apis: ["GET /pipelines/status — which devices are RUNNING (dashboard + live sync)", "GET /pipelines/{id} — model and destination info",
        "RTSP out on :8555 /uav-mavlink-<device>", "WebRTC WHIP publish → mediamtx (*_caption)", "MQTT publish uav/caption/<device>"] },
    "uav-mtx": { p: "oss", x: 36, y: 322, w: 250, h: 62, icon: "relay", t: "mediamtx", s: ["WebRTC (WHEP) for *_caption"],
      kind: "Video relay (UAV host)", role: "Receives the annotated stream from DL Streamer over WHIP and serves it to browsers over WHEP — this is what the dashboard plays in WebRTC - Direct mode.",
      image: "bluenviron/mediamtx", apis: ["WHIP in from DL Streamer", "WHEP out, via nginx /webrtc/<path>/"] },
    mqtt: { p: "oss", x: 36, y: 410, w: 250, h: 62, icon: "broker", t: "Mosquitto", s: ["MQTT uav/caption/<device>"],
      kind: "MQTT broker", role: "Carries VLM scene captions from DL Streamer. viz-backend subscribes through nginx's :1883 passthrough.",
      image: "eclipse-mosquitto:2.0.22", apis: ["MQTT uav/caption/<device>"] },
    "uav-metrics": { p: "oep", x: 36, y: 498, w: 250, h: 62, icon: "gauge", t: "Metrics Manager", s: ["UAV board CPU · GPU · NPU"],
      kind: "Telemetry service", role: "Companion-board utilization shown in the \u201cUAV Companion Board Utilization\u201d chart.",
      image: "intel/metrics-manager:2026.2.0", apis: ["GET /metrics/stream (SSE), via nginx /metrics/"] },
    coturn: { p: "oss", x: 36, y: 584, w: 250, h: 48, icon: "turn", t: "Coturn", tag: "TURN relay",
      kind: "TURN relay", role: "Relays WebRTC media between the browser and the UAV host's mediamtx when a direct path isn't available.",
      image: "coturn/coturn:4.12", apis: ["TURN"] },
    nginx: { p: "oss", x: 318, y: 146, w: 100, h: 420, icon: "shield", t: "nginx", s: ["gateway", "", ":443 HTTPS", " / → DLSPS", " /webrtc/", " /metrics/", "", ":8555 RTSP", "", ":1883 MQTT"],
      kind: "TLS gateway (UAV host)", role: "The only entry point into the UAV host: HTTPS for the pipeline REST API, WHEP signalling and metrics; TCP passthrough for RTSP and MQTT.",
      image: "nginx:1.27-alpine", apis: [":443 / → Pipeline Server REST", ":443 /webrtc/ → mediamtx WHEP", ":443 /metrics/ → Metrics Manager", ":8555 → RTSP", ":1883 → MQTT"] },

    browser: { x: 476, y: 52, w: 300, h: 92, icon: "monitor", t: "Dashboard", tag: "browser", s: ["live video · captions · utilization", "Live Natural Language Search popup"],
      kind: "Client", role: "Plays video directly over WebRTC and talks to viz-backend for everything else (REST + SSE). It never contacts the UAV host's REST/MQTT or VSS directly.",
      apis: ["HTTP :8080 REST + SSE to viz-backend", "WebRTC WHEP from the UAV host (Direct) or local mediamtx (Relay)"] },
    backend: { p: "app", x: 476, y: 206, w: 300, h: 156, icon: "server", t: "viz-backend", tag: "FastAPI · :8080",
      s: ["serves this UI; proxies the UAV host and VSS", "• running-pipeline detection + info", "• utilization + captions → SSE", "• H.264 relay control", "• VSS search proxy + live-stream sync"],
      kind: "Custom service", role: "Read-only towards the UAV host (never starts or stops pipelines). With VSS enabled it registers every running pipeline as a VSS live stream and pauses it when the pipeline stops.",
      runs: "CPU — no models", image: "uav-ground-station-backend (built locally)",
      apis: ["GET /api/detect-pipelines, /api/pipeline-info", "GET /api/metrics-stream, /api/local-metrics-stream, /api/captions-stream (SSE)",
        "POST /api/relay-path", "POST /api/search, GET /api/search-video, /api/search-frame", "GET·POST·DELETE /api/search/live-sync"] },
    "gs-mtx": { p: "oss", x: 476, y: 440, w: 140, h: 80, icon: "relay", t: "mediamtx", s: ["H.264 relay", "make up-relay"],
      kind: "Video relay (ground station, optional)", role: "For --h264 pipelines without native WebRTC: pulls the UAV RTSP on demand and re-serves it as WebRTC (pure remux, no transcoding).",
      image: "bluenviron/mediamtx:1.21.2", apis: ["RTSP pull from UAV :8555", "WHEP out :8889", "REST /v3 — paths configured by viz-backend"] },
    "gs-metrics": { p: "oep", x: 636, y: 440, w: 140, h: 80, icon: "gauge", t: "Metrics Mgr", s: ["this machine", "CPU · GPU · NPU"],
      kind: "Telemetry service", role: "Ground-station utilization shown in the \u201cUAV Ground Station Utilization\u201d chart.",
      image: "intel/metrics-manager:2026.2.0", apis: ["GET /metrics/stream (SSE)"] },

    pm: { p: "oep", vss: true, x: 840, y: 52, w: 324, h: 96, icon: "server", t: "Pipeline Manager", tag: ":3000",
      s: ["VSS orchestrator API"], ep: ["POST /search/query", "POST·PATCH·DELETE /streams"],
      kind: "VSS orchestrator", role: "VSS's public API. Runs queries through Video Search and manages live streams in DataPrep; keeps video and stream records in Postgres.",
      image: "pipeline-manager", apis: ["GET /health, /app/features", "POST /search/query", "GET /videos, /frames", "POST /streams, PATCH·DELETE /streams/{id}"] },
    vs: { p: "oep", vss: true, x: 840, y: 180, w: 156, h: 84, icon: "search", t: "Video Search", s: ["prompt → ranked clips"],
      kind: "Search service", role: "Embeds the prompt with Embedding Serving and asks the Vector Retriever for the nearest frames.", image: "video-search" },
    emb: { p: "oep", vss: true, x: 1008, y: 180, w: 156, h: 84, icon: "chip", t: "Embedding", s: ["Serving"], chips: [["query_embedding", "CLIP"]],
      kind: "Embedding service", role: "Turns the typed prompt into a CLIP ViT-B/32 text embedding.", runsKey: "query_embedding", runs: "CLIP ViT-B/32 (OpenVINO)",
      image: "multimodal-embedding-serving" },
    dataprep: { p: "oep", vss: true, x: 840, y: 300, w: 324, h: 112, icon: "chip", t: "Multimodal DataPrep",
      s: ["live RTSP → frames → vectors"], ep: ["live streams · segments"], chips: [["ingest_embedding", "CLIP frames"], ["detection", "YOLOX-S crops"]],
      kind: "Ingestion service", role: "Pulls each registered RTSP feed, cuts ~10 s segments to MinIO, samples frames, embeds them (plus YOLOX object crops) and writes vectors to VDMS.",
      runs: "CLIP ViT-B/32 frame embeddings; YOLOX-S object crops", image: "multimodal-dataprep",
      apis: ["RTSP pull rtsp://<uav>:8555/uav-mavlink-<dev>", "Vectors → VDMS", "Segments → MinIO live-streams bucket", "Stream registry → Postgres"] },
    retr: { p: "oep", vss: true, x: 840, y: 446, w: 156, h: 66, icon: "search", t: "Vector Retriever", s: ["kNN over VDMS"],
      kind: "Retrieval service", role: "Nearest-neighbour search over the frame embeddings.", image: "vector-retriever-vdms" },
    vdms: { p: "oss", vss: true, x: 1008, y: 446, w: 156, h: 66, icon: "db", t: "VDMS", s: ["vector database"],
      kind: "Vector database", role: "Stores frame/crop embeddings and their metadata.", image: "intellabs/vdms:v2.12.0" },
    pg: { p: "oss", vss: true, x: 840, y: 546, w: 156, h: 66, icon: "db", t: "Postgres", s: ["videos · streams"],
      kind: "Database", role: "VSS video records and the live-stream registry.", image: "postgres:17.4" },
    minio: { p: "oss", vss: true, x: 1008, y: 546, w: 156, h: 66, icon: "db", t: "MinIO", s: ["videos · segments"],
      kind: "Object storage", role: "Uploaded videos and live-stream segments, streamed to the search popup through viz-backend.", image: "chainguard/minio" },
  };

  // [id, class, from, to, path, label, lx, ly, anchor, bothWays]
  const EDGES = [
    ["c1", "video", "camera", "dlsps", "M161 116V144", "video", 170, 134, "start"],
    ["r1", "video", "dlsps", "nginx", "M286 196H316", "RTSP", 301, 188],
    ["p1", "rest", "nginx", "dlsps", "M318 252H288", "REST", 302, 244],
    ["w1", "webrtc", "dlsps", "uav-mtx", "M100 292V320", "WHIP", 110, 310, "start"],
    ["q1", "mqtt", "dlsps", "mqtt", "M36 272H26V441H34", "", 0, 0],
    ["w2", "webrtc", "uav-mtx", "nginx", "M286 353H316", "WHEP", 301, 345],
    ["q2", "mqtt", "mqtt", "nginx", "M286 441H316", "MQTT", 301, 433],
    ["m1", "rest", "uav-metrics", "nginx", "M286 529H316", "SSE", 301, 521],
    ["w3", "webrtc", "nginx", "browser", "M368 146V98H474", "WebRTC - Direct", 378, 90, "start"],
    ["t1", "turn", "coturn", "browser", "M286 608H444V130H474", "TURN", 360, 600, "middle", true],
    ["h1", "rest", "backend", "nginx", "M474 236H420", "HTTPS", 446, 228],
    ["q3", "mqtt", "nginx", "backend", "M418 300H474", "MQTT", 446, 292],
    ["r2", "video", "nginx", "gs-mtx", "M418 480H474", "RTSP", 446, 472],
    ["r3", "video", "nginx", "dataprep", "M418 540H806V372H838", "RTSP → live indexing", 612, 556],
    ["h2", "rest", "backend", "browser", "M626 204V146", "REST · SSE", 636, 180, "start", true],
    ["w4", "webrtc", "gs-mtx", "browser", "M500 440V420H460V140H474", "", 0, 0],
    ["h3", "rest", "backend", "gs-mtx", "M546 362V438", "/v3 paths", 556, 404, "start"],
    ["h4", "rest", "gs-metrics", "backend", "M706 440V364", "SSE", 716, 404, "start"],
    ["h5", "rest", "backend", "pm", "M776 250H812V100H838", "REST", 794, 242, "middle", true],
    ["f1", "files", "minio", "backend", "M1086 612V632H816V340H778", "clips", 900, 628],
    ["h6", "rest", "pm", "vs", "M918 148V178", "query", 926, 168, "start"],
    ["h7", "rest", "pm", "dataprep", "M1164 100H1178V356H1166", "/streams", 1170, 284, "end"],
    ["h8", "rest", "vs", "emb", "M996 222H1006", "", 0, 0],
    ["h9", "rest", "vs", "retr", "M840 240H828V479H838", "", 0, 0],
    ["h10", "rest", "retr", "vdms", "M996 479H1006", "", 0, 0],
    ["h11", "rest", "dataprep", "vdms", "M1086 412V444", "vectors", 1094, 432, "start"],
    ["f2", "files", "dataprep", "minio", "M1164 396H1180V579H1166", "", 0, 0],
  ];

  const SCENARIOS = {
    direct: { t: "Live video · Direct", edges: ["c1", "w1", "w2", "w3", "t1"], steps: [
      "The camera feeds DL Streamer, which detects objects with YOLO11s and burns in the telemetry overlay.",
      "*_caption pipeline variants also publish the annotated stream to the UAV host's mediamtx over WebRTC (WHIP).",
      "The dashboard embeds the UAV host's own WHEP viewer (https://<host>/webrtc/<path>/) — nothing on the ground station is in the video path.",
      "Coturn relays the media when a direct path isn't available."] },
    relay: { t: "Live video · H.264 relay", edges: ["c1", "r1", "r2", "h3", "w4"], steps: [
      "Pipelines started with --h264 (no native WebRTC) only publish RTSP on :8555.",
      "viz-backend configures a path on the local mediamtx (REST /v3), which pulls the UAV RTSP on demand.",
      "mediamtx re-serves it to the browser as WebRTC (WHEP) — a pure remux, no transcoding.",
      "Needs make up-relay (docker-compose-mediamtx.yml)."] },
    captions: { t: "Scene captions", edges: ["q1", "q2", "q3", "h2"], steps: [
      "*_caption pipelines run InternVL2-1B on clean frames and publish captions on MQTT uav/caption/<device>.",
      "viz-backend subscribes through nginx's :1883 passthrough.",
      "Captions reach the browser over SSE (/api/captions-stream) and fill each card's timeline."] },
    metrics: { t: "Utilization", edges: ["m1", "h1", "h4", "h2"], steps: [
      "The UAV host's Metrics Manager streams companion-board CPU/GPU/NPU usage; viz-backend proxies it via nginx /metrics/.",
      "The ground station's own Metrics Manager reports this machine's usage.",
      "Both reach the charts over SSE, so the browser never needs the UAV host's self-signed TLS."] },
    index: { t: "Live indexing", vss: true, edges: ["c1", "r1", "p1", "h1", "h5", "h7", "r3", "h11", "f2"], steps: [
      "On Connect, viz-backend checks GET /pipelines/status on the UAV host for RUNNING devices.",
      "Each running device is registered with Pipeline Manager (POST /streams) as rtsp://<uav>:8555/uav-mavlink-<device>; it re-checks every 15 s, adding new pipelines and pausing stopped ones.",
      "DataPrep pulls each RTSP feed, stores ~10 s segments in MinIO and writes CLIP frame embeddings (+ YOLOX crops) to VDMS.",
      "Disconnect pauses the streams; indexed footage is kept for VSS_LIVE_RETENTION_HOURS."] },
    search: { t: "Search", vss: true, edges: ["h2", "h5", "h6", "h8", "h9", "h10", "f1"], steps: [
      "The prompt goes from the popup to viz-backend (POST /api/search), then to Pipeline Manager POST /search/query.",
      "Video Search embeds it with CLIP (Embedding Serving) and the Vector Retriever finds the nearest frames in VDMS.",
      "Ranked hits come back with stream names and timestamps.",
      "The popup streams each clip from MinIO through viz-backend (Range requests) and falls back to the best-matching frame."] },
  };

  const DEVICE_COLORS = { cpu: "cpu", gpu: "gpu", npu: "npu" };
  let vss = { enabled: false, devices: {} };
  let selected = null;

  const el = (tag, attrs, parent, text) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text !== undefined) node.textContent = text;
    if (parent) parent.append(node);
    return node;
  };
  const h = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function chipDevice(key) {
    if (key === "pipe") return { label: "CPU·GPU·NPU", cls: "pipe" };
    const dev = String((vss.devices && vss.devices[key]) || "CPU").toLowerCase();
    return { label: dev.toUpperCase(), cls: DEVICE_COLORS[dev] || "cpu" };
  }

  function draw() {
    for (const id of ["arch-lanes", "arch-edges", "arch-nodes"]) document.getElementById(id).replaceChildren();
    const lanesG = document.getElementById("arch-lanes");
    let maxX = 0, maxY = 0;
    for (const lane of LANES) {
      if (lane.id === "vss" && !vss.enabled) continue;
      const g = el("g", { class: `lane lane-${lane.id}` }, lanesG);
      el("rect", { x: lane.x, y: lane.y, width: lane.w, height: lane.h, rx: 14 }, g);
      el("text", { class: "lane-title", x: lane.x + 16, y: lane.y + 20 }, g, lane.t);
      el("text", { class: "lane-sub", x: lane.x + lane.w - 14, y: lane.y + 20, "text-anchor": "end" }, g, lane.s);
      maxX = Math.max(maxX, lane.x + lane.w);
      maxY = Math.max(maxY, lane.y + lane.h);
    }
    // GROUND STATION and VSS SEARCH are two blocks but one physical machine —
    // bracket their bottom edges together to make that explicit without
    // merging the boxes (only drawn when the VSS lane is actually shown).
    if (vss.enabled) {
      const gsLane = LANES.find((l) => l.id === "gs");
      const vssLane = LANES.find((l) => l.id === "vss");
      const left = gsLane.x, right = vssLane.x + vssLane.w, bottom = gsLane.y + gsLane.h;
      const midX = (left + right) / 2;
      const barY = bottom + 10, tickY = barY + 8, textY = tickY + 16;
      const wrap = el("g", { class: "machine-wrap" }, lanesG);
      el("path", { d: `M${left} ${bottom} V${barY} H${right} V${bottom} M${midX} ${barY} V${tickY}` }, wrap);
      el("text", { x: midX, y: textY, "text-anchor": "middle" }, wrap, "same physical machine");
      maxY = Math.max(maxY, textY + 8);
    }
    svg.setAttribute("viewBox", `0 0 ${maxX + 16} ${maxY + 16}`);

    const edgesG = document.getElementById("arch-edges");
    for (const [id, cls, from, to, d, label, lx, ly, anchor, both] of EDGES) {
      if ((NODES[from].vss || NODES[to].vss || id === "r3") && !vss.enabled) continue;
      const g = el("g", { class: `edge ${cls}`, "data-edge": id }, edgesG);
      const attrs = { d, "marker-end": "url(#arch-arrow)" };
      if (both) attrs["marker-start"] = "url(#arch-arrow)";
      el("path", attrs, g);
      if (label) el("text", { x: lx, y: ly, "text-anchor": anchor || "middle" }, g, label);
    }

    const nodesG = document.getElementById("arch-nodes");
    for (const [id, n] of Object.entries(NODES)) {
      if (n.vss && !vss.enabled) continue;
      const g = el("g", { class: "node" + (n.p ? " " + n.p : ""), "data-node": id,
        tabindex: 0, role: "button", "aria-label": `${n.t} — details` }, nodesG);
      el("rect", { class: "box", x: n.x, y: n.y, width: n.w, height: n.h, rx: 10 }, g);
      if (n.p === "oep") el("rect", { class: "bar", x: n.x, y: n.y, width: n.w, height: 4, rx: 2 }, g);
      el("path", { class: "icon", d: ICONS[n.icon], transform: `translate(${n.x + 10} ${n.y + 10}) scale(0.8)` }, g);
      const title = el("text", { class: "t", x: n.x + 34, y: n.y + 26 }, g, n.t);
      if (n.tag) el("tspan", { class: "tag", dx: 6 }, title, n.tag);
      let y = n.y + 44;
      for (const line of n.s || []) {
        if (line) el("text", { class: "s", x: n.x + 12, y }, g, line);
        y += line ? 16 : 6;
      }
      for (const ep of n.ep || []) {
        el("text", { class: "ep", x: n.x + 12, y }, g, ep);
        y += 15;
      }
      let cx = n.x + 12;
      for (const [key, label] of n.chips || []) {
        const dev = chipDevice(key);
        const text = key === "pipe" ? label : `${label} · ${dev.label}`;
        const w = text.length * 6.1 + 14;
        el("rect", { class: `chip ${dev.cls}`, x: cx, y: n.y + n.h - 26, width: w, height: 18, rx: 9 }, g);
        el("text", { class: "chip-t", x: cx + w / 2, y: n.y + n.h - 13, "text-anchor": "middle" }, g, text);
        cx += w + 6;
      }
      g.addEventListener("click", () => selectNode(id));
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectNode(id); }
      });
    }
    for (const p of Object.keys(PROVIDERS)) {
      const count = modal.querySelector(`[data-count="${p}"]`);
      if (count) count.textContent = Object.values(NODES).filter((n) => n.p === p && (!n.vss || vss.enabled)).length;
    }
    for (const [key, sc] of Object.entries(SCENARIOS)) {
      const btn = scenarioBar.querySelector(`[data-scenario="${key}"]`);
      if (btn) btn.classList.toggle("hidden", !!sc.vss && !vss.enabled);
    }
    const stillValid = !selected
      || (selected.type === "node" && !(NODES[selected.id].vss && !vss.enabled))
      || (selected.type === "scenario" && !(SCENARIOS[selected.id].vss && !vss.enabled))
      || selected.type === "provider";
    if (selected && stillValid) reapply();
    else if (selected) showOverview();
  }

  function clearHighlight() {
    svg.classList.remove("focus");
    svg.querySelectorAll(".hl").forEach((n) => n.classList.remove("hl"));
    modal.querySelectorAll(".arch-scenarios button, .arch-provider").forEach((b) => b.setAttribute("aria-pressed", "false"));
  }

  function highlight(nodeIds, edgeIds) {
    svg.classList.add("focus");
    nodeIds.forEach((id) => { const n = svg.querySelector(`[data-node="${id}"]`); if (n) n.classList.add("hl"); });
    edgeIds.forEach((id) => { const e = svg.querySelector(`[data-edge="${id}"]`); if (e) e.classList.add("hl"); });
  }

  function cardHeader(kicker, title, badge) {
    const head = h("div", "arch-card-head");
    head.append(h("div", "arch-kicker", kicker));
    const t = h("h3", "", title);
    if (badge) t.append(" ", badge);
    head.append(t);
    return head;
  }

  function badgeFor(p) {
    return p ? h("span", `arch-badge ${p}`, PROVIDERS[p].name) : null;
  }

  function showOverview() {
    selected = null;
    clearHighlight();
    const body = [cardHeader("Overview", "Select a service or a flow")];
    body.push(h("p", "", "The ground station is a separate machine from the UAV. It reads the UAV host only through its nginx gateway and never starts or stops pipelines there."));
    body.push(h("p", "", vss.enabled
      ? "VSS search is running: every RUNNING pipeline is indexed live while the dashboard is connected."
      : "VSS search is not running. Start it with make up-vss to enable Live Natural Language Search."));
    const list = h("ul", "arch-list");
    for (const [p, info] of Object.entries(PROVIDERS)) {
      const li = h("li");
      li.append(badgeFor(p), " ", info.text);
      list.append(li);
    }
    body.push(list);
    card.replaceChildren(...body);
  }

  function selectNode(id) {
    const n = NODES[id];
    selected = { type: "node", id };
    clearHighlight();
    const related = EDGES.filter((e) => e[2] === id || e[3] === id);
    highlight([id, ...related.flatMap((e) => [e[2], e[3]])], related.map((e) => e[0]));
    const body = [cardHeader(n.kind, n.t, badgeFor(n.p)), h("p", "", n.role)];
    if (n.vss && !vss.enabled) body.push(h("p", "arch-warn", "Not running — start with make up-vss."));
    const dl = h("dl", "arch-kv");
    const add = (k, v) => { if (v) dl.append(h("dt", "", k), h("dd", "", v)); };
    let runs = n.runs;
    if (n.chips) runs = `${n.runs || ""}${n.runs ? " — " : ""}${n.chips.map(([k, l]) => `${l}: ${chipDevice(k).label}`).join(", ")}`;
    add("Runs", runs);
    add("Image", n.image);
    if (dl.childElementCount) body.push(dl);
    if (n.apis && n.apis.length) {
      body.push(h("div", "arch-kicker", "Interfaces"));
      const ul = h("ul", "arch-list mono");
      n.apis.forEach((a) => ul.append(h("li", "", a)));
      body.push(ul);
    }
    card.replaceChildren(...body);
  }

  function selectScenario(key) {
    const sc = SCENARIOS[key];
    selected = { type: "scenario", id: key };
    clearHighlight();
    const nodes = new Set();
    sc.edges.forEach((eid) => { const e = EDGES.find((x) => x[0] === eid); nodes.add(e[2]); nodes.add(e[3]); });
    highlight([...nodes], sc.edges);
    const btn = scenarioBar.querySelector(`[data-scenario="${key}"]`);
    if (btn) btn.setAttribute("aria-pressed", "true");
    const body = [cardHeader("Data flow", sc.t)];
    if (sc.vss && !vss.enabled) body.push(h("p", "arch-warn", "Needs the VSS overlay — start with make up-vss."));
    const ol = h("ol", "arch-steps");
    sc.steps.forEach((s) => ol.append(h("li", "", s)));
    body.push(ol);
    card.replaceChildren(...body);
  }

  function selectProvider(p) {
    selected = { type: "provider", id: p };
    clearHighlight();
    const ids = Object.keys(NODES).filter((id) => NODES[id].p === p && (!NODES[id].vss || vss.enabled));
    highlight(ids, []);
    const btn = modal.querySelector(`.arch-provider[data-provider="${p}"]`);
    if (btn) btn.setAttribute("aria-pressed", "true");
    const ul = h("ul", "arch-list");
    ids.forEach((id) => {
      const li = h("li");
      const a = h("button", "arch-link", NODES[id].t);
      a.type = "button";
      a.addEventListener("click", () => selectNode(id));
      li.append(a, ` — ${NODES[id].kind}`);
      ul.append(li);
    });
    card.replaceChildren(cardHeader("Origin", PROVIDERS[p].title, badgeFor(p)), h("p", "", PROVIDERS[p].text), ul);
  }

  function reapply() {
    if (!selected) return showOverview();
    if (selected.type === "node") selectNode(selected.id);
    else if (selected.type === "scenario") selectScenario(selected.id);
    else selectProvider(selected.id);
  }

  function toggle(kind, id, fn) {
    if (selected && selected.type === kind && selected.id === id) showOverview();
    else fn(id);
  }

  for (const [key, sc] of Object.entries(SCENARIOS)) {
    const b = h("button", "arch-scenario", sc.t);
    b.type = "button";
    b.dataset.scenario = key;
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => toggle("scenario", key, selectScenario));
    scenarioBar.append(b);
  }
  modal.querySelectorAll(".arch-provider").forEach((b) => {
    b.setAttribute("aria-pressed", "false");
    b.addEventListener("click", () => toggle("provider", b.dataset.provider, selectProvider));
  });

  async function renderDevices() {
    const groups = { npu: [], gpu: [], cpu: [] };
    const note = [];
    const host = (document.getElementById("host-input") || {}).value;
    const connected = /disconnect/i.test((document.getElementById("connect-btn") || {}).textContent || "");
    let running = [];
    if (host && connected) {
      try {
        const r = await fetch(`/api/detect-pipelines?host=${encodeURIComponent(host)}`);
        if (r.ok) running = (await r.json()).devices || [];
      } catch (_e) { /* show the static description */ }
    }
    if (running.length) {
      for (const d of running) {
        const dev = String(d.device || "").toLowerCase();
        if (!groups[dev]) continue;
        groups[dev].push(`UAV · YOLO11s detection (${d.pipeline_name || "uav-mavlink-" + dev})`);
        if (/caption/.test(d.pipeline_name || "")) groups[dev].push("UAV · InternVL2-1B scene captions");
      }
    } else {
      note.push("UAV host: YOLO11s detection and InternVL2-1B captions run on the device each pipeline variant targets (connect to see what is running).");
    }
    if (vss.enabled) {
      const put = (key, text) => { const d = chipDevice(key).cls; (groups[d] || groups.cpu).push(text); };
      put("query_embedding", "Ground station · CLIP ViT-B/32 prompt embedding");
      put("ingest_embedding", "Ground station · CLIP ViT-B/32 frame embedding");
      put("detection", "Ground station · YOLOX-S object crops");
    }
    groups.cpu.push("viz-backend, relays, brokers and databases (no models)");
    const cols = Object.entries(groups).map(([dev, items]) => {
      const col = h("div", `arch-device ${dev}`);
      col.append(h("h4", "", dev.toUpperCase()));
      const ul = h("ul");
      (items.length ? items : ["Nothing assigned to this device right now"]).forEach((t) => ul.append(h("li", items.length ? "" : "muted", t)));
      col.append(ul);
      return col;
    });
    devicesEl.replaceChildren(...cols);
    if (note.length) devicesEl.append(h("p", "arch-note muted", note.join(" ")));
  }

  async function refreshVss() {
    try {
      const r = await fetch("/api/search/status");
      if (r.ok) {
        const data = await r.json();
        vss = { enabled: data.enabled === true, devices: data.devices || {} };
      }
    } catch (_e) { vss = { enabled: false, devices: {} }; }
  }

  let lastFocus = null;
  async function open() {
    lastFocus = document.activeElement;
    modal.classList.remove("hidden");
    document.body.classList.add("modal-open");
    closeBtn.focus();
    await refreshVss();
    draw();
    reapply();
    renderDevices();
  }
  function close() {
    modal.classList.add("hidden");
    document.body.classList.remove("modal-open");
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  openBtn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  modal.addEventListener("mousedown", (e) => { if (e.target === modal) close(); });
  document.addEventListener("keydown", (e) => {
    if (modal.classList.contains("hidden")) return;
    if (e.key === "Escape") { e.preventDefault(); close(); }
  });

  draw();
  showOverview();
})();
