// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// "Live Natural Language Search": prompt popup + results (player on the left,
// ranked list on the right), backed by viz-backend's /api/search* proxy to the
// separate VSS containers. The entry points stay hidden unless viz-backend
// reports search as enabled (VSS_ENABLED, set by docker-compose-vss.yml).
// All result text is set via textContent.
(() => {
  const modal = document.getElementById("search-modal");
  const openBtns = [document.getElementById("open-search-btn"), document.getElementById("topbar-search-btn")];
  const entryPoints = [document.getElementById("search-panel"), document.getElementById("topbar-search-btn")];
  const closeBtn = document.getElementById("search-modal-close");
  const form = document.getElementById("search-form");
  const input = document.getElementById("search-query-input");
  const submitBtn = document.getElementById("search-submit-btn");
  const vssPill = document.getElementById("search-vss-status");
  const body = document.getElementById("search-body");

  const EXAMPLES = ["a white car on the road", "a person walking", "a truck parked near buildings", "an empty street"];

  let inFlight = null;
  let lastFocus = null;
  let results = [];
  let filter = "all";
  let selectedIndex = -1;
  let playerEl = null;
  let listEl = null;
  let searchAvailable = false;

  // ---------- helpers ----------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(kind) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const paths = {
      search: ["M20 20l-3.5-3.5", "circle"],
      alert: ["M12 8v5", "M12 16.5v.01", "M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"],
      film: ["M4 5h16v14H4z", "M8 5v14", "M16 5v14", "M4 9h4", "M4 15h4", "M16 9h4", "M16 15h4"],
    }[kind];
    paths.forEach((d) => {
      if (d === "circle") {
        const c = document.createElementNS(ns, "circle");
        c.setAttribute("cx", "11"); c.setAttribute("cy", "11"); c.setAttribute("r", "7");
        svg.appendChild(c);
      } else {
        const p = document.createElementNS(ns, "path");
        p.setAttribute("d", d);
        svg.appendChild(p);
      }
    });
    return svg;
  }

  function formatTime(seconds) {
    if (seconds == null || Number.isNaN(seconds)) return "?";
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  }

  function formatCaptured(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function displayName(r) {
    if (r.source === "live") return r.stream_name || `Stream ${String(r.video_id || "").slice(0, 8)}`;
    return r.filename || r.video_id || "Unknown video";
  }

  function whenText(r) {
    if (r.source === "live" && r.captured_at) return formatCaptured(r.captured_at) || "";
    if (r.segment_start != null && r.segment_end != null && r.segment_end > r.segment_start) {
      return `${formatTime(r.segment_start)} – ${formatTime(r.segment_end)}`;
    }
    return `at ${formatTime(r.seek_timestamp ?? r.segment_start)}`;
  }

  function scorePct(r) {
    return r.score == null ? null : Math.round(Math.max(0, Math.min(1, r.score)) * 100);
  }

  function setPill(state, text) {
    vssPill.classList.remove("pill-off", "pill-on", "pill-pending");
    vssPill.classList.add(state === "on" ? "pill-on" : state === "off" ? "pill-off" : "pill-pending");
    vssPill.textContent = text;
  }

  // ---------- body states ----------
  function renderMessage(kind, title, detail, withExamples) {
    const wrap = el("div", `search-state search-state-${kind}`);
    if (kind === "loading") {
      wrap.appendChild(el("div", "spinner"));
    } else {
      const ic = el("div", "search-state-icon");
      ic.appendChild(icon(kind === "error" ? "alert" : kind === "empty" ? "film" : "search"));
      wrap.appendChild(ic);
    }
    wrap.appendChild(el("div", "search-state-title", title));
    if (detail) wrap.appendChild(el("div", "search-state-detail muted", detail));
    if (withExamples) {
      const chips = el("div", "search-examples");
      chips.appendChild(el("span", "muted", "Try:"));
      EXAMPLES.forEach((q) => {
        const chip = el("button", "search-example", q);
        chip.type = "button";
        chip.addEventListener("click", () => {
          input.value = q;
          runSearch(q);
        });
        chips.appendChild(chip);
      });
      wrap.appendChild(chips);
    }
    body.replaceChildren(wrap);
    playerEl = null;
    listEl = null;
  }

  function renderIdle() {
    renderMessage("idle", "Search your UAV footage", "Describe a scene, object or activity. Results are ranked by relevance.", true);
  }

  // ---------- player ----------
  function renderFrameFallback(container, r, note) {
    const frame = el("div", "player-media player-frame");
    if (r.thumbnail_url) {
      const img = el("img");
      img.alt = "Best-matching frame";
      img.src = r.thumbnail_url;
      img.addEventListener("error", () => {
        img.remove();
        note = "Media is no longer available (it may have expired from VSS live-stream retention).";
        noteEl.textContent = note;
      }, { once: true });
      frame.appendChild(img);
    }
    const noteEl = el("div", "player-note", note);
    frame.appendChild(noteEl);
    container.replaceChildren(frame);
  }

  function renderPlayer(r, autoplay) {
    if (!playerEl) return;
    const mediaBox = el("div", "player-media-box");

    if (r.video_proxy_url) {
      const video = el("video", "player-media");
      video.controls = true;
      video.muted = true;
      video.playsInline = true;
      video.preload = "metadata";
      if (r.thumbnail_url) video.poster = r.thumbnail_url;
      const seekTo = r.seek_timestamp ?? r.segment_start ?? 0;
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = Math.min(seekTo, video.duration || seekTo);
        if (autoplay) video.play().catch(() => {});
      }, { once: true });
      // Browsers can't decode some stored codecs (e.g. MJPEG live segments).
      video.addEventListener("error", () => {
        renderFrameFallback(mediaBox, r, "This clip's codec can't be played in the browser — showing the best-matching frame.");
      }, { once: true });
      video.src = r.video_proxy_url;
      mediaBox.appendChild(video);
    } else if (r.thumbnail_url) {
      renderFrameFallback(mediaBox, r, "Showing the best-matching frame.");
    } else {
      renderFrameFallback(mediaBox, r, "No preview available for this result.");
    }

    const info = el("div", "player-info");
    const titleRow = el("div", "player-title-row");
    titleRow.appendChild(el("span", `badge ${r.source === "live" ? "badge-live" : "badge-upload"}`, r.source === "live" ? "LIVE" : "UPLOAD"));
    const title = el("h3", "player-title", displayName(r));
    title.title = displayName(r);
    titleRow.appendChild(title);
    const pct = scorePct(r);
    if (pct != null) titleRow.appendChild(el("span", "score-pill", `${pct}% match`));
    info.appendChild(titleRow);

    const facts = el("dl", "player-facts");
    const addFact = (k, v) => {
      if (!v) return;
      facts.appendChild(el("dt", null, k));
      facts.appendChild(el("dd", null, v));
    };
    if (r.source === "live") addFact("Captured", r.captured_at ? formatCaptured(r.captured_at) : null);
    if (r.segment_start != null && r.segment_end != null) {
      addFact(r.source === "live" ? "Within segment" : "Clip", `${formatTime(r.segment_start)} – ${formatTime(r.segment_end)}`);
    }
    if (r.seek_timestamp != null) addFact("Best match", formatTime(r.seek_timestamp));
    if (r.tags && r.tags.length) addFact("Tags", r.tags.join(", "));
    info.appendChild(facts);

    const actions = el("div", "player-actions");
    if (r.video_proxy_url && r.seek_timestamp != null) {
      const jump = el("button", "btn btn-ghost btn-sm", "Jump to best match");
      jump.type = "button";
      jump.addEventListener("click", () => {
        const v = mediaBox.querySelector("video");
        if (v) { v.currentTime = r.seek_timestamp; v.play().catch(() => {}); }
      });
      actions.appendChild(jump);
    }
    if (r.video_proxy_url) {
      const dl = el("a", "btn btn-ghost btn-sm", "Download clip");
      dl.href = r.video_proxy_url;
      dl.setAttribute("download", "");
      actions.appendChild(dl);
    }
    if (actions.childElementCount) info.appendChild(actions);

    playerEl.replaceChildren(mediaBox, info);
  }

  // ---------- list ----------
  function visibleResults() {
    return results
      .map((r, i) => ({ r, i }))
      .filter(({ r }) => filter === "all" || r.source === filter);
  }

  function renderListItem(r, i) {
    const item = el("button", "result-item");
    item.type = "button";
    item.dataset.index = String(i);
    item.setAttribute("aria-pressed", "false");

    const thumb = el("div", "result-thumb");
    if (r.thumbnail_url) {
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      img.src = r.thumbnail_url;
      img.addEventListener("error", () => { img.remove(); thumb.classList.add("result-thumb-missing"); }, { once: true });
      thumb.appendChild(img);
    } else {
      thumb.classList.add("result-thumb-missing");
    }
    thumb.appendChild(el("span", "result-rank", `#${i + 1}`));
    if (r.seek_timestamp != null) thumb.appendChild(el("span", "result-time", formatTime(r.seek_timestamp)));
    item.appendChild(thumb);

    const text = el("div", "result-text");
    const titleRow = el("div", "result-title-row");
    titleRow.appendChild(el("span", `badge ${r.source === "live" ? "badge-live" : "badge-upload"}`, r.source === "live" ? "LIVE" : "UPLOAD"));
    const title = el("span", "result-title", displayName(r));
    title.title = displayName(r);
    titleRow.appendChild(title);
    text.appendChild(titleRow);
    text.appendChild(el("div", "result-when muted", whenText(r)));

    const pct = scorePct(r);
    if (pct != null) {
      const score = el("div", "result-score");
      const bar = el("div", "score-bar");
      const fill = el("div", "score-fill");
      fill.style.width = `${pct}%`;
      bar.appendChild(fill);
      score.appendChild(bar);
      score.appendChild(el("span", "score-value", `${pct}%`));
      text.appendChild(score);
    }
    item.appendChild(text);

    item.addEventListener("click", () => select(i, true));
    return item;
  }

  function renderList() {
    if (!listEl) return;
    const items = visibleResults();
    listEl.replaceChildren(...items.map(({ r, i }) => renderListItem(r, i)));
    highlightSelected();
  }

  function highlightSelected() {
    if (!listEl) return;
    listEl.querySelectorAll(".result-item").forEach((node) => {
      const active = Number(node.dataset.index) === selectedIndex;
      node.classList.toggle("active", active);
      node.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function select(i, autoplay) {
    if (i < 0 || i >= results.length) return;
    selectedIndex = i;
    highlightSelected();
    renderPlayer(results[i], autoplay);
    const node = listEl && listEl.querySelector(`.result-item[data-index="${i}"]`);
    if (node) node.scrollIntoView({ block: "nearest" });
  }

  function renderFilterBar(counts) {
    const bar = el("div", "results-toolbar");
    bar.appendChild(el("span", "results-count", `${results.length} result${results.length === 1 ? "" : "s"}`));
    if (counts.live && counts.upload) {
      const seg = el("div", "segmented");
      [["all", `All ${results.length}`], ["upload", `Uploads ${counts.upload}`], ["live", `Live ${counts.live}`]].forEach(([key, label]) => {
        const b = el("button", `segmented-btn${filter === key ? " active" : ""}`, label);
        b.type = "button";
        b.addEventListener("click", () => {
          filter = key;
          seg.querySelectorAll(".segmented-btn").forEach((n) => n.classList.toggle("active", n === b));
          renderList();
          const visible = visibleResults();
          if (visible.length && !visible.some(({ i }) => i === selectedIndex)) select(visible[0].i, false);
        });
        seg.appendChild(b);
      });
      bar.appendChild(seg);
    }
    return bar;
  }

  function renderResults() {
    const counts = results.reduce((acc, r) => { acc[r.source] = (acc[r.source] || 0) + 1; return acc; }, {});
    filter = "all";

    const layout = el("div", "results-layout");
    playerEl = el("section", "player-pane");
    const listPane = el("section", "list-pane");
    listPane.appendChild(renderFilterBar(counts));
    listEl = el("div", "result-list");
    listEl.setAttribute("role", "list");
    listPane.appendChild(listEl);
    layout.append(playerEl, listPane);
    body.replaceChildren(layout);

    renderList();
    select(0, false);
  }

  // ---------- data ----------
  async function checkVss() {
    setPill("pending", "checking VSS…");
    try {
      const resp = await fetch("/api/search/status");
      const data = await resp.json();
      if (!data.enabled) {
        setPill("off", "search not enabled");
        if (!results.length) {
          renderMessage("error", "Search is not enabled", "Start the ground station with docker-compose-vss.yml to enable search.");
        }
      } else if (!data.reachable) {
        setPill("off", "VSS offline");
        if (!results.length) {
          renderMessage("error", "VSS is not reachable",
            "The VSS search containers are not responding yet. They can take a few minutes to start the first time (models download on first start).");
        }
      } else if (!data.search_enabled) {
        setPill("off", "search disabled");
        if (!results.length) {
          renderMessage("error", "Search is not enabled in VSS", "Redeploy VSS with --search, --dual or --unified.");
        }
      } else {
        setPill("on", "VSS ready");
        if (!results.length && !inFlight && body.querySelector(".search-state-error")) renderIdle();
      }
    } catch (_err) {
      setPill("off", "status unknown");
    }
  }

  async function runSearch(query) {
    if (inFlight) inFlight.abort();
    const controller = new AbortController();
    inFlight = controller;
    results = [];
    selectedIndex = -1;

    submitBtn.disabled = true;
    submitBtn.textContent = "Searching…";
    renderMessage("loading", `Searching for “${query}”…`, "Ranking matching video segments.");
    try {
      const resp = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
        signal: controller.signal,
      });
      let data = null;
      try { data = await resp.json(); } catch (_e) { /* non-JSON error body */ }
      if (!resp.ok) {
        const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${resp.status}`;
        renderMessage("error", "Search failed", detail);
        return;
      }
      results = (data && data.results) || [];
      if (!results.length) {
        renderMessage("empty", `No matches for “${query}”`,
          "Try a different description, or make sure videos have been uploaded and indexed in VSS.", true);
        return;
      }
      renderResults();
    } catch (err) {
      if (err.name !== "AbortError") renderMessage("error", "Search failed", "Can't reach the ground-station backend.");
    } finally {
      if (inFlight === controller) {
        inFlight = null;
        submitBtn.disabled = false;
        submitBtn.textContent = "Search";
      }
    }
  }

  // ---------- modal ----------
  function isOpen() {
    return !modal.classList.contains("hidden");
  }

  function openModal() {
    if (!searchAvailable) return;
    lastFocus = document.activeElement;
    modal.classList.remove("hidden");
    document.body.classList.add("modal-open");
    if (!body.childElementCount) renderIdle();
    input.focus();
    input.select();
    checkVss();
  }

  function closeModal() {
    modal.classList.add("hidden");
    document.body.classList.remove("modal-open");
    body.querySelectorAll("video").forEach((v) => v.pause());
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  openBtns.forEach((b) => b && b.addEventListener("click", openModal));

  // Show the search entry points only when this deployment includes VSS.
  const availability = (async () => {
    try {
      const resp = await fetch("/api/search/status");
      if (resp.ok) searchAvailable = (await resp.json()).enabled === true;
    } catch (_err) {
      searchAvailable = false;
    }
    entryPoints.forEach((n) => n && n.classList.toggle("hidden", !searchAvailable));
    return searchAvailable;
  })();

  // ---- Live indexing: the backend registers every running UAV pipeline as a
  // VSS live stream; this panel just starts/stops that and shows its state.
  const liveSummary = document.getElementById("live-index-summary");
  const liveList = document.getElementById("live-index-list");
  let livePollTimer = null;

  function renderLiveSync(data) {
    if (!liveSummary || !liveList) return;
    const streams = (data && data.streams) || [];
    liveList.replaceChildren(...streams.map((st) => {
      const li = document.createElement("li");
      li.className = "live-index-item";
      const state = String(st.state || "unknown");
      const name = document.createElement("span");
      name.textContent = (st.device || st.stream_name || "feed").toUpperCase();
      const badge = document.createElement("span");
      badge.className = `live-state live-state-${state.replace(/[^a-z]/g, "")}`;
      badge.textContent = state;
      if (st.last_error) badge.title = st.last_error;
      const count = document.createElement("span");
      count.className = "muted";
      count.textContent = st.embeddings_created != null ? `${st.embeddings_created} indexed` : "";
      li.append(name, count, badge);
      return li;
    }));
    const running = streams.filter((st) => st.state === "running").length;
    if (!data || !data.host) liveSummary.textContent = streams.length ? "paused \u2014 connect to a UAV to resume" : "connect to a UAV to start";
    else if (data.last_error && !streams.length) liveSummary.textContent = "sync error";
    else liveSummary.textContent = `${running} of ${streams.length} feed${streams.length === 1 ? "" : "s"} live`;
    liveSummary.title = (data && data.last_error) || "";
  }

  async function liveSync(method, body) {
    try {
      const resp = await fetch("/api/search/live-sync", {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (resp.ok) renderLiveSync(await resp.json());
    } catch (_err) { /* panel keeps its last state */ }
  }

  document.addEventListener("uav:connected", async (e) => {
    if (!(await availability)) return;
    clearInterval(livePollTimer);
    await liveSync("POST", { host: e.detail.host });
    livePollTimer = setInterval(() => liveSync("GET"), 10000);
  });
  document.addEventListener("uav:disconnected", async () => {
    if (!(await availability)) return;
    clearInterval(livePollTimer);
    livePollTimer = null;
    await liveSync("DELETE");
  });
  closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("mousedown", (e) => {
    if (e.target === modal) closeModal();
  });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    if (!isOpen()) {
      if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey && !document.body.classList.contains("modal-open")) {
        e.preventDefault();
        openModal();
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closeModal();
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && results.length && document.activeElement !== input) {
      const visible = visibleResults().map(({ i }) => i);
      const pos = visible.indexOf(selectedIndex);
      const next = visible[Math.max(0, Math.min(visible.length - 1, pos + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next != null && next !== selectedIndex) {
        e.preventDefault();
        select(next, true);
        const node = listEl && listEl.querySelector(`.result-item[data-index="${next}"]`);
        if (node) node.focus();
      }
    } else if (e.key === "Tab") {
      // Keep keyboard focus inside the dialog.
      const focusables = [...modal.querySelectorAll("button, a[href], input, video[controls]")].filter((n) => !n.disabled && n.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = input.value.trim();
    if (!query) {
      input.focus();
      input.classList.add("input-shake");
      setTimeout(() => input.classList.remove("input-shake"), 400);
      return;
    }
    runSearch(query);
  });
})();
