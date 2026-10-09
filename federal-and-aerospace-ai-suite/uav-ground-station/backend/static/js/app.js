// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

(() => {
  const hostInput = document.getElementById("host-input");
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

  // device -> { wrap, video, chipTtft, chipTpot, chipThroughput, chipLag,
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

    const video = document.createElement("img");
    video.className = "device-video";
    video.alt = `Live video preview (${device.toUpperCase()})`;

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
    wrap.appendChild(video);
    wrap.appendChild(captionPanel);

    const card = {
      device,
      wrap,
      titleEl: title,
      metaEl: meta,
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
    card.video.src = "";
    card.video.onload = () => setStatus("connected");
    card.video.onerror = () => {
      card.captionStatus.textContent = "Connection failed: video stream could not be reached.";
    };
    const url = `/api/stream/mjpeg?host=${encodeURIComponent(host)}&device=${card.device}&_=${Date.now()}`;
    card.video.src = url;
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
      card.metaEl.textContent = parts.join(" • ");
    } catch (err) {
      card.metaEl.textContent = "error";
    }
  }

  // --- Metrics (CPU/GPU/NPU via metrics-manager SSE proxy, host-level) -----
  function startMetricsStream(host) {
    if (metricsSource) metricsSource.close();
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

  function setBar(prefix, value) {
    const bar = document.getElementById(`bar-${prefix}`);
    const val = document.getElementById(`val-${prefix}`);
    if (!bar || !val || value === undefined || value === null || Number.isNaN(value)) return;
    const pct = Math.max(0, Math.min(100, value));
    bar.style.width = `${pct}%`;
    val.textContent = `${pct.toFixed(0)}%`;
  }

  const gpuRowsSeen = new Set();

  function ensureGpuRow(gpuId) {
    if (gpuRowsSeen.has(gpuId)) return;
    gpuRowsSeen.add(gpuId);
    const container = document.getElementById("gpu-rows");
    const row = document.createElement("div");
    row.className = "metric-row";
    row.innerHTML = `
      <span class="metric-label">GPU ${gpuId}</span>
      <div class="bar"><div class="bar-fill" id="bar-gpu-${gpuId}"></div></div>
      <span class="metric-value" id="val-gpu-${gpuId}">—</span>`;
    container.appendChild(row);
  }

  function applyMetrics(metrics) {
    for (const m of metrics) {
      if (m.name === "cpu_usage_user") setBar("cpu", m.value);
      if (m.name === "mem_used_percent") setBar("mem", m.value);
      if (m.name === "npu_utilization") setBar("npu", m.value);
      if (m.name === "gpu_engine_usage_usage") {
        const gpuId = (m.labels && m.labels.gpu_id) || "0";
        ensureGpuRow(gpuId);
        setBar(`gpu-${gpuId}`, m.value);
      }
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
    startMjpegStream(host, card);
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

  loadRuntimeConfig();
})();
