/* Small shared helpers: formatting, DOM, hash parsing. No dependencies. */

export const $  = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Escape untrusted text (paths, commit subjects, author names) for HTML. */
export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (value !== null && value !== undefined && value !== false) {
      if (key === "value") node.value = value; else node.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

/* ------------------------------------------------------------ numbers */
const INT = new Intl.NumberFormat("en-US");

export function fmtInt(n) {
  if (n === null || n === undefined) return "–";
  return INT.format(Math.round(n));
}

export function fmtCompact(n) {
  if (n === null || n === undefined) return "–";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(abs >= 1e10 ? 0 : 1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(abs >= 1e7 ? 0 : 1).replace(/\.0$/, "") + "M";
  if (abs >= 1e4) return (n / 1e3).toFixed(abs >= 1e5 ? 0 : 1).replace(/\.0$/, "") + "k";
  return INT.format(Math.round(n));
}

export function fmtSigned(n) {
  if (n === null || n === undefined) return "–";
  if (n > 0) return "+" + INT.format(n);
  return INT.format(n);
}

export function fmtPct(x, digits = 1) {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  return (x * 100).toFixed(digits) + "%";
}

export function fmtRate(x, digits = 2) {
  if (x === null || x === undefined) return "–";
  if (x === 0) return "0";
  if (Math.abs(x) < 0.01) return x.toExponential(1);
  return x.toFixed(digits);
}

/* --------------------------------------------------------------- time */
const D = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
const DT = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });

export const fmtDate = (ts) => (ts ? D.format(new Date(ts * 1000)) : "–");
export const fmtDateTime = (ts) => (ts ? DT.format(new Date(ts * 1000)) : "–");

export function fmtDay(ts) {           // unix day number -> date
  return D.format(new Date(ts * 86400 * 1000));
}

export function timeAgo(ts) {
  if (!ts) return "–";
  const seconds = Math.max(0, Date.now() / 1000 - ts);
  const steps = [[31536000, "y"], [2592000, "mo"], [604800, "w"], [86400, "d"], [3600, "h"], [60, "m"]];
  for (const [size, name] of steps) {
    if (seconds >= size) return `${Math.floor(seconds / size)}${name} ago`;
  }
  return "just now";
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds < 1) return (seconds * 1000).toFixed(0) + " ms";
  if (seconds < 60) return seconds.toFixed(1) + " s";
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds - m * 60)}s`;
}

/* ------------------------------------------------------------- dates (filters) */
/** "YYYY-MM-DD" (or epoch) -> unix seconds at UTC midnight. */
export function dateToEpoch(value) {
  if (!value) return null;
  if (typeof value === "number") return value;
  if (/^\d+$/.test(value)) return parseInt(value, 10);
  const ms = Date.parse(value + "T00:00:00Z");
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

export const epochToDate = (ts) =>
  ts === null || ts === undefined ? "" : new Date(ts * 1000).toISOString().slice(0, 10);

/* ---------------------------------------------------------------- misc */
export function debounce(fn, ms = 200) {
  let handle = null;
  return (...args) => {
    clearTimeout(handle);
    handle = setTimeout(() => fn(...args), ms);
  };
}

export function throttleRaf(fn) {
  let queued = false;
  return (...args) => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(...args); });
  };
}

export function copy(text) {
  navigator.clipboard?.writeText(text);
}

/** Colour ramp for churn magnitudes (used by treemap and bars). */
export function ramp(t) {
  t = Math.max(0, Math.min(1, t));
  const from = [46, 84, 168], to = [122, 164, 255];
  const c = from.map((v, i) => Math.round(v + (to[i] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

export function heat(t) {
  t = Math.max(0, Math.min(1, t));
  const alpha = 0.14 + 0.86 * Math.pow(t, 0.6);
  return `rgba(91, 140, 255, ${alpha.toFixed(3)})`;
}

/* ------------------------------------------------------------ hash urls */
/** Parse "#/r/<rid>/<view>(/<path>)?<query>" into a route object. */
export function parseHash(hash = location.hash) {
  const raw = (hash || "").replace(/^#\/?/, "");
  const [pathPart, queryPart] = raw.split("?");
  const query = new URLSearchParams(queryPart || "");
  const segments = pathPart.split("/").filter(Boolean).map(decodeURIComponent);
  if (!segments.length) return { view: "repos", query };
  if (segments[0] === "add") return { view: "add", query };
  if (segments[0] !== "r" || segments.length < 2) return { view: "repos", query };
  const rid = segments[1];
  const view = ["overview", "browse", "commits", "authors"].includes(segments[2])
    ? segments[2] : "overview";
  const rest = segments.slice(3).join("/");
  return { view, rid, path: rest, query };
}

export function buildHash(view, { rid, path, params } = {}) {
  let hash = "#/";
  if (view === "add") return "#/add";
  if (!rid) return "#/";
  hash = `#/r/${encodeURIComponent(rid)}/${view}`;
  if (path) hash += "/" + path.split("/").map(encodeURIComponent).join("/");
  const query = params instanceof URLSearchParams ? params.toString() : (params || "");
  if (query) hash += "?" + query;
  return hash;
}

export function navigate(view, parts) {
  const hash = buildHash(view, parts);
  if (location.hash === hash) window.dispatchEvent(new HashChangeEvent("hashchange"));
  else location.hash = hash;
}
