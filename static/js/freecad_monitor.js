(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const RESOURCE_COLORS = {
    system: "#4d9ef5",
    systemFill: "rgba(77,158,245,0.12)",
    apiWorker: "#34d399",
    freecad: "#f59e42",
  };
  const fmtMb = (v) => v == null ? "-" : `${Number(v).toFixed(0)} MB`;
  const fmtGb = (v) => v == null ? "-" : `${(Number(v) / 1024).toFixed(2)} GB`;
  const fmtPct = (v) => v == null ? "-" : `${Number(v).toFixed(1)}%`;
  const fmtNum = (v) => Number(v || 0).toLocaleString();
  const fmtPriority = (v) => v == null || v === "" ? "P-" : `P${Number(v)}`;
  const shortId = (s, n = 18) => !s ? "-" : String(s).length <= n ? String(s) : `${String(s).slice(0, n)}...`;
  const safe = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
  }[c]));

  const fmtMs = (ms) => {
    if (ms == null) return "-";
    const n = Number(ms);
    if (n >= 60000) return `${(n / 60000).toFixed(1)} min`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)} s`;
    return `${Math.round(n)} ms`;
  };

  const fmtTime = (iso) => {
    if (!iso) return "-";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
  };

  const pickServerRam = (system) => Number(
    system?.server_ram_used_mb ??
    system?.system_ram_used_mb ??
    system?.process_rss_mb ??
    system?.ram_used_mb ??
    0
  );
  const pickServerCpu = (system) => Number(
    system?.server_cpu_percent ??
    system?.system_cpu_percent ??
    system?.process_cpu_percent ??
    system?.cpu_percent ??
    0
  );

  const state = {
    timer: null,
    paused: false,
    ramChart: null,
    cpuChart: null,
    stageChart: null,
    resourceWindowMinutes: 1,
    lastOverview: null,
  };

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function setKpi(id, value) {
    const el = $(id);
    if (!el) return;
    const text = String(value);
    if (el.textContent !== text) {
      el.textContent = text;
      el.classList.add("flash");
      setTimeout(() => el.classList.remove("flash"), 350);
    }
  }

  function renderOverview(data) {
    const summary = data.summary || {};
    const system = data.system || {};
    const api = data.api_process || {};

    setKpi("kpi-workers", `${summary.busy_workers || 0} / ${summary.total_workers || 0}`);
    setKpi("kpi-running", summary.running_jobs || 0);
    setKpi("kpi-queued", summary.queued_jobs || 0);
    setKpi("kpi-ram", fmtMb(pickServerRam(system)));
    setKpi("kpi-cpu", fmtPct(pickServerCpu(system)));
    setKpi("kpi-failed", summary.failed_jobs || 0);

    $("service-state").textContent = "FreeCAD monitor online";
    $("mqtt-state").textContent = `MQTT ${data.mqtt_connected ? "connected" : "offline"}`;
    $("redis-state").textContent = "Redis connected";
    $("stat-api-rss").textContent = fmtMb(api.rss_mb);
    $("stat-cores").textContent = system.cpu_count_physical
      ? `${system.cpu_count ?? "-"} / ${system.cpu_count_physical}`
      : system.cpu_count ?? "-";
    $("size-workers").textContent = summary.total_workers || 0;
    $("size-busy").textContent = summary.busy_workers || 0;
    $("size-queued").textContent = summary.queued_jobs || 0;
    $("size-total-ram").textContent = fmtGb(system.ram_total_mb);

    const history = data.history || [];
    $("footer-count").textContent = `${history.length} history`;
    renderLatency(data.latency_ms || {});
    const workers = data.workers || [];
    const queue = data.queue || [];
    const liveJobs = enrichJobPriorities(data.live_jobs || [], workers, queue);
    renderWorkers(workers);
    renderRunningJobs(liveJobs);
    renderQueue(queue);
    renderHistory(history);
    renderPriority(data.queue_by_priority || {});
    renderStageDonut(liveJobs);
    renderPerCoreCpu(system);
    renderPeaks(liveJobs, history, data.resource_peaks || {});
    $("last-updated").textContent = `Updated ${fmtTime(data.updated_at)}`;
  }

  function renderPerCoreCpu(system) {
    const grid = $("cpu-core-grid");
    if (!grid) return;

    const perCore = system.cpu_per_core || {};
    const entries = Object.entries(perCore)
      .map(([name, value]) => [name, Number(value)])
      .filter(([, value]) => Number.isFinite(value))
      .sort(([a], [b]) => Number(a.replace("core_", "")) - Number(b.replace("core_", "")));

    $("cpu-core-summary").textContent = entries.length
      ? `${entries.length} logical${system.cpu_count_physical ? ` / ${system.cpu_count_physical} physical` : ""}`
      : "No data";

    if (!entries.length) {
      grid.innerHTML = `<div class="empty-state compact"><i class="fas fa-microchip"></i><span>No per-core CPU samples</span></div>`;
      return;
    }

    grid.innerHTML = entries.map(([name, rawValue]) => {
      const value = Math.max(0, Math.min(100, rawValue));
      const level = value >= 85 ? "hot" : value >= 60 ? "warm" : "cool";
      return `<div class="cpu-core-card ${level}">
        <div class="cpu-core-top">
          <span>${safe(name.replace("_", " "))}</span>
          <strong>${fmtPct(value)}</strong>
        </div>
        <div class="cpu-core-track"><div class="cpu-core-fill" style="width:${value}%"></div></div>
      </div>`;
    }).join("");
  }

  function enrichJobPriorities(jobs, workers, queue) {
    const priorityByUser = {};
    workers.forEach((worker) => {
      if (worker.current_user) {
        priorityByUser[worker.current_user] = worker.priority ?? worker.current_priority;
      }
    });
    queue.forEach((item) => {
      if (item.user_id) priorityByUser[item.user_id] = item.priority;
    });
    return jobs.map((job) => ({
      ...job,
      priority: job.priority ?? priorityByUser[job.user_id],
    }));
  }

  function renderLatency(lat) {
    $("lat-avg").textContent = fmtMs(lat.avg);
    $("lat-p50").textContent = fmtMs(lat.p50);
    $("lat-p90").textContent = fmtMs(lat.p90);
    $("lat-p95").textContent = fmtMs(lat.p95);
    $("lat-max").textContent = fmtMs(lat.max);
  }

  function renderPeaks(liveJobs, history, resourcePeaks = {}) {
    const all = [...liveJobs, ...history];
    let jobRamPeak = 0;
    let jobCpuPeak = 0;
    let freecadPeak = 0;
    let freecadCpuPeak = 0;
    all.forEach((job) => {
      const peaks = job.peaks || {};
      jobRamPeak = Math.max(jobRamPeak, Number(peaks.system_ram_mb || 0));
      jobCpuPeak = Math.max(jobCpuPeak, Number(peaks.system_cpu_percent || 0));
      freecadPeak = Math.max(freecadPeak, Number(peaks.freecad_rss_mb || 0));
      freecadCpuPeak = Math.max(freecadCpuPeak, Number(peaks.freecad_cpu_percent || 0));
    });
    const serverRamPeak = Number(resourcePeaks.system_ram_used_mb || 0) || jobRamPeak;
    const serverCpuPeak = Number(resourcePeaks.system_cpu_percent || 0) || jobCpuPeak;
    $("stat-ram-peak").textContent = serverRamPeak ? fmtMb(serverRamPeak) : "-";
    $("stat-cpu-peak").textContent = serverCpuPeak ? fmtPct(serverCpuPeak) : "-";
    $("live-peaks").innerHTML = [
      `<span class="chip">Server RAM ${fmtMb(serverRamPeak)}</span>`,
      `<span class="chip">Server CPU ${fmtPct(serverCpuPeak)}</span>`,
      `<span class="chip">FreeCAD ${fmtMb(freecadPeak)}</span>`,
      `<span class="chip">FreeCAD CPU ${fmtPct(freecadCpuPeak)}</span>`,
      `<span class="chip">Peak @ ${fmtTime(resourcePeaks.timestamp)}</span>`,
      `<span class="chip">Live ${liveJobs.length}</span>`,
    ].join("");
  }

  function renderStageDonut(jobs) {
    const counts = {};
    jobs.forEach((job) => {
      const stage = job.stage || job.status || "unknown";
      counts[stage] = (counts[stage] || 0) + 1;
    });
    const entries = Object.entries(counts);
    const colors = ["#4d9ef5", "#34d399", "#f59e42", "#a78bfa", "#2dd4bf", "#f87171"];
    $("stage-legend").innerHTML = entries.length
      ? entries.map(([name, count], idx) => `<div class="legend-item"><span class="legend-dot" style="background:${colors[idx % colors.length]}"></span>${safe(name)} <span class="muted">x${count}</span></div>`).join("")
      : `<span class="empty-hint">No active jobs</span>`;

    const chartData = {
      labels: entries.map(([name]) => name),
      datasets: [{ data: entries.map(([, count]) => count), backgroundColor: colors, borderWidth: 0 }]
    };
    const opts = { responsive: true, maintainAspectRatio: false, cutout: "62%", plugins: { legend: { display: false } } };
    if (!state.stageChart) state.stageChart = new Chart($("stage-chart"), { type: "doughnut", data: chartData, options: opts });
    else { state.stageChart.data = chartData; state.stageChart.update("none"); }
  }

  function jobProgress(job) {
    const raw = job.progress;
    if (raw == null) return 0;
    return Math.max(0, Math.min(100, Number(raw)));
  }

  function renderRunningJobs(jobs) {
    const list = $("running-jobs");
    const running = jobs.filter((job) => (job.status || "").toLowerCase() === "running");
    if (!running.length) {
      list.innerHTML = `<div class="empty-state"><i class="fas fa-satellite-dish"></i><span>No FreeCAD request running</span></div>`;
      return;
    }

    list.innerHTML = running.map((job) => {
      const peaks = job.peaks || {};
      const freecad = job.freecad_process || {};
      const worker = job.worker_process || {};
      const progress = jobProgress(job);
      return `<div class="request-card">
        <div class="request-card-top">
          <div class="id-text" title="${safe(job.user_id)}">${safe(shortId(job.user_id, 24))}</div>
          <div class="priority-badge" title="Request priority">Priority ${fmtPriority(job.priority)}</div>
        </div>
        <div class="meta-row">
          <span>${safe(job.stage || "running")}</span>
          <span>worker ${safe(shortId(job.worker_id, 18))}</span>
          <span>FreeCAD ${fmtMb(freecad.rss_mb)} / ${fmtPct(freecad.cpu_percent)}</span>
          <span>Worker ${fmtMb(worker.rss_mb)} / ${fmtPct(worker.cpu_percent)}</span>
          <span>Peak ${fmtMb(peaks.freecad_rss_mb)}</span>
        </div>
        <div class="meta-row"><span>${safe(job.message || "")}</span></div>
        <div class="progress-track"><div class="progress-fill" style="width:${progress}%"></div></div>
      </div>`;
    }).join("");
  }

  function renderQueue(queue) {
    $("queue-count-label").textContent = `${queue.length} waiting`;
    const list = $("queue-list");
    if (!queue.length) {
      list.innerHTML = `<div class="empty-state"><i class="fas fa-inbox"></i><span>Queue is empty</span></div>`;
      return;
    }
    list.innerHTML = queue.slice(0, 60).map((item) => `<div class="job-card">
      <div class="card-top">
        <div>
          <div class="id-text">${safe(shortId(item.user_id, 28))}</div>
          <div class="meta-row"><span>#${item.position}</span><span>${safe(item.queue_name)}</span><span>${safe(item.estimated_wait_time)}</span></div>
        </div>
        <div class="priority-badge" title="Request priority">Priority ${fmtPriority(item.priority)}</div>
      </div>
    </div>`).join("");
  }

  function renderHistory(history) {
    $("history-count-label").textContent = `${history.length} saved`;
    const list = $("history-list");
    if (!history.length) {
      list.innerHTML = `<div class="empty-state"><i class="fas fa-clock"></i><span>No saved history yet</span></div>`;
      return;
    }
    list.innerHTML = history.slice(0, 80).map((job) => {
      const peaks = job.peaks || {};
      const status = (job.status || "unknown").toLowerCase();
      return `<div class="job-card">
        <div class="card-top">
          <div>
            <div class="id-text">${safe(shortId(job.user_id, 28))}</div>
            <div class="meta-row">
              <span>${fmtMs(job.duration_ms)}</span>
              <span>priority ${fmtPriority(job.priority)}</span>
              <span>FreeCAD pk ${fmtMb(peaks.freecad_rss_mb)}</span>
              <span>CPU pk ${fmtPct(peaks.freecad_cpu_percent)}</span>
            </div>
          </div>
          <div class="card-badges">
            <div class="priority-badge" title="Request priority">Priority ${fmtPriority(job.priority)}</div>
            <div class="status-badge ${safe(status)}">${safe(status)}</div>
          </div>
        </div>
      </div>`;
    }).join("");
  }

  function renderWorkers(workers) {
    const list = $("worker-list");
    if (!workers.length) {
      list.innerHTML = `<div class="empty-state"><i class="fas fa-user-gear"></i><span>No workers found</span></div>`;
      return;
    }
    list.innerHTML = workers.map((worker) => {
      const status = (worker.status || "unknown").toLowerCase();
      return `<div class="worker-card ${safe(status)}">
        <div class="card-top">
          <div class="id-text" title="${safe(worker.worker_id)}">${safe(shortId(worker.worker_id, 26))}</div>
          <div class="status-badge ${safe(status)}">${safe(status)}</div>
        </div>
        <div class="meta-row">
          <span>user ${safe(shortId(worker.current_user, 18))}</span>
          <span>priority ${fmtPriority(worker.priority ?? worker.current_priority)}</span>
          <span>${safe(worker.queue_name || "-")}</span>
          <span>${worker.progress == null ? "-" : `${worker.progress}%`}</span>
        </div>
      </div>`;
    }).join("");
  }

  function renderPriority(queueByPriority) {
    const entries = Object.entries(queueByPriority).sort((a, b) => Number(b[0]) - Number(a[0]));
    const max = Math.max(1, ...entries.map(([, count]) => Number(count)));
    const list = $("priority-list");
    if (!entries.length) {
      list.innerHTML = `<div class="empty-state"><i class="fas fa-arrow-up-wide-short"></i><span>No waiting priority buckets</span></div>`;
      return;
    }
    list.innerHTML = entries.map(([priority, count]) => `<div class="priority-card">
      <div class="priority-badge">P${safe(priority)}</div>
      <div class="priority-bar"><div class="priority-fill" style="width:${Number(count) * 100 / max}%"></div></div>
      <div class="mono">${fmtNum(count)}</div>
    </div>`).join("");
  }

  async function renderResourceCharts() {
    const limit = Math.max(90, Math.ceil(state.resourceWindowMinutes * 75));
    const data = await fetchJson(`/freecad/monitor/resources?limit=${limit}`);
    const cutoff = Date.now() - state.resourceWindowMinutes * 60 * 1000;
    const samples = (data.samples || []).filter((sample) => {
      const ts = new Date(sample.timestamp).getTime();
      return Number.isFinite(ts) && ts >= cutoff;
    });

    const labels = samples.map((sample) => fmtTime(sample.timestamp));
    const systemRam = samples.map((sample) => pickServerRam(sample.system));
    const apiRam = samples.map((sample) => sample.api_process?.rss_mb || sample.worker_process?.rss_mb || 0);
    const freecadRam = samples.map((sample) => sample.freecad_process?.rss_mb || 0);
    const systemCpu = samples.map((sample) => pickServerCpu(sample.system));
    const apiCpu = samples.map((sample) => sample.api_process?.cpu_percent || sample.worker_process?.cpu_percent || 0);
    const freecadCpu = samples.map((sample) => sample.freecad_process?.cpu_percent || 0);

    const baseOptions = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#9aa3b8", boxWidth: 10, font: { size: 10 } } } },
      scales: {
        x: { grid: { color: "rgba(255,255,255,0.04)" }, ticks: { color: "#5c6480", maxTicksLimit: 5, font: { size: 10 } } },
        y: { grid: { color: "rgba(255,255,255,0.06)" }, ticks: { color: "#5c6480", font: { size: 10 } } }
      }
    };

    const ramData = {
      labels,
      datasets: [
        { label: "System RAM", data: systemRam, borderColor: RESOURCE_COLORS.system, backgroundColor: RESOURCE_COLORS.systemFill, tension: 0.35, fill: true, pointRadius: 0 },
        { label: "API/Worker RSS", data: apiRam, borderColor: RESOURCE_COLORS.apiWorker, tension: 0.35, pointRadius: 0 },
        { label: "FreeCAD RSS", data: freecadRam, borderColor: RESOURCE_COLORS.freecad, tension: 0.35, pointRadius: 0 },
      ]
    };
    if (!state.ramChart) state.ramChart = new Chart($("ram-chart"), { type: "line", data: ramData, options: baseOptions });
    else { state.ramChart.data = ramData; state.ramChart.update("none"); }

    const cpuData = {
      labels,
      datasets: [
        { label: "System CPU", data: systemCpu, borderColor: RESOURCE_COLORS.system, tension: 0.35, pointRadius: 0 },
        { label: "API/Worker CPU", data: apiCpu, borderColor: RESOURCE_COLORS.apiWorker, tension: 0.35, pointRadius: 0 },
        { label: "FreeCAD CPU", data: freecadCpu, borderColor: RESOURCE_COLORS.freecad, tension: 0.35, pointRadius: 0 },
      ]
    };
    if (!state.cpuChart) state.cpuChart = new Chart($("cpu-chart"), { type: "line", data: cpuData, options: baseOptions });
    else { state.cpuChart.data = cpuData; state.cpuChart.update("none"); }

    const latest = samples[samples.length - 1];
    if (latest) {
      $("resource-readout").textContent = `${fmtTime(latest.timestamp)} | RAM ${fmtMb(pickServerRam(latest.system))} | CPU ${fmtPct(pickServerCpu(latest.system))} | FreeCAD ${fmtMb(latest.freecad_process?.rss_mb)}`;
    }
  }

  async function refresh() {
    if (state.paused) return;
    try {
      const overview = await fetchJson("/freecad/monitor/overview");
      state.lastOverview = overview;
      renderOverview(overview);
      await renderResourceCharts();
      $("pulse-dot").classList.remove("error");
    } catch (err) {
      $("service-state").textContent = `Monitor error: ${err.message}`;
      $("redis-state").textContent = "Redis unknown";
      $("pulse-dot").classList.add("error");
    }
  }

  function bindEvents() {
    $("btn-refresh").addEventListener("click", refresh);
    $("btn-pause").addEventListener("click", () => {
      state.paused = !state.paused;
      $("btn-pause").classList.toggle("paused", state.paused);
      $("btn-pause").innerHTML = state.paused
        ? `<i class="fas fa-play"></i><span>Resume</span>`
        : `<i class="fas fa-pause"></i><span>Pause</span>`;
      $("pulse-dot").classList.toggle("paused", state.paused);
    });
    $("btn-export").addEventListener("click", () => {
      window.open("/freecad/monitor/export", "_blank");
    });
    document.querySelectorAll(".chart-range").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.resourceWindowMinutes = Number(btn.dataset.minutes || 1);
        document.querySelectorAll(".chart-range").forEach((item) => item.classList.remove("active"));
        btn.classList.add("active");
        renderResourceCharts();
      });
    });
  }

  bindEvents();
  refresh();
  state.timer = setInterval(refresh, 1000);
})();
