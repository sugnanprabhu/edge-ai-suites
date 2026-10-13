// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

(() => {
  const hostInput = document.getElementById("host-input");
  const connectBtn = document.getElementById("connect-btn");
  const refreshBtn = document.getElementById("refresh-btn");
  const connStatus = document.getElementById("conn-status");
  const cardsContainer = document.getElementById("cards-container");

  let metricsSource = null;
  let localMetricsSource = null;
  let pipelinePollTimer = null;
  let currentHost = null;
  let isConnected = false; // true once at least one device card is live
  let remoteHttpsPort = 443; // overwritten by /api/runtime-config
  let mediamtxWebrtcHost = "127.0.0.1"; // overwritten by /api/runtime-config
  let mediamtxWebrtcPort = 8889; // overwritten by /api/runtime-config

  // device -> { wrap, videoContainer, video, chipTtft, chipTpot, chipThroughput, chipLag,
  //             captionStatus, captionTimeline, captionHistory,
  //             lastCaptionReceivedAt, captionsSource, titleEl, metaEl }
  const cards = new Map();


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
      if (cfg.remoteHttpsPort) remoteHttpsPort = cfg.remoteHttpsPort;
      if (cfg.mediamtxWebrtcHost) mediamtxWebrtcHost = cfg.mediamtxWebrtcHost;
      if (cfg.mediamtxWebrtcPort) mediamtxWebrtcPort = cfg.mediamtxWebrtcPort;
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
    const deviceBadge = document.createElement("span");
    deviceBadge.className = `device-badge device-badge-${device}`;
    deviceBadge.textContent = device.toUpperCase();
    title.appendChild(deviceBadge);
    const meta = document.createElement("div");
    meta.className = "device-card-meta";
    meta.textContent = "—";
    // Expands the live video to the whole screen (Esc returns). Fullscreens
    // our own container rather than relying on the remote viewer page's
    // controls, so it works for both Direct and Relay modes.
    const expandBtn = document.createElement("button");
    expandBtn.type = "button";
    expandBtn.className = "btn btn-ghost btn-icon-only device-expand-btn";
    expandBtn.title = "Full screen";
    expandBtn.setAttribute("aria-label", `View ${device.toUpperCase()} video full screen`);
    expandBtn.disabled = true;
    expandBtn.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
    const headerRight = document.createElement("div");
    headerRight.className = "device-card-header-right";
    headerRight.appendChild(meta);
    headerRight.appendChild(expandBtn);
    header.appendChild(title);
    header.appendChild(headerRight);

    // Holds either an <iframe> (the remote host's native WHEP viewer) or a
    // placeholder <div> (no native WebRTC destination available) -- swapped
    // by syncVideoMode() below.
    const videoContainer = document.createElement("div");
    videoContainer.className = "device-video-container";
    // Enabled only while a live video iframe is mounted (not a placeholder).
    new MutationObserver(() => {
      expandBtn.disabled = !videoContainer.querySelector("iframe");
    }).observe(videoContainer, { childList: true });
    // videoContainer's children are replaced on every mode switch, so the
    // full-screen target is a stable wrapper that also holds the exit button.
    const videoStage = document.createElement("div");
    videoStage.className = "device-video-stage";
    const exitBtn = document.createElement("button");
    exitBtn.type = "button";
    exitBtn.className = "btn btn-ghost device-exit-fullscreen";
    exitBtn.textContent = `${device.toUpperCase()} \u2022 Exit full screen (Esc)`;
    exitBtn.addEventListener("click", () => document.exitFullscreen().catch(() => {}));
    videoStage.appendChild(videoContainer);
    videoStage.appendChild(exitBtn);
    expandBtn.addEventListener("click", () => {
      if (videoStage.requestFullscreen) videoStage.requestFullscreen().catch(() => {});
    });

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
    wrap.appendChild(videoStage);
    wrap.appendChild(captionPanel);

    const card = {
      device,
      wrap,
      titleEl: title,
      metaEl: meta,
      videoContainer,
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
    card.wrap.remove();
  }

  // --- Video modes -----------------------------------------------------
  // "WebRTC - Direct": the remote UAV stack already serves a complete,
  // working WHEP viewer page at https://<host>/webrtc/<path>/ (see
  // uav-vision-analytics's nginx/mediamtx/coturn setup) -- no local relay,
  // no transcoding, no extra backend code: just iframe it directly. Only
  // available while a *_caption pipeline variant (the only ones that attach
  // a native WebRTC destination) is running -- see /api/pipeline-info's
  // "webrtc_native".
  //
  // "H.264 Relay": used instead whenever the running remote pipeline's RTSP
  // destination is H.264 (--h264, see "rtsp_encoding") but has no native
  // WebRTC destination. The local viz-backend configures a mediamtx
  // sidecar (docker-compose-mediamtx.yml) to pull that already-H.264 RTSP
  // stream on demand and re-serve it as WebRTC -- pure remux, no
  // transcoding -- then this iframes mediamtx's own WHEP viewer page
  // directly, same pattern as WebRTC - Direct.
  //
  // See LEGACY_STREAM_MODES.md for the fully ffmpeg-transcoded MJPEG/
  // WebRTC-Relay this project used to have instead.
  function startWebrtcDirectStream(host, card) {
    card.videoContainer.innerHTML = "";
    const iframe = document.createElement("iframe");
    iframe.className = "device-video device-video-webrtc";
    iframe.allow = "autoplay; fullscreen";
    iframe.allowFullscreen = true;
    iframe.src = `https://${host}:${remoteHttpsPort}/webrtc/${card.rtspPath}/`;
    card.videoContainer.appendChild(iframe);
    setStatus("connected");
  }

  async function startH264RelayStream(host, card) {
    card.videoContainer.innerHTML = "";
    const loading = document.createElement("div");
    loading.className = "device-video-placeholder";
    loading.textContent = "Starting H.264 relay…";
    card.videoContainer.appendChild(loading);
    try {
      const res = await fetch(
        `/api/relay-path?host=${encodeURIComponent(host)}&device=${card.device}`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(`relay-path ${res.status}`);
      const { path } = await res.json();
      // Only swap the DOM in if this card is still in relay mode and
      // pointed at the same host -- avoids a slow response clobbering a
      // newer mode switch that happened while this fetch was in flight.
      if (card.activeVideoMode !== "relay" || card.relayHost !== host) return;
      card.videoContainer.innerHTML = "";
      const iframe = document.createElement("iframe");
      iframe.className = "device-video device-video-webrtc";
      iframe.allow = "autoplay; fullscreen";
      iframe.allowFullscreen = true;
      iframe.src = `http://${mediamtxWebrtcHost}:${mediamtxWebrtcPort}/${path}/`;
      card.videoContainer.appendChild(iframe);
      setStatus("connected");
    } catch (err) {
      card.videoContainer.innerHTML = "";
      const placeholder = document.createElement("div");
      placeholder.className = "device-video-placeholder";
      placeholder.textContent = "H.264 relay unavailable — is docker-compose-mediamtx.yml running?";
      card.videoContainer.appendChild(placeholder);
    }
  }

  function showNoWebrtcPlaceholder(card) {
    card.videoContainer.innerHTML = "";
    const placeholder = document.createElement("div");
    placeholder.className = "device-video-placeholder";
    placeholder.textContent = "No video stream — start this device with --caption or --h264.";
    card.videoContainer.appendChild(placeholder);
  }

  // --- Pipeline/model info (shown compactly in the card header) ------------
  async function refreshPipelineInfo(host, card) {
    try {
      const res = await fetch(`/api/pipeline-info?host=${encodeURIComponent(host)}&device=${card.device}`);
      const info = await res.json();
      const state = info.reachable ? (info.state || "unknown") : "unreachable";
      const parts = [state];
      if (info.pipeline_name) parts.push(info.pipeline_name);
      // Header shows only state + pipeline; the rest stays available on hover.
      const details = [info.captioning ? "captioning on" : "captioning off"];
      if (info.frame_rate !== null && info.frame_rate !== undefined) details.push(`${info.frame_rate} fps`);
      if (info.webrtc_native) {
        details.push("WebRTC - Direct");
      } else if (info.rtsp_encoding === "h264") {
        details.push("H.264 Relay");
      } else {
        details.push("no video stream");
      }
      card.metaEl.textContent = parts.join(" • ");
      card.metaEl.title = [...parts, ...details].join(" • ");
      card.rtspPath = info.rtsp_path || `uav-mavlink-${card.device}`;
      syncVideoMode(host, card, Boolean(info.webrtc_native), info.rtsp_encoding === "h264");
    } catch (err) {
      card.metaEl.textContent = "error";
    }
  }

  // Starts/swaps the card's video element to match the current remote
  // destination's capabilities, without restarting anything that's already
  // active and still correct (avoids tearing down a working WebRTC session
  // on every 5s pipeline-info poll). Picks, in order: WebRTC - Direct
  // (native remote WebRTC destination) > H.264 Relay (local mediamtx remux,
  // only when the remote RTSP is already H.264) > placeholder (default
  // MJPEG RTSP, nothing we can show without a transcode).
  function syncVideoMode(host, card, webrtcNative, h264Available) {
    const mode = webrtcNative ? "direct" : h264Available ? "relay" : "none";
    if (card.activeVideoMode === mode && card.relayHost === host) return;
    card.activeVideoMode = mode;
    card.relayHost = host;

    if (mode === "direct") {
      startWebrtcDirectStream(host, card);
    } else if (mode === "relay") {
      startH264RelayStream(host, card);
    } else {
      showNoWebrtcPlaceholder(card);
    }
  }

  // --- Metrics (CPU/GPU/NPU via metrics-manager SSE proxy, host-level) -----
  ChartManager.createConsolidatedChart('statsChart', [
    { key: 'cpu', label: 'CPU %', color: '#1ad0ff' },
    { key: 'mem', label: 'RAM %', color: '#8ca0c2' },
    { key: 'npu', label: 'NPU %', color: '#b388ff' },
  ]);

  // --- Metrics (this ground-station machine's own gs-metrics-manager) -----
  // Independent of the remote "connect" lifecycle -- always shows how much
  // local compute is spent on H.264 Relay remuxing / WHEP viewing here.
  // Same image/metric set as the remote chart, so it reuses applyMetricsTo.
  const GroundStationChartManager = createChartManager();
  GroundStationChartManager.createConsolidatedChart('gsStatsChart', [
    { key: 'cpu', label: 'CPU %', color: '#1ad0ff' },
    { key: 'mem', label: 'RAM %', color: '#8ca0c2' },
    { key: 'npu', label: 'NPU %', color: '#b388ff' },
  ]);

  function startLocalMetricsStream() {
    if (localMetricsSource) return;
    localMetricsSource = new EventSource('/api/local-metrics-stream');
    localMetricsSource.onmessage = (evt) => {
      let payload;
      try {
        payload = JSON.parse(evt.data);
      } catch (err) {
        return;
      }
      applyMetricsTo(payload.metrics || [], GroundStationChartManager, 'gs');
    };
  }

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
      applyMetricsTo(payload.metrics || [], ChartManager, '');
    };
  }

  function setHeaderValue(id, value) {
    const el = document.getElementById(id);
    if (!el || value === undefined || value === null || Number.isNaN(value)) return;
    el.textContent = `${Math.max(0, Math.min(100, value)).toFixed(0)}%`;
  }

  function resetMetricsHeader() {
    const cpuVal = document.getElementById("cpuVal");
    if (cpuVal) cpuVal.textContent = "—";
    const ramVal = document.getElementById("ramVal");
    if (ramVal) ramVal.textContent = "—";
    const npuVal = document.getElementById("npuVal");
    if (npuVal) npuVal.textContent = "—";
    const npuStat = document.getElementById("npuStat");
    if (npuStat) npuStat.style.display = "none";
    const gpuValues = document.getElementById("gpuValues");
    if (gpuValues) {
      gpuValues.innerHTML = "";
      gpuValues.style.display = "none";
    }
  }

  // Shared metrics-manager payload parser -- `idPrefix` picks which panel's
  // DOM ids to update ("" for the remote "UAV Companion Board" panel's
  // cpuVal/ramVal/npuVal/npuStat/gpuValues, "gs" for this ground station's
  // own gsCpuVal/gsRamVal/gsNpuVal/gsNpuStat/gsGpuValues).
  function applyMetricsTo(metrics, chartManager, idPrefix) {
    // "" (remote panel) uses lowerCamelCase ids (cpuVal, npuStat, ...);
    // "gs" (ground station panel) prefixes the capitalized suffix (gsCpuVal, gsNpuStat, ...).
    const id = (suffix) => (idPrefix ? `${idPrefix}${suffix}` : suffix.charAt(0).toLowerCase() + suffix.slice(1));
    const frameSamples = {};
    const gpuUsageByDevice = new Map();

    for (const m of metrics) {
      if (m.name === "cpu_usage_user") {
        frameSamples.cpu = m.value;
        setHeaderValue(id("CpuVal"), m.value);
      }
      if (m.name === "mem_used_percent") {
        frameSamples.mem = m.value;
        setHeaderValue(id("RamVal"), m.value);
      }
      if (m.name === "npu_utilization") {
        frameSamples.npu = m.value;
        setHeaderValue(id("NpuVal"), m.value);
        const npuStat = document.getElementById(id("NpuStat"));
        if (npuStat) npuStat.style.display = "";
      }
      if (m.name === "gpu_engine_usage_usage") {
        const gpuId = String((m.labels && m.labels.gpu_id) || "0");
        gpuUsageByDevice.set(gpuId, Math.max(gpuUsageByDevice.get(gpuId) || 0, m.value));
      }
    }

    if (gpuUsageByDevice.size > 0) {
      const gpuValues = document.getElementById(id("GpuValues"));
      if (gpuValues) gpuValues.innerHTML = "";
      Array.from(gpuUsageByDevice.entries())
        .sort(([a], [b]) => (Number(a) || 0) - (Number(b) || 0))
        .forEach(([gpuId, usage]) => {
          const key = `gpu:${gpuId}`;
          frameSamples[key] = usage;
          if (gpuValues) {
            const chip = document.createElement("span");
            chip.className = "gpu-value-chip";
            chip.style.color = chartManager.getSeriesColor(key);
            chip.textContent = `GPU-${gpuId} ${usage.toFixed(0)}%`;
            gpuValues.appendChild(chip);
          }
        });
      if (gpuValues) gpuValues.style.display = "";
    }

    if (Object.keys(frameSamples).length > 0) {
      chartManager.pushStatFrame(frameSamples);
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
    // know whether the pipeline has a native WebRTC destination;
    // card.activeVideoMode starts unset so that first call always (re)starts
    // it.
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
    // search.js registers this host's running pipelines for VSS live search.
    document.dispatchEvent(new CustomEvent("uav:connected", { detail: { host } }));
  }

  function disconnectNow() {
    stopAllStreams();
    ChartManager.resetChart();
    resetMetricsHeader();
    renderEmptyState(null);
    setStatus("off");
    document.dispatchEvent(new CustomEvent("uav:disconnected"));
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

  loadRuntimeConfig();
  startLocalMetricsStream();
})();
