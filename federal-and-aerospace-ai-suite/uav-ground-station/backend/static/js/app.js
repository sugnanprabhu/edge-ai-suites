// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

(() => {
  const hostInput = document.getElementById("host-input");
  const streamModeSelect = document.getElementById("stream-mode-select");
  const connectBtn = document.getElementById("connect-btn");
  const refreshBtn = document.getElementById("refresh-btn");
  const connStatus = document.getElementById("conn-status");
  const cardsContainer = document.getElementById("cards-container");
  const toggleArchBtn = document.getElementById("toggle-arch");
  const archPanel = document.getElementById("arch-panel");

  let metricsSource = null;
  let pipelinePollTimer = null;
  let currentHost = null;
  let isConnected = false; // true once at least one device card is live
  let webrtcPort = 8889; // overwritten by /api/runtime-config
  let remoteHttpsPort = 443; // overwritten by /api/runtime-config

  // device -> { wrap, videoContainer, video, chipTtft, chipTpot, chipThroughput, chipLag,
  //             captionStatus, captionTimeline, captionHistory,
  //             lastCaptionReceivedAt, captionsSource, titleEl, metaEl }
  const cards = new Map();

  toggleArchBtn.addEventListener("click", () => archPanel.classList.toggle("hidden"));

  function setStatus(state) {
    connStatus.classList.remove("pill-off", "pill-on", "pill-pending");
    if (state === "connected") {
      connStatus.classList.add("pill-on");
      connStatus.textContent = "connected";
      isConnected = true;
      connectBtn.textContent = "Disconnect";
    } else if (state === "connecting") {
      connStatus.classList.add("pill-pending");
      connStatus.textContent = "connecting…";
    } else {
      connStatus.classList.add("pill-off");
      connStatus.textContent = "disconnected";
      isConnected = false;
      connectBtn.textContent = "Connect";
    }
  }

  function renderEmptyState(message) {
    cardsContainer.innerHTML = "";
    if (!message) return;
    const empty = document.createElement("div");
    empty.className = "cards-empty";
    empty.textContent = message;
    cardsContainer.appendChild(empty);
  }

  function clearEmptyState() {
    const empty = cardsContainer.querySelector(".cards-empty");
    if (empty) empty.remove();
  }

  // --- Caption formatting (mirrors live-video-captioning's run-card timeline) -
  const MAX_CAPTION_BUFFER = 20;
  const VISIBLE_CAPTION_LIMIT = 4;

  function formatStreamSeconds(seconds) {
    if (!Number.isFinite(seconds)) return "—";
    const safeSeconds = Math.max(0, seconds);
    const minutes = Math.floor(safeSeconds / 60);
    const remaining = safeSeconds - minutes * 60;
    const secondsText = remaining.toFixed(2).padStart(5, "0");
    return `${String(minutes).padStart(2, "0")}:${secondsText}`;
  }

  function formatTimelinePositionLabel(index) {
    return index === 0 ? "Latest" : `latest -${index}`;
  }

  function formatCaptionTimestamp(data) {
    if (data && data.timestamp_seconds !== undefined) {
      return formatStreamSeconds(data.timestamp_seconds);
    }
    if (data && data.timestamp) {
      return `at ${new Date(data.timestamp).toLocaleTimeString()}`;
    }
    return `at ${new Date().toLocaleTimeString()}`;
  }

  function renderCaptionTimeline(card) {
    const entries = card.captionHistory.slice(0, VISIBLE_CAPTION_LIMIT);
    card.captionTimeline.innerHTML = "";
    if (entries.length === 0) {
      const empty = document.createElement("div");
      empty.className = "caption-entry caption-entry-placeholder";
      empty.textContent = "Waiting for captions…";
      card.captionTimeline.appendChild(empty);
      return;
    }
    entries.forEach((item, index) => {
      const row = document.createElement("article");
      row.className = "caption-entry";

      const meta = document.createElement("div");
      meta.className = "caption-entry-meta";
      meta.textContent = `${formatTimelinePositionLabel(index)} • ${item.timestampLabel}`;

      const text = document.createElement("p");
      text.className = "caption-entry-text";
      text.textContent = item.captionText;

      row.appendChild(meta);
      row.appendChild(text);
      card.captionTimeline.appendChild(row);
    });
  }

  function updateCaptionHistory(card, data, captionText) {
    card.captionHistory.unshift({
      captionText,
      timestampLabel: formatCaptionTimestamp(data),
    });
    if (card.captionHistory.length > MAX_CAPTION_BUFFER) {
      card.captionHistory.length = MAX_CAPTION_BUFFER;
    }
    renderCaptionTimeline(card);
  }

  // Update every card's lag chip every 100ms, same cadence as
  // live-video-captioning (it does the same thing across all its run cards).
  setInterval(() => {
    const now = Date.now();
    for (const card of cards.values()) {
      if (card.lastCaptionReceivedAt === null) continue;
      const lagSeconds = (now - card.lastCaptionReceivedAt) / 1000;
      card.chipLag.textContent = `${lagSeconds.toFixed(2)}s`;
    }
  }, 100);

  // --- Pipeline auto-detection -----------------------------------------------
  // Finds every device (cpu/gpu/npu) that currently has a RUNNING pipeline,
  // so the user never has to pick one manually, and so multiple concurrently
  // running devices can each get their own card.
  async function detectPipelines(host) {
    try {
      const res = await fetch(`/api/detect-pipelines?host=${encodeURIComponent(host)}`);
      return await res.json();
    } catch (err) {
      return { reachable: false, devices: [] };
    }
  }

  async function loadRuntimeConfig() {
    try {
      const res = await fetch("/api/runtime-config");
      const cfg = await res.json();
      hostInput.value = cfg.defaultHost || "127.0.0.1";
      hostInput.placeholder = cfg.defaultHost || "127.0.0.1";
      if (cfg.webrtcPort) webrtcPort = cfg.webrtcPort;
      if (cfg.remoteHttpsPort) remoteHttpsPort = cfg.remoteHttpsPort;
      return cfg;
    } catch (err) {
      hostInput.value = "127.0.0.1";
      return {};
    }
  }

  // --- Card creation/teardown ------------------------------------------------
  function createCard(device) {
    const wrap = document.createElement("section");
    wrap.className = "device-card panel";

    const header = document.createElement("div");
    header.className = "device-card-header";
    const title = document.createElement("div");
    title.className = "device-card-title";
    title.textContent = device.toUpperCase();
    const meta = document.createElement("div");
    meta.className = "device-card-meta";
    meta.textContent = "—";
    header.appendChild(title);
    header.appendChild(meta);

    // Holds either an <img> (MJPEG-over-HTTP) or an <iframe> (mediamtx's
    // built-in WHEP player) -- only one is ever attached at a time,
    // swapped by startMjpegStream()/startWebrtcStream() below.
    const videoContainer = document.createElement("div");
    videoContainer.className = "device-video-container";
    const video = document.createElement("img");
    video.className = "device-video";
    video.alt = `Live video preview (${device.toUpperCase()})`;
    videoContainer.appendChild(video);

    const captionPanel = document.createElement("div");
    captionPanel.className = "caption-panel";
    const chipsRow = document.createElement("div");
    chipsRow.className = "chips-row";
    const chips = document.createElement("div");
    chips.className = "chips";

    function makeChip(label) {
      const chip = document.createElement("span");
      chip.className = "chip";
      const strong = document.createElement("strong");
      strong.textContent = label;
      const value = document.createElement("span");
      value.textContent = "—";
      chip.appendChild(strong);
      chip.appendChild(value);
      chips.appendChild(chip);
      return value;
    }

    const chipTtft = makeChip("TTFT");
    const chipTpot = makeChip("TPOT");
    const chipThroughput = makeChip("Throughput");
    const chipLag = makeChip("Lag");

    const captionStatus = document.createElement("div");
    captionStatus.className = "caption-status";
    captionStatus.textContent = "—";

    chipsRow.appendChild(chips);
    chipsRow.appendChild(captionStatus);

    const captionTimeline = document.createElement("div");
    captionTimeline.className = "caption-timeline";

    captionPanel.appendChild(chipsRow);
    captionPanel.appendChild(captionTimeline);

    wrap.appendChild(header);
    wrap.appendChild(videoContainer);
    wrap.appendChild(captionPanel);

    const card = {
      device,
      wrap,
      titleEl: title,
      metaEl: meta,
      videoContainer,
      video,
      captionStatus,
      captionTimeline,
      chipTtft,
      chipTpot,
      chipThroughput,
      chipLag,
      captionHistory: [],
      lastCaptionReceivedAt: null,
      captionsSource: null,
    };
    renderCaptionTimeline(card);
    return card;
  }

  function teardownCard(card) {
    if (card.captionsSource) card.captionsSource.close();
    card.video.src = "";
    card.wrap.remove();
  }

  // --- Video (MJPEG-over-HTTP, multipart/x-mixed-replace) ------------------
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

  // --- Video (WebRTC - Direct: remote host's own native WHEP viewer) -------
  // Default, lowest-latency mode. The remote UAV stack already serves a
  // complete, working WHEP viewer page at https://<host>/webrtc/<path>/
  // (see uav-vision-analytics's nginx/mediamtx/coturn setup) -- no local
  // relay, no transcoding, no extra backend code: just iframe it directly.
  // Only available while a *_caption pipeline variant (the only ones that
  // attach a native WebRTC destination) is running -- see
  // /api/pipeline-info's "webrtc_native".
  function startWebrtcDirectStream(host, card) {
    card.videoContainer.innerHTML = "";
    const iframe = document.createElement("iframe");
    iframe.className = "device-video device-video-webrtc";
    iframe.allow = "autoplay";
    iframe.src = `https://${host}:${remoteHttpsPort}/webrtc/${card.rtspPath}/`;
    card.videoContainer.appendChild(iframe);
    setStatus("connected");
  }

  // --- Video (WebRTC - Relay via the local mediamtx sidecar) ----------------
  // Legacy, opt-in fallback (requires the docker-compose-mediamtx.yml
  // overlay) for when the remote pipeline has no native WebRTC destination
  // (e.g. a base, non-caption pipeline). viz-backend transparently
  // transcodes MJPEG (or passes through H.264) into a local mediamtx path
  // via a per-(host,device) ffmpeg relay (see /api/stream/webrtc-path and
  // _webrtc_relays in backend/main.py) -- WebRTC itself still has no MJPEG
  // codec support, but the relay's ffmpeg always publishes H.264 to
  // mediamtx regardless of the source codec.
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

  // --- Pipeline/model info (shown compactly in the card header) ------------
  async function refreshPipelineInfo(host, card) {
    try {
      const res = await fetch(`/api/pipeline-info?host=${encodeURIComponent(host)}&device=${card.device}`);
      const info = await res.json();
      const state = info.reachable ? (info.state || "unknown") : "unreachable";
      const parts = [state];
      if (info.pipeline_name) parts.push(info.pipeline_name);
      parts.push(info.captioning ? "captioning on" : "captioning off");
      if (info.frame_rate !== null && info.frame_rate !== undefined) parts.push(`${info.frame_rate} fps`);
      parts.push((info.encoding || "mjpeg").toUpperCase());
      if (!info.webrtc_native) parts.push("no native WebRTC");
      card.metaEl.textContent = parts.join(" • ");
      card.rtspPath = info.rtsp_path || `uav-mavlink-${card.device}`;
      syncVideoMode(host, card, Boolean(info.webrtc_native));
    } catch (err) {
      card.metaEl.textContent = "error";
    }
  }

  // Starts/swaps the card's video element to match the currently-selected
  // stream mode, without restarting anything that's already active and
  // still correct (avoids tearing down a working ffmpeg/WebRTC session on
  // every 5s pipeline-info poll). "webrtc-direct" falls back to MJPEG
  // whenever the running remote instance has no native WebRTC destination
  // (webrtcNative=false, e.g. a base/non-caption pipeline); it switches back
  // to WebRTC - Direct automatically once/if that changes (e.g. the device
  // is restarted with --caption).
  function syncVideoMode(host, card, webrtcNative) {
    let desiredMode = streamModeSelect.value;
    if (desiredMode === "webrtc-direct" && !webrtcNative) {
      desiredMode = "mjpeg";
    }
    if (card.activeVideoMode === desiredMode) return;
    card.activeVideoMode = desiredMode;

    if (desiredMode === "webrtc-direct") {
      startWebrtcDirectStream(host, card);
    } else if (desiredMode === "webrtc-relay") {
      startWebrtcRelayStream(host, card);
    } else {
      startMjpegStream(host, card);
    }
  }

  // --- Metrics (CPU/GPU/NPU via metrics-manager SSE proxy, host-level) -----
  ChartManager.createConsolidatedChart('statsChart', [
    { key: 'cpu', label: 'CPU %', color: '#1ad0ff' },
    { key: 'mem', label: 'RAM %', color: '#8ca0c2' },
    { key: 'npu', label: 'NPU %', color: '#b388ff' },
  ]);

  function startMetricsStream(host) {
    if (metricsSource) metricsSource.close();
    ChartManager.resetChart();
    metricsSource = new EventSource(`/api/metrics-stream?host=${encodeURIComponent(host)}`);
    metricsSource.onmessage = (evt) => {
      let payload;
      try {
        payload = JSON.parse(evt.data);
      } catch (err) {
        return;
      }
      applyMetrics(payload.metrics || []);
    };
  }

  function setHeaderValue(id, value) {
    const el = document.getElementById(id);
    if (!el || value === undefined || value === null || Number.isNaN(value)) return;
    el.textContent = `${Math.max(0, Math.min(100, value)).toFixed(0)}%`;
  }

  function applyMetrics(metrics) {
    const frameSamples = {};
    const gpuUsageByDevice = new Map();

    for (const m of metrics) {
      if (m.name === "cpu_usage_user") {
        frameSamples.cpu = m.value;
        setHeaderValue("cpuVal", m.value);
      }
      if (m.name === "mem_used_percent") {
        frameSamples.mem = m.value;
        setHeaderValue("ramVal", m.value);
      }
      if (m.name === "npu_utilization") {
        frameSamples.npu = m.value;
        setHeaderValue("npuVal", m.value);
        const npuStat = document.getElementById("npuStat");
        if (npuStat) npuStat.style.display = "";
      }
      if (m.name === "gpu_engine_usage_usage") {
        const gpuId = String((m.labels && m.labels.gpu_id) || "0");
        gpuUsageByDevice.set(gpuId, Math.max(gpuUsageByDevice.get(gpuId) || 0, m.value));
      }
    }

    if (gpuUsageByDevice.size > 0) {
      const gpuValues = document.getElementById("gpuValues");
      if (gpuValues) gpuValues.innerHTML = "";
      Array.from(gpuUsageByDevice.entries())
        .sort(([a], [b]) => (Number(a) || 0) - (Number(b) || 0))
        .forEach(([gpuId, usage]) => {
          const key = `gpu:${gpuId}`;
          frameSamples[key] = usage;
          if (gpuValues) {
            const chip = document.createElement("span");
            chip.className = "gpu-value-chip";
            chip.style.color = ChartManager.getSeriesColor(key);
            chip.textContent = `GPU-${gpuId} ${usage.toFixed(0)}%`;
            gpuValues.appendChild(chip);
          }
        });
      if (gpuValues) gpuValues.style.display = "";
    }

    if (Object.keys(frameSamples).length > 0) {
      ChartManager.pushStatFrame(frameSamples);
    }
  }


  // --- Captions (MQTT bridged via SSE) --------------------------------------
  // Payload shape (from the gvagenai -> MQTTPublisher gvapython stage):
  //   { "metadata": { "result": "<caption text>", "timestamp_seconds": <float>,
  //                    "timestamp": <int ns>, "metrics": {...}, ... }, "blob": "" }
  function startCaptionsStream(host, card) {
    if (card.captionsSource) card.captionsSource.close();
    card.captionHistory = [];
    card.lastCaptionReceivedAt = null;
    renderCaptionTimeline(card);
    card.chipTtft.textContent = "—";
    card.chipTpot.textContent = "—";
    card.chipThroughput.textContent = "—";
    card.chipLag.textContent = "—";
    card.captionsSource = new EventSource(`/api/captions-stream?host=${encodeURIComponent(host)}&device=${card.device}`);
    card.captionsSource.onmessage = (evt) => {
      let payload;
      try {
        payload = JSON.parse(evt.data);
      } catch (err) {
        return;
      }
      const data = (payload && typeof payload === "object" && payload.metadata) ? payload.metadata : payload;
      const captionText = data && typeof data === "object" && data.result
        ? data.result
        : (typeof data === "string" ? data : JSON.stringify(data));
      updateCaptionHistory(card, data, captionText);

      const metrics = (data && typeof data === "object" && data.metrics) ? data.metrics : {};
      card.chipTtft.textContent = Number.isFinite(metrics.ttft_mean) ? `${metrics.ttft_mean.toFixed(0)} ms` : "—";
      card.chipTpot.textContent = Number.isFinite(metrics.tpot_mean) ? `${metrics.tpot_mean.toFixed(2)} ms` : "—";
      card.chipThroughput.textContent = Number.isFinite(metrics.throughput_mean) ? `${metrics.throughput_mean.toFixed(2)} tok/s` : "—";
      card.lastCaptionReceivedAt = Date.now();
      card.chipLag.textContent = "0.00s";

      card.captionStatus.textContent = data && data.timestamp_seconds !== undefined
        ? `Updated ${data.timestamp_seconds.toFixed(2)}s into stream`
        : data && data.timestamp
          ? `Updated at ${new Date(data.timestamp).toLocaleTimeString()}`
          : "—";
    };
    card.captionsSource.addEventListener("connected", () => {
      card.captionStatus.textContent = "Listening for captions…";
    });
  }

  function stopAllStreams() {
    if (metricsSource) { metricsSource.close(); metricsSource = null; }
    if (pipelinePollTimer) { clearInterval(pipelinePollTimer); pipelinePollTimer = null; }
    for (const card of cards.values()) teardownCard(card);
    cards.clear();
  }

  function connectCard(host, card) {
    // Video is started by refreshPipelineInfo()'s syncVideoMode() once we
    // know the pipeline's actual encoding (needed to pick MJPEG vs WebRTC
    // correctly); card.activeVideoMode starts unset so that first call
    // always (re)starts it.
    card.activeVideoMode = null;
    startCaptionsStream(host, card);
    refreshPipelineInfo(host, card);
  }

  // Reconciles the card grid against the currently-detected running devices:
  //  - a device that's newly running -> add a card, start its streams.
  //  - a device that's no longer running -> remove its card, stop its streams.
  //  - a device still running -> just refresh its pipeline-info text.
  // (Metrics are host-level, not device-level, so they're left untouched.)
  function syncCards(host, detectedDevices) {
    const detectedSet = new Set(detectedDevices);

    for (const [device, card] of Array.from(cards.entries())) {
      if (!detectedSet.has(device)) {
        teardownCard(card);
        cards.delete(device);
      } else {
        refreshPipelineInfo(host, card);
      }
    }

    for (const device of detectedDevices) {
      if (!cards.has(device)) {
        clearEmptyState();
        const card = createCard(device);
        cardsContainer.appendChild(card.wrap);
        cards.set(device, card);
        connectCard(host, card);
      }
    }

    if (cards.size === 0) {
      renderEmptyState("No running pipeline detected on this host.");
      setStatus("off");
    } else {
      setStatus("connected");
    }
  }

  async function pollAndReconcile(host) {
    const detected = await detectPipelines(host);
    if (!detected.reachable) {
      // Host went unreachable -- leave existing cards as-is (video/caption
      // streams will show their own connection errors) rather than tearing
      // everything down on a possibly-transient network blip.
      return;
    }
    syncCards(host, detected.devices.map((d) => d.device));
  }

  async function connectNow() {
    const host = (hostInput.value || "127.0.0.1").trim();
    currentHost = host;
    stopAllStreams();
    setStatus("connecting");
    renderEmptyState("Detecting running pipelines…");
    startMetricsStream(host);

    const detected = await detectPipelines(host);
    if (!detected.reachable) {
      renderEmptyState("Host unreachable.");
      setStatus("off");
    } else {
      syncCards(host, detected.devices.map((d) => d.device));
    }
    pipelinePollTimer = setInterval(() => pollAndReconcile(host), 5000);
  }

  function disconnectNow() {
    stopAllStreams();
    renderEmptyState(null);
    setStatus("off");
  }

  connectBtn.addEventListener("click", () => {
    if (isConnected) {
      disconnectNow();
    } else {
      connectNow();
    }
  });

  refreshBtn.addEventListener("click", () => {
    if (!isConnected || !currentHost) return;
    pollAndReconcile(currentHost);
  });

  // Re-sync every live card's video element when the user switches stream
  // mode mid-session (e.g. MJPEG -> WebRTC) instead of requiring a full
  // reconnect.
  streamModeSelect.addEventListener("change", () => {
    if (!isConnected || !currentHost) return;
    for (const card of cards.values()) {
      card.activeVideoMode = null;
      refreshPipelineInfo(currentHost, card);
    }
  });

  loadRuntimeConfig();
})();
