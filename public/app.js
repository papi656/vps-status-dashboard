/* VPS Telemetry Console — client renderer.
   Framework-free: fetch JSON, paint the DOM, poll. No build step. */

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/* ---------------------------------------------------------------- formatting */

const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];
function fmtBytes(bytes, digits = 1) {
  if (bytes == null || Number.isNaN(bytes)) return "—";
  let v = Number(bytes), i = 0;
  while (Math.abs(v) >= 1024 && i < UNITS.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${UNITS[i]}`;
}
const fmtRate = (bps) => (bps == null ? "—" : `${fmtBytes(bps)}/s`);
const fmtPct = (v, d = 1) => (v == null ? "—" : `${Number(v).toFixed(d)}%`);
const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));

function fmtUptime(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}D ${String(h).padStart(2, "0")}H ${String(m).padStart(2, "0")}M`;
}
function fmtDuration(s) {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}
function ago(seconds) {
  if (seconds == null) return "—";
  return seconds < 5 ? "now" : `${fmtDuration(seconds)} ago`;
}
const clock = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(11, 19) + "Z" : "—");
const stamp = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(0, 19).replace("T", " ") + "Z" : "—");

function tone(pct, warn, crit) {
  if (pct == null) return "";
  if (pct >= crit) return "err";
  if (pct >= warn) return "amb";
  return "ok";
}

/* --------------------------------------------------------------------- theme */

const THEME_KEY = "vpsdash.theme";
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  document.querySelectorAll("[data-theme-set]").forEach((b) =>
    b.classList.toggle("active", b.dataset.themeSet === theme)
  );
  try { localStorage.setItem(THEME_KEY, theme); } catch {}
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch {}
  const prefersLight = window.matchMedia?.("(prefers-color-scheme: light)").matches;
  applyTheme(saved || (prefersLight ? "light" : "dark"));
  document.querySelectorAll("[data-theme-set]").forEach((b) =>
    b.addEventListener("click", () => applyTheme(b.dataset.themeSet))
  );
}

/* ---------------------------------------------------------------- small bits */

function setText(id, value) { const el = $(id); if (el) el.textContent = value; }
function setValue(id, value, cls) {
  const el = $(id);
  if (!el) return;
  el.firstChild.nodeValue = value;
  if (cls !== undefined) el.className = `big-value ${cls}`;
}
function kvRow(k, v, cls = "") {
  return `<div class="row"><span class="k">${k}</span><span class="v ${cls}">${v}</span></div>`;
}

/* ------------------------------------------------------------------ renders */

let lastSampleTs = null;
let lastHistory = null;
let live = false;

function renderCpu(s) {
  const cpu = s.cpu;
  const t = tone(cpu.total_pct, 70, 90);
  setValue("cpu-value", cpu.total_pct.toFixed(1), t);
  setText("cpu-meta", `${cpu.per_core_pct.length} LOGICAL CORES`);
  const state = $("cpu-state");
  state.textContent = cpu.total_pct >= 90 ? "CRITICAL" : cpu.total_pct >= 70 ? "ELEVATED" : "NOMINAL";
  state.className = `state ${cpu.total_pct >= 90 ? "crit" : cpu.total_pct >= 70 ? "warn" : ""}`;

  $("cpu-cores").innerHTML = cpu.per_core_pct
    .map((p, i) => {
      const c = tone(p, 70, 90);
      return `<div class="core-row"><span>CORE ${String(i).padStart(2, "0")}: ${p.toFixed(1)}%</span>
        <span class="core-track"><span class="core-fill ${c}" style="width:${clamp(p, 0, 100)}%"></span></span></div>`;
    })
    .join("") || `<div class="core-row dim">per-core telemetry unavailable</div>`;
}

function renderDisks(s) {
  const disks = s.disks || [];
  const total = disks.reduce((a, d) => a + d.total, 0);
  const used = disks.reduce((a, d) => a + d.used, 0);
  const pct = total ? (used / total) * 100 : 0;
  const t = tone(pct, 75, 90);
  setValue("disk-value", pct.toFixed(1), t);
  setText("disk-total", `${fmtBytes(used)} / ${fmtBytes(total)}`);
  setText("disk-meta", `${disks.length} MOUNT${disks.length === 1 ? "" : "S"}`);

  const palette = ["ok", "cy", "amb", "err"];
  $("disk-bar").innerHTML =
    disks
      .map((d, i) => {
        const w = total ? (d.used / total) * 100 : 0;
        const color = `var(--${palette[i % palette.length]})`;
        return `<span class="seg" style="width:${w}%;background:${color}" title="${d.mount} ${fmtBytes(d.used)}"></span>`;
      })
      .join("") + `<span class="seg free" style="flex:1"></span>`;

  $("disk-list").innerHTML =
    disks
      .map((d) => {
        const p = d.total ? (d.used / d.total) * 100 : 0;
        return kvRow(
          d.mount,
          `${fmtBytes(d.used)} / ${fmtBytes(d.total)} (${p.toFixed(0)}%)`,
          tone(p, 75, 90)
        );
      })
      .join("") || `<div class="row dim">no block devices reported</div>`;

  setText("disk-read", s.diskio.reset ? "RESET" : fmtRate(s.diskio.read_per_s));
  setText("disk-write", s.diskio.reset ? "RESET" : fmtRate(s.diskio.write_per_s));
}

function renderMem(s) {
  const m = s.mem;
  const pct = m.total ? (m.used / m.total) * 100 : 0;
  const cached = Math.max(0, m.total - m.used - m.available);
  const t = tone(pct, 75, 90);
  setValue("mem-value", pct.toFixed(1), t);
  setText("mem-total", `${fmtBytes(m.used)} / ${fmtBytes(m.total)}`);
  setText("mem-meta", pct >= 90 ? "CRITICAL > 90%" : pct >= 75 ? "CAUTION > 75%" : "NOMINAL");

  const usedW = m.total ? (m.used / m.total) * 100 : 0;
  const cacheW = m.total ? (cached / m.total) * 100 : 0;
  $("mem-bar").innerHTML =
    `<span class="seg" style="width:${usedW}%;background:var(--ok)"></span>` +
    `<span class="seg" style="width:${cacheW}%;background:var(--amb)"></span>` +
    `<span class="seg free" style="flex:1"></span>`;

  $("mem-list").innerHTML =
    kvRow("ACTIVE / COMMITTED", fmtBytes(m.used)) +
    kvRow("BUFFER / CACHE (EST)", fmtBytes(cached)) +
    kvRow("AVAILABLE HEADROOM", fmtBytes(m.available), "ok");

  const swapPct = m.swap_total ? (m.swap_used / m.swap_total) * 100 : 0;
  setText("mem-swap", m.swap_total ? `${fmtBytes(m.swap_used)} / ${fmtBytes(m.swap_total)} (${swapPct.toFixed(0)}%)` : "NOT CONFIGURED");
  setText("mem-avail", fmtBytes(m.available));
}

function renderNet(s) {
  const n = s.net;
  const rx = n.rx_per_s, tx = n.tx_per_s;
  const combined = (rx || 0) + (tx || 0);
  setValue("net-value", n.reset ? "—" : fmtBytes(combined), n.reset ? "" : "cy");
  setText("net-note", n.reset ? "COUNTER RESET" : `${fmtBytes(s.diskio.read_per_s)} disk read`);
  setText("net-meta", n.reset ? "REBOOT DETECTED" : "LIVE RATE");

  setText("net-rx", n.reset ? "—" : fmtRate(rx));
  setText("net-tx", n.reset ? "—" : fmtRate(tx));
  setText("net-rx-total", `${fmtBytes(n.rx_bytes)} TOTAL ACC`);
  setText("net-tx-total", `${fmtBytes(n.tx_bytes)} TOTAL ACC`);
  setText("net-io-r", fmtRate(s.diskio.read_per_s));
  setText("net-io-w", fmtRate(s.diskio.write_per_s));
}

function renderProcesses(s) {
  const topCpu = s.processes.top_cpu || [];
  const topMem = s.processes.top_mem || [];
  const seen = new Set();
  const rows = [];
  for (const p of topCpu) { seen.add(p.pid); rows.push(p); }
  for (const p of topMem) if (!seen.has(p.pid)) rows.push(p);

  setText("proc-counts", `${fmtNum(s.processes.count)} TOTAL // TOP ${rows.length} LISTED`);
  setText("proc-count-inline", fmtNum(s.processes.count));

  const maxCpu = Math.max(1, ...rows.map((p) => p.cpu_pct));
  $("proc-body").innerHTML =
    rows
      .map((p) => {
        const cw = clamp((p.cpu_pct / maxCpu) * 100, 0, 100);
        const mw = clamp(p.mem_pct, 0, 100);
        return `<tr>
          <td class="pid">${p.pid}</td>
          <td class="name">${escapeHtml(p.name)}</td>
          <td class="mono">${p.cpu_pct.toFixed(1)}%</td>
          <td class="mono">${p.mem_pct.toFixed(1)}%</td>
          <td class="bar-cell"><span class="bar-inline"><i class="ok" style="width:${cw}%;background:var(--ok)"></i><i style="width:${mw}%;background:var(--cy)"></i></span></td>
        </tr>`;
      })
      .join("") ||
    `<tr><td colspan="5" class="dim">no process telemetry reported (names only, by design)</td></tr>`;
}

function renderReceiver(stats, sample) {
  const cadence = stats.cadence_s ? `${stats.cadence_s}s` : "—";
  $("rx-kv").innerHTML =
    kvRow("LAST SAMPLE", sample ? `${ago(sample.age_s)}` : "—", sample && sample.age_s > 180 ? "amb" : "ok") +
    kvRow("SAMPLES STORED", fmtNum(stats.samples)) +
    kvRow("DETECTED CADENCE", cadence, "cy") +
    kvRow("RETENTION", `${stats.retention_hours}h samples / ${stats.scan_retention_days}d scans`) +
    kvRow("STORE SIZE", fmtBytes(stats.db_bytes)) +
    kvRow("FIRST SAMPLE", stamp(stats.first_ts)) +
    kvRow("INGEST AUTH", "BEARER TOKEN REQUIRED", "ok");

  const cad = stats.cadence_s ? `${stats.cadence_s}s FIXED DB CADENCE` : "—";
  setText("chip-cadence", cad);
  setText("hdr-sync", cad);
  setText("bottom-retention", `${stats.retention_hours}H SAMPLES`);
  setText("bottom-host", stats.hostname || "—");
}

function renderScan(scan) {
  if (!scan) {
    setText("scan-age", "NO SCAN");
    $("scan-dirs").innerHTML = `<div class="row dim">no disk-space scan received yet (runs every 12h)</div>`;
    $("scan-files").innerHTML = "";
    return;
  }
  const ageS = Math.max(0, Math.floor(Date.now() / 1000) - scan.scanned_at);
  setText("scan-age", `${ago(ageS)}`);
  const render = (items, withBytes) => {
    const max = Math.max(1, ...items.map((i) => i.bytes));
    return items
      .map((i) => {
        const w = (i.bytes / max) * 100;
        const label = i.path.length > 26 ? "…" + i.path.slice(-25) : i.path;
        return `<div class="row"><span class="k" title="${escapeHtml(i.path)}">${escapeHtml(label)}</span><span class="v">${fmtBytes(i.bytes)}</span></div>
          <div class="bar-inline" style="margin-bottom:.25rem"><i style="width:${w}%;background:var(--ok)"></i></div>`;
      })
      .join("");
  };
  $("scan-dirs").innerHTML =
    `<div class="row"><span class="k dim">TOP DIRECTORIES</span><span class="v dim">SCANNED ${stamp(scan.scanned_at)}</span></div>` +
    (render(scan.top_dirs) || `<div class="row dim">—</div>`);
  $("scan-files").innerHTML =
    `<div class="row"><span class="k dim">TOP FILES</span><span class="v dim">&nbsp;</span></div>` +
    (render(scan.top_files) || `<div class="row dim">—</div>`);
}

function renderBanner(s, stats) {
  setText("host-badge", (s.hostname || "UNKNOWN").toUpperCase());
  setText("banner-host", s.hostname);
  setText("banner-cores", String(s.cpu.per_core_pct.length));
  setText("banner-uptime", fmtUptime(s.uptime_s));
  setText("hdr-uptime", fmtUptime(s.uptime_s));
  setText("chip-mem", fmtBytes(s.mem.total));
  const diskTotal = (s.disks || []).reduce((a, d) => a + d.total, 0);
  setText("chip-disk", fmtBytes(diskTotal));
  setText("load-vals", s.load.map((l) => `[${l.toFixed(2)}]`).join(" "));
  setText("net-total-inline", `${fmtBytes(s.net.rx_bytes)} / ${fmtBytes(s.net.tx_bytes)}`);
  void stats;
}

/* ------------------------------------------------------------------- chart */

let chartState = null;

function buildChart(history) {
  const pts = (history?.points || []).filter((p) => p.cpu != null);
  if (pts.length < 2) {
    chartState = null;
    $("chart").innerHTML = `<div class="dim mono" style="height:220px;display:flex;align-items:center;justify-content:center">NOT ENOUGH HISTORY YET — WAITING FOR MORE SAMPLES</div>`;
    setText("chart-note", "—");
    return;
  }
  const W = 1000, H = 260, PL = 38, PR = 46, PT = 12, PB = 22;
  const iw = W - PL - PR, ih = H - PT - PB;
  const step = Math.max(1, Math.ceil(pts.length / 320));
  const data = pts.filter((_, i) => i % step === 0 || i === pts.length - 1);

  const maxNet = Math.max(1, ...data.map((p) => p.rx || 0));
  const x = (i) => PL + (i / (data.length - 1)) * iw;
  const yPct = (v) => PT + ih - (clamp(v, 0, 100) / 100) * ih;
  const yNet = (v) => PT + ih - (clamp((v || 0) / maxNet, 0, 1) / 1) * ih;

  const line = (key, mapper) =>
    data.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${mapper(p[key]).toFixed(1)}`).join(" ");
  const area = (key, mapper) =>
    `${line(key, mapper)} L${x(data.length - 1).toFixed(1)},${PT + ih} L${x(0).toFixed(1)},${PT + ih} Z`;

  const gridY = [0, 25, 50, 75, 100]
    .map((g) => {
      const y = yPct(g);
      return `<line x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}" stroke="var(--line)" stroke-width="1"/>
              <text x="${PL - 6}" y="${y + 3}" fill="var(--muted)" font-size="9" text-anchor="end">${g}</text>`;
    })
    .join("");

  const ticks = [0, Math.floor((data.length - 1) / 2), data.length - 1]
    .map((i) => `<text x="${x(i)}" y="${H - 6}" fill="var(--muted)" font-size="9" text-anchor="middle">${clock(data[i].ts)}</text>`)
    .join("");

  $("chart").innerHTML = `
    <div class="chart-svg">
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="CPU and memory history">
        ${gridY}
        <path d="${area("rx", yNet)}" fill="var(--amb)" opacity="0.08"/>
        <path d="${line("rx", yNet)}" fill="none" stroke="var(--amb)" stroke-width="1" opacity="0.45"/>
        <path d="${area("mem", yPct)}" fill="var(--cy)" opacity="0.10"/>
        <path d="${line("mem", yPct)}" fill="none" stroke="var(--cy)" stroke-width="1.4"/>
        <path d="${area("cpu", yPct)}" fill="var(--ok)" opacity="0.12"/>
        <path d="${line("cpu", yPct)}" fill="none" stroke="var(--ok)" stroke-width="1.6"/>
        ${ticks}
        <text x="${W - PR + 6}" y="${PT + 8}" fill="var(--amb)" font-size="9" text-anchor="start">${fmtBytes(maxNet)}/s</text>
      </svg>
    </div>
    <div class="chart-cursor" id="chart-cursor" hidden></div>
    <span class="chart-dot ok" id="dot-cpu" hidden></span>
    <span class="chart-dot cy" id="dot-mem" hidden></span>
    <span class="chart-dot amb" id="dot-net" hidden></span>
    <div class="chart-tip" id="chart-tip" hidden></div>`;

  chartState = { data, W, H, PL, iw, yPct, yNet };

  const last = pts[pts.length - 1];
  const span = pts[pts.length - 1].ts - pts[0].ts;
  setText("chart-note", `${pts.length} PTS / ${fmtDuration(span)} // CPU ${last.cpu.toFixed(1)}% MEM ${last.mem.toFixed(1)}%`);
}

function hideChartTip() {
  for (const id of ["chart-cursor", "dot-cpu", "dot-mem", "dot-net", "chart-tip"]) {
    const el = $(id);
    if (el) el.hidden = true;
  }
}

function bindChartHover() {
  const wrap = $("chart");
  wrap.addEventListener("mousemove", (event) => {
    if (!chartState) return;
    const { data, W, H, PL, iw, yPct, yNet } = chartState;
    const rect = wrap.getBoundingClientRect();
    const ratio = clamp((event.clientX - rect.left) / rect.width, 0, 1);
    const svgX = ratio * W;
    const idx = Math.round(clamp((svgX - PL) / iw, 0, 1) * (data.length - 1));
    const p = data[idx];

    const xPct = (svgX / W) * 100;
    const cursor = $("chart-cursor");
    cursor.hidden = false;
    cursor.style.left = `${xPct}%`;

    const place = (id, topPct) => {
      const el = $(id);
      if (!el) return;
      el.hidden = false;
      el.style.left = `${xPct}%`;
      el.style.top = `${topPct}%`;
    };
    place("dot-cpu", (yPct(p.cpu) / H) * 100);
    place("dot-mem", (yPct(p.mem) / H) * 100);
    place("dot-net", (yNet(p.rx) / H) * 100);

    const tip = $("chart-tip");
    tip.hidden = false;
    tip.innerHTML = `
      <div class="tip-ts">${stamp(p.ts)}</div>
      <div class="tip-row"><i class="sw ok"></i>CPU <b>${fmtPct(p.cpu)}</b></div>
      <div class="tip-row"><i class="sw cy"></i>MEM <b>${fmtPct(p.mem)}</b></div>
      <div class="tip-row"><i class="sw amb"></i>NET RX <b>${fmtRate(p.rx)}</b></div>`;

    const px = ratio * rect.width;
    const tw = tip.offsetWidth;
    let left = px + 14;
    if (left + tw > rect.width) left = px - 14 - tw;
    tip.style.left = `${Math.max(2, left)}px`;
    tip.style.top = "10px";
  });
  wrap.addEventListener("mouseleave", hideChartTip);
}

function renderSpark(history) {
  const pts = (history?.points || []).filter((p) => p.cpu != null).slice(-60);
  if (pts.length < 2) { $("cpu-spark").innerHTML = ""; return; }
  const W = 200, H = 44;
  const x = (i) => (i / (pts.length - 1)) * W;
  const y = (v) => H - 2 - (clamp(v, 0, 100) / 100) * (H - 4);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.cpu).toFixed(1)}`).join(" ");
  $("cpu-spark").innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <path d="${d}" fill="none" stroke="var(--ok)" stroke-width="1.4"/>
    </svg>`;
}

function renderRing(history) {
  const pts = (history?.points || []).slice(-8).reverse();
  if (!pts.length) {
    $("ring").innerHTML = `<div class="line dim">awaiting samples…</div>`;
    setText("ring-note", "FIFO 0/8");
    return;
  }
  setText("ring-note", `TAIL ${pts.length}/8`);
  $("ring").innerHTML = pts
    .map((p) => {
      const flag = p.cpu >= 90 || p.mem >= 90 ? "wrn" : "nom";
      const label = p.cpu >= 90 || p.mem >= 90 ? "[WRN]" : "[NOM]";
      return `<div class="line">
        <span class="ts">${clock(p.ts)}</span>
        <span class="flag ${flag}">${label}</span>
        <span>CPU ${p.cpu.toFixed(1)}% MEM ${p.mem.toFixed(1)}% NET ${fmtRate(p.rx)}</span>
      </div>`;
    })
    .join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* -------------------------------------------------------------- tick / live */

function tickClock() {
  const now = Math.floor(Date.now() / 1000);
  if (lastSampleTs) {
    const age = Math.max(0, now - lastSampleTs);
    setText("foot-lag", fmtDuration(age));
    setText("foot-last", clock(lastSampleTs));
    const stale = age > 180;
    const badge = $("liveness-badge");
    if (badge) {
      badge.classList.toggle("stale", stale);
      setText("liveness-label", stale ? "TELEMETRY STALE // NO RECENT SNAPSHOT" : "PASSIVE OBSERVER // AIR-GAPPED DB SINK");
    }
    setText("bottom-sync", stale ? "[STALE]" : "[SYNCHRONIZED]");
    const b = $("bottom-sync");
    if (b) b.className = stale ? "amb" : "ok";
  }
  const hex = (lastSampleTs || 0).toString(16).padStart(8, "0");
  setText("foot-hash", `RAW_STREAM: 0x${hex}_DAEMON_OK`);
}

/* ------------------------------------------------------------------- fetch */

async function fetchJSON(url) {
  const res = await fetch(url, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

let currentRange = "24h";

async function refreshCurrent() {
  try {
    const data = await fetchJSON("/api/current");
    live = true;
    $("empty-state").hidden = !!data.sample;
    if (!data.sample) { lastSampleTs = null; return; }
    lastSampleTs = data.sample.ts;
    renderBanner(data.sample, data.stats);
    renderCpu(data.sample);
    renderDisks(data.sample);
    renderMem(data.sample);
    renderNet(data.sample);
    renderProcesses(data.sample);
    renderReceiver(data.stats, data.sample);
    renderScan(data.disk_scan);
    tickClock();
  } catch (err) {
    live = false;
    setText("liveness-label", "RECEIVER UNREACHABLE");
    $("liveness-badge")?.classList.add("stale");
    console.error(err);
  }
}

async function refreshHistory() {
  try {
    const data = await fetchJSON(`/api/history?range=${currentRange}`);
    lastHistory = data;
    buildChart(data);
    renderSpark(data);
    renderRing(data);
  } catch (err) {
    console.error(err);
  }
}

function initRange() {
  document.querySelectorAll("[data-range]").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll("[data-range]").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      currentRange = b.dataset.range;
      refreshHistory();
    })
  );
}

/* -------------------------------------------------------------------- boot */

initTheme();
initRange();
bindChartHover();
refreshCurrent();
refreshHistory();
setInterval(refreshCurrent, 10_000);
setInterval(refreshHistory, 60_000);
setInterval(tickClock, 1_000);
