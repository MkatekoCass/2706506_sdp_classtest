/* Hand-rolled SVG charts: no libraries, no CDN, fully offline.
   Every chart takes (container, data, options) and owns its own tooltip and
   resize handling.  Colours come from the CSS variables via the constants
   below so the charts match the app theme exactly. */

import { el, fmtCompact, fmtDate, fmtInt, fmtSigned, heat, ramp } from "./util.js";

const C = {
  added: "#34d399",
  removed: "#fb7185",
  churn: "#6366f1",
  cumulative: "#22d3ee",
  commits: "#fbbf24",
  text: "#95a5c8",
  line: "#1e2942",
};

const NS = "http://www.w3.org/2000/svg";
const svgNode = (tag, attrs = {}) => {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};

/** Floating tooltip attached to a chart container. */
function makeTip(container) {
  const tip = el("div", { class: "chart-tip", style: "display:none" });
  container.append(tip);
  let bounds = null;
  return {
    show(event, html) {
      bounds = container.getBoundingClientRect();
      tip.innerHTML = html;
      tip.style.display = "block";
      const rect = tip.getBoundingClientRect();
      let x = event.clientX - bounds.left + 12;
      let y = event.clientY - bounds.top + 12;
      if (x + rect.width > bounds.width) x = bounds.width - rect.width - 4;
      if (y + rect.height > bounds.height + 60) y = event.clientY - bounds.top - rect.height - 10;
      tip.style.left = Math.max(0, x) + "px";
      tip.style.top = Math.max(0, y) + "px";
    },
    hide() { tip.style.display = "none"; },
  };
}

/** Re-render on container resize (debounced to one frame). */
function responsive(container, draw) {
  let frame = null;
  const run = () => { frame = null; draw(); };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(run); };
  draw();
  if (container._ratObserver) container._ratObserver.disconnect();
  const observer = new ResizeObserver(schedule);
  observer.observe(container);
  container._ratObserver = observer;
}

/** "Nice" axis ticks covering [min, max] with about `count` steps. */
function ticks(min, max, count = 5) {
  if (min === max) { min = Math.min(min, 0); max = max || 1; }
  const span = max - min;
  const rawStep = span / Math.max(1, count);
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * magnitude).find(s => s >= rawStep) || magnitude * 10;
  const start = Math.floor(min / step) * step;
  const out = [];
  for (let v = start; v <= max + step * 0.5; v += step) out.push(v);
  return out;
}

/* ======================================================================
   Timeline: mirrored l+/l- bars, optional churn bars, cumulative delta
   line and commit-count line.
   data = { points: [[t, added, removed, churn, commits, cumulative]],
            bucket, bucket_seconds, start, end }
   ====================================================================== */
export function timelineChart(container, data, opts = {}) {
  container.classList.add("chart");
  const height = opts.height || 250;
  const series = opts.series || { addRemove: true, churn: false, cumulative: true, commits: false };
  const tip = makeTip(container);

  function draw() {
    container.querySelectorAll("svg, .legend").forEach(n => n.remove());
    const width = Math.max(320, container.clientWidth);
    const margin = { top: 10, right: series.cumulative || series.commits ? 62 : 12, bottom: 22, left: 56 };
    const plotW = width - margin.left - margin.right;
    const plotH = height - margin.top - margin.bottom;
    const points = data.points || [];
    if (!points.length) { container.append(el("div", { class: "empty" }, "no activity in this commit set")); return; }

    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
    const g = svgNode("g", { transform: `translate(${margin.left},${margin.top})` });
    svg.append(g);

    const t0 = points[0][0], t1 = points[points.length - 1][0];
    const span = Math.max(1, t1 - t0);
    const maxLR = Math.max(1, ...points.map(p => Math.max(p[1], p[2])));
    const maxChurn = Math.max(1, ...points.map(p => p[3]));
    const yLeft = (v) => plotH - (v / maxLR) * plotH;
    const x = (t) => ((t - t0) / span) * plotW;

    const cumuls = points.map(p => p[5]);
    const cumMin = Math.min(0, ...cumuls), cumMax = Math.max(0, ...cumuls);
    const yRight = (v) => plotH - ((v - cumMin) / Math.max(1, cumMax - cumMin)) * plotH;
    const commitMax = Math.max(1, ...points.map(p => p[4]));
    const yCommits = (v) => plotH - (v / commitMax) * plotH * 0.92;

    // horizontal grid + left axis
    for (const value of ticks(0, maxLR, 4)) {
      const y = yLeft(value);
      g.append(svgNode("line", { class: "gridline", x1: 0, x2: plotW, y1: y, y2: y }));
      const label = svgNode("text", { x: -8, y: y + 3.5, "text-anchor": "end", fill: C.text });
      label.textContent = fmtCompact(value);
      g.append(label);
    }
    // zero line
    g.append(svgNode("line", { x1: 0, x2: plotW, y1: yLeft(0), y2: yLeft(0), stroke: "#33426e" }));

    const barW = Math.max(1, Math.min(14, plotW / points.length - 1));

    if (series.addRemove) {
      for (const p of points) {
        const cx = x(p[0]);
        if (p[1]) g.append(svgNode("rect", { x: cx - barW / 2, y: yLeft(p[1]), width: barW,
                                            height: Math.max(1, yLeft(0) - yLeft(p[1])), fill: C.added, opacity: .85 }));
        if (p[2]) g.append(svgNode("rect", { x: cx - barW / 2, y: yLeft(0), width: barW,
                                            height: Math.max(1, yLeft(p[2]) - yLeft(0)), fill: C.removed, opacity: .85 }));
      }
    }
    if (series.churn) {
      for (const p of points) {
        const cx = x(p[0]);
        const h = (p[3] / maxChurn) * plotH;
        if (h) g.append(svgNode("rect", { x: cx - barW / 2, y: plotH - h, width: barW,
                                          height: Math.max(1, h), fill: C.churn, opacity: .5 }));
      }
    }
    if (series.cumulative) {
      const path = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${yRight(p[5]).toFixed(1)}`).join("");
      g.append(svgNode("path", { d: path, fill: "none", stroke: C.cumulative, "stroke-width": 1.8 }));
    }
    if (series.commits) {
      const path = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${yCommits(p[4]).toFixed(1)}`).join("");
      g.append(svgNode("path", { d: path, fill: "none", stroke: C.commits, "stroke-width": 1.4, "stroke-dasharray": "3 3" }));
    }

    // x axis ticks
    const step = Math.max(1, Math.ceil(points.length / 8));
    for (let i = 0; i < points.length; i += step) {
      const label = svgNode("text", { x: x(points[i][0]), y: plotH + 15, "text-anchor": "middle", fill: C.text });
      label.textContent = shortDate(points[i][0], span);
      g.append(label);
    }
    if (series.cumulative || series.commits) {
      for (const value of ticks(cumMin, cumMax, 4)) {
        const label = svgNode("text", { x: plotW + 8, y: yRight(value) + 3.5, fill: C.text });
        label.textContent = fmtCompact(value);
        g.append(label);
      }
    }

    // hover: nearest bucket + crosshair
    const hover = svgNode("line", { y1: 0, y2: plotH, stroke: "#586cad", "stroke-dasharray": "2 3", opacity: 0 });
    g.append(hover);
    const hit = svgNode("rect", { x: 0, y: 0, width: plotW, height: plotH, fill: "transparent" });
    g.append(hit);
    hit.addEventListener("mousemove", (event) => {
      const rect = svg.getBoundingClientRect();
      const px = event.clientX - rect.left - margin.left;
      const index = Math.max(0, Math.min(points.length - 1, Math.round((px / plotW) * span / bucket(span, points.length))));
      const point = points[index];
      hover.setAttribute("x1", x(point[0]));
      hover.setAttribute("x2", x(point[0]));
      hover.setAttribute("opacity", 1);
      tip.show(event, `<div class="t-head">${fmtDate(point[0])}</div>
        <div><span style="color:${C.added}">+${fmtInt(point[1])}</span>
        &nbsp;<span style="color:${C.removed}">−${fmtInt(point[2])}</span></div>
        <div>λ churn ${fmtInt(point[3])} · ${fmtInt(point[4])} commits</div>
        <div>cumulative ${fmtSigned(point[5])}</div>`);
    });
    hit.addEventListener("mouseleave", () => { hover.setAttribute("opacity", 0); tip.hide(); });

    container.append(svg);

    // legend (clickable toggles)
    const legend = el("div", { class: "legend" });
    const keys = [
      ["addRemove", C.added, "l+ / l− per bucket"],
      ["churn", C.churn, "λ churn (l+ + l−)"],
      ["cumulative", C.cumulative, "cumulative δ"],
      ["commits", C.commits, "commits per bucket"],
    ];
    for (const [name, colour, label] of keys) {
      legend.append(el("span", {
        class: "key" + (series[name] ? "" : " off"),
        onclick: () => { series[name] = !series[name]; opts.onToggle?.(name); draw(); },
        title: label,
      }, el("span", { class: "swatch", style: `background:${colour}` }), label));
    }
    const bucketNote = el("span", { class: "dim" }, `bucket: ${data.bucket}`);
    legend.append(bucketNote);
    container.append(legend);
  }

  function bucket(span, count) { return span / Math.max(1, count); }
  responsive(container, draw);
}

function shortDate(ts, span) {
  const date = new Date(ts * 1000);
  if (span > 86400 * 360) return String(date.getUTCFullYear());
  return date.toLocaleDateString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
}

/* ======================================================================
   Calendar heatmap.  GitHub style (weeks x days) while the range is short
   enough to read; months x years grid beyond that.
   data = { cells: [[commits, churn]], origin_day, start, end }
   ====================================================================== */
export function calendarChart(container, data, opts = {}) {
  container.classList.add("chart");
  const metric = opts.metric || "commits";     // commits | churn
  const tip = makeTip(container);
  const dayMs = 86400;

  function draw() {
    container.querySelectorAll("svg").forEach(n => n.remove());
    const cells = data.cells || [];
    if (!cells.length) { container.append(el("div", { class: "empty" }, "no activity")); return; }
    const width = Math.max(320, container.clientWidth);
    const values = cells.map(c => metric === "commits" ? c[0] : c[1]);
    const max = Math.max(1, ...values);
    const style = cells.length > 800 ? "months" : "weeks";

    if (style === "months") drawMonths(container, cells, data.origin_day, values, max, width, tip, metric);
    else drawWeeks(container, cells, data.origin_day, values, max, width, tip, metric);
  }
  responsive(container, draw);
}

function drawWeeks(container, cells, originDay, values, max, width, tip, metric) {
  const gap = 2, cell = Math.max(9, Math.min(14, Math.floor(width / (Math.ceil(cells.length / 7) + 4))));
  const weeks = Math.ceil((cells.length + (originDay + 4) % 7) / 7);
  const height = 7 * (cell) + 16;
  const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
  const offset = (originDay + 4) % 7;               // 1970-01-01 was a Thursday
  // month labels
  let lastMonth = -1;
  for (let day = 0; day < cells.length; day++) {
    const t = (originDay + day) * 86400 * 1000;
    const month = new Date(t).getUTCMonth();
    if (month !== lastMonth) {
      const week = Math.floor((day + offset) / 7);
      const label = svgNode("text", { x: week * cell, y: 9, fill: C.text });
      label.textContent = new Date(t).toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" });
      svg.append(label);
      lastMonth = month;
    }
  }
  for (let day = 0; day < cells.length; day++) {
    const slot = day + offset;
    const rect = svgNode("rect", {
      x: Math.floor(slot / 7) * cell, y: 15 + (slot % 7) * cell,
      width: cell - 2, height: cell - 2, rx: 2, fill: heat(values[day] / max),
    });
    rect.addEventListener("mousemove", (event) => tip.show(event,
      `<div class="t-head">${fmtDate((originDay + day) * 86400)}</div>
       <div>${fmtInt(cells[day][0])} commits · churn ${fmtInt(cells[day][1])}</div>`));
    rect.addEventListener("mouseleave", () => tip.hide());
    svg.append(rect);
  }
  container.append(svg);
  const legend = el("div", { class: "legend" },
    el("span", { class: "dim" }, `${weeks} weeks · darker = more ${metric}`));
  container.append(legend);
}

function drawMonths(container, cells, originDay, values, max, width, tip, metric) {
  const cellH = 13, cellW = Math.max(10, Math.min(22, width / 26));
  const byYear = new Map();
  for (let day = 0; day < cells.length; day++) {
    const date = new Date((originDay + day) * 86400 * 1000);
    const key = date.getUTCFullYear();
    const month = date.getUTCMonth();
    if (!byYear.has(key)) byYear.set(key, { commits: new Array(12).fill(0), churn: new Array(12).fill(0) });
    byYear.get(key).commits[month] += cells[day][0];
    byYear.get(key).churn[month] += cells[day][1];
  }
  const years = [...byYear.keys()].sort();
  const height = years.length * cellH + 22;
  const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
  const months = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];
  months.forEach((name, month) => {
    const label = svgNode("text", { x: 44 + month * cellW + cellW / 2, y: 10, fill: C.text, "text-anchor": "middle" });
    label.textContent = name;
    svg.append(label);
  });
  let row = 0;
  const rowMax = {};
  for (const year of years) {
    const entry = byYear.get(year);
    const list = metric === "commits" ? entry.commits : entry.churn;
    for (const value of list) rowMax[year] = Math.max(rowMax[year] || 1, value);
    const tag = svgNode("text", { x: 38, y: 16 + row * cellH + cellH / 2 + 3, fill: C.text, "text-anchor": "end" });
    tag.textContent = year;
    svg.append(tag);
    list.forEach((value, month) => {
      const rect = svgNode("rect", {
        x: 44 + month * cellW, y: 16 + row * cellH, width: cellW - 2, height: cellH - 2, rx: 2,
        fill: heat(value / (rowMax[year] || 1)), stroke: "#0a0f1f",
      });
      rect.addEventListener("mousemove", (event) => tip.show(event,
        `<div class="t-head">${year}-${String(month + 1).padStart(2, "0")}</div>
         <div>${fmtInt(entry.commits[month])} commits · churn ${fmtInt(entry.churn[month])}</div>`));
      rect.addEventListener("mouseleave", () => tip.hide());
      svg.append(rect);
    });
    row += 1;
  }
  container.append(svg);
  container.append(el("div", { class: "legend" },
    el("span", { class: "dim" }, `monthly ${metric} (range too long for a weekly calendar)`)));
}

/* ======================================================================
   Squarified treemap of the directory tree (max 2 levels per render;
   click to drill in).
   ====================================================================== */
export function treemapChart(container, tree, opts = {}) {
  container.classList.add("chart");
  const tip = makeTip(container);
  const height = opts.height || 340;

  function layout(items, x, y, w, h) {
    const out = [];
    let total = items.reduce((sum, it) => sum + it.value, 0);
    if (total <= 0) return out;
    let remaining = items.map(it => ({ ...it, area: (it.value / total) * w * h }));
    let rx = x, ry = y, rw = w, rh = h;
    let index = 0;
    while (index < remaining.length) {
      const vertical = rw >= rh;
      const side = Math.max(1, vertical ? rh : rw);
      const row = [];
      let rowArea = 0, best = Infinity, j = index;
      while (j < remaining.length) {
        const candidate = [...row, remaining[j]];
        const rowLength = (rowArea + remaining[j].area) / side;
        let worst = 0;
        for (const it of candidate) {
          const size = it.area / Math.max(1e-9, rowLength);
          worst = Math.max(worst, Math.max(rowLength / size, size / rowLength));
        }
        if (worst <= best || row.length === 0) {
          row.push(remaining[j]);
          rowArea += remaining[j].area;
          best = worst;
          j += 1;
        } else break;
      }
      const rowLength = Math.max(1e-9, rowArea / side);
      let cursor = vertical ? ry : rx;
      for (const it of row) {
        const size = it.area / rowLength;
        out.push(vertical
          ? { ...it, x: rx, y: cursor, w: rowLength, h: size }
          : { ...it, x: cursor, y: ry, w: size, h: rowLength });
        cursor += size;
      }
      if (vertical) { rx += rowLength; rw -= rowLength; } else { ry += rowLength; rh -= rowLength; }
      index = j;
    }
    return out;
  }

  function draw() {
    container.querySelectorAll("svg").forEach(n => n.remove());
    const width = Math.max(320, container.clientWidth);
    const children = (tree?.children || []).filter(c => c.value > 0)
      .sort((a, b) => b.value - a.value).slice(0, 60);
    if (!children.length) { container.append(el("div", { class: "empty" }, "nothing measured yet")); return; }

    const margin = 2;
    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
    const max = children[0].value;
    const rects = layout(children, 0, 0, width, height);

    for (const node of rects) {
      const group = svgNode("g", { class: "treemap" });
      const rect = svgNode("rect", {
        x: node.x + margin, y: node.y + margin,
        width: Math.max(1, node.w - margin * 2), height: Math.max(1, node.h - margin * 2),
        rx: 3, fill: ramp(Math.sqrt(node.value / max) * 0.85 + 0.08),
        opacity: node.type === "file" ? .82 : 1,
      });
      group.append(rect);
      if (node.w > 52 && node.h > 24) {
        const label = svgNode("text", { x: node.x + 7, y: node.y + 17 });
        label.textContent = clip(node.name, node.w - 12);
        group.append(label);
        if (node.h > 40) {
          const sub = svgNode("text", { x: node.x + 7, y: node.y + 32, class: "small" });
          sub.textContent = `λ ${fmtCompact(node.value)}`;
          group.append(sub);
        }
      }
      group.addEventListener("mousemove", (event) => tip.show(event,
        `<div class="t-head">${node.type === "dir" ? "📁" : "📄"} ${escapeLabel(node.path) || node.name}</div>
         <div>λ churn ${fmtInt(node.value)} · n ${fmtInt(node.mods)}</div>
         <div><span style="color:${C.added}">+${fmtInt(node.added)}</span>
          <span style="color:${C.removed}">−${fmtInt(node.removed)}</span></div>
         <div class="dim">click to explore</div>`));
      group.addEventListener("mouseleave", () => tip.hide());
      group.addEventListener("click", () => opts.onSelect?.(node));
      svg.append(group);
    }
    container.append(svg);
    container.append(el("div", { class: "legend" },
      el("span", { class: "dim" }, "area = λ churn in the current commit set · click a tile to explore")));
  }

  const clip = (text, px) => text.length * 6.4 > px ? text.slice(0, Math.max(3, Math.floor(px / 6.4) - 1)) + "…" : text;
  const escapeLabel = (text) => text || "";
  responsive(container, draw);
}

/* ======================================================================
   Donut (author ownership ω).
   items = [{label, value, share, id}] colour assigned from a palette.
   ====================================================================== */
const PALETTE = ["#6366f1", "#22d3ee", "#34d399", "#fbbf24", "#fb7185", "#a78bfa",
                 "#38bdf8", "#f472b6", "#fb923c", "#2dd4bf", "#818cf8", "#e879f9"];

export function donutChart(container, items, opts = {}) {
  container.classList.add("chart");
  const tip = makeTip(container);
  const size = opts.size || 170;

  function draw() {
    container.querySelectorAll("svg, .legend").forEach(n => n.remove());
    const data = (items || []).filter(i => i.value > 0);
    if (!data.length) { container.append(el("div", { class: "empty" }, "no ownership data")); return; }
    const total = data.reduce((sum, i) => sum + i.value, 0);
    const svg = svgNode("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}` });
    const cx = size / 2, cy = size / 2, outer = size / 2 - 4, inner = outer * 0.62;
    let angle = -Math.PI / 2;
    const arc = (a0, a1, r0, r1) => {
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const p = (a, r) => [cx + Math.cos(a) * r, cy + Math.sin(a) * r];
      const [x0, y0] = p(a0, r1), [x1, y1] = p(a1, r1), [x2, y2] = p(a1, r0), [x3, y3] = p(a0, r0);
      return `M${x0},${y0} A${r1},${r1} 0 ${large} 1 ${x1},${y1} L${x2},${y2} A${r0},${r0} 0 ${large} 0 ${x3},${y3}Z`;
    };
    const legend = el("div", { class: "legend", style: "flex-direction:column;gap:4px" });
    data.forEach((item, index) => {
      const sweep = (item.value / total) * Math.PI * 2;
      const colour = PALETTE[index % PALETTE.length];
      const path = svgNode("path", { d: arc(angle, angle + sweep - 0.004, inner, outer), fill: colour });
      path.addEventListener("mousemove", (event) => tip.show(event,
        `<div class="t-head">${item.label}</div>
         <div>λ ${fmtInt(item.value)} · ${(item.value / total * 100).toFixed(1)}%</div>`));
      path.addEventListener("mouseleave", () => tip.hide());
      path.addEventListener("click", () => opts.onSelect?.(item));
      svg.append(path);
      angle += sweep;
      legend.append(el("span", { class: "key", onclick: () => opts.onSelect?.(item) },
        el("span", { class: "swatch", style: `background:${colour}` }),
        el("span", {}, `${item.label} `),
        el("span", { class: "dim" }, `${(item.value / total * 100).toFixed(1)}%`)));
    });
    const centre = svgNode("text", { x: cx, y: cy + 4, "text-anchor": "middle", fill: C.text, "font-size": 11 });
    centre.textContent = fmtCompact(total);
    svg.append(centre);
    container.append(el("div", { style: "display:flex;gap:14px;align-items:center;flex-wrap:wrap" }, svg, legend));
  }
  draw();
}

/* ======================================================================
   Sparkline: per-commit l+ / l- for one object.
   points = [[t, added, removed]]
   ====================================================================== */
export function sparkChart(container, points, opts = {}) {
  container.classList.add("chart");
  const tip = makeTip(container);
  const height = opts.height || 64;

  function draw() {
    container.querySelectorAll("svg").forEach(n => n.remove());
    const width = Math.max(160, container.clientWidth);
    const data = points || [];
    if (!data.length) { container.append(el("div", { class: "dim" }, "no changes")); return; }
    let max = 1;
    for (const p of data) max = Math.max(max, p[1], p[2]);
    const t0 = data[0][0], t1 = data[data.length - 1][0] || t0 + 1;
    const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (width - 2) + 1;
    const yAdd = (v) => height / 2 - (v / max) * (height / 2 - 3);
    const yRem = (v) => height / 2 + (v / max) * (height / 2 - 3);
    const svg = svgNode("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
    const areaAdd = data.map((p, i) => `${i ? "L" : "M"}${x(p[0])},${yAdd(p[1])}`).join("")
      + `L${x(t1)},${height / 2}L${x(t0)},${height / 2}Z`;
    const areaRem = data.map((p, i) => `${i ? "L" : "M"}${x(p[0])},${yRem(p[2])}`).join("")
      + `L${x(t1)},${height / 2}L${x(t0)},${height / 2}Z`;
    svg.append(svgNode("path", { d: areaAdd, fill: "#34d39930", stroke: C.added, "stroke-width": 1 }));
    svg.append(svgNode("path", { d: areaRem, fill: "#fb718530", stroke: C.removed, "stroke-width": 1 }));
    svg.append(svgNode("line", { x1: 0, x2: width, y1: height / 2, y2: height / 2, stroke: "#33426e" }));
    // invisible hover strip
    const hit = svgNode("rect", { x: 0, y: 0, width, height, fill: "transparent" });
    hit.addEventListener("mousemove", (event) => {
      const rect = svg.getBoundingClientRect();
      const ratio = (event.clientX - rect.left) / rect.width;
      const index = Math.max(0, Math.min(data.length - 1, Math.round(ratio * (data.length - 1))));
      const p = data[index];
      tip.show(event, `<div class="t-head">${fmtDate(p[0])}</div>
        <div><span style="color:${C.added}">+${fmtInt(p[1])}</span>
        <span style="color:${C.removed}">−${fmtInt(p[2])}</span></div>`);
    });
    hit.addEventListener("mouseleave", () => tip.hide());
    svg.append(hit);
    container.append(svg);
  }
  responsive(container, draw);
}
