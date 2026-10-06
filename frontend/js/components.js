/* Shared UI components: toasts, modals, filter bar, pager, small widgets.
   Plain DOM + CSS classes from app.css; no framework. */

import { el, fmtInt, fmtCompact, fmtDate, parseHash, navigate, esc } from "./util.js";

/* ---------------------------------------------------------------- toast */
export function toast(message, { title = "", kind = "info", timeout = 5200 } = {}) {
  const node = el("div", { class: `toast ${kind}` });
  if (title) node.append(el("div", { class: "t-title" }, title));
  node.append(el("div", { class: "t-body" }, message));
  node.addEventListener("click", () => node.remove());
  document.getElementById("toasts").append(node);
  if (timeout) setTimeout(() => node.remove(), timeout);
  return node;
}

export const toastError = (message, title = "Something went wrong") =>
  toast(message, { title, kind: "error", timeout: 9000 });

/* ---------------------------------------------------------------- modal */
export function modal({ title, body, footer, wide = false, onClose } = {}) {
  const root = document.getElementById("modal-root");
  const content = el("div", { class: "modal-body" });
  if (typeof body === "string") content.innerHTML = body; else if (body) content.append(body);
  const box = el("div", { class: "modal" + (wide ? " wide" : "") },
    el("div", { class: "modal-head" },
      el("h2", {}, title || ""),
      el("span", { class: "spacer" }),
      el("button", { class: "close-x", title: "Close (Esc)", onclick: () => close() }, "✕")),
    content,
    footer ? el("div", { class: "modal-foot" }, footer) : null);
  const back = el("div", { class: "modal-back", onclick: (event) => {
    if (event.target === back) close();
  } }, box);
  root.append(back);
  document.body.classList.add("modal-open");
  const onKey = (event) => { if (event.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    back.remove();
    if (!root.children.length) document.body.classList.remove("modal-open");
    onClose?.();
  }
  return { close, body: content, box };
}

export function confirmDialog({ title, message, okLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    let answer = false;
    const handle = modal({
      title,
      body: el("div", {}, message),
      footer: [
        el("button", { class: "btn", onclick: () => handle.close() }, "Cancel"),
        el("button", {
          class: "btn " + (danger ? "btn-danger" : "btn-primary"),
          onclick: () => { answer = true; handle.close(); },
        }, okLabel),
      ],
      onClose: () => resolve(answer),
    });
  });
}

export function promptDialog({ title, label, value = "", placeholder = "", okLabel = "Save" }) {
  return new Promise((resolve) => {
    let answer = null;
    const input = el("input", { type: "text", value, placeholder, style: "width:100%" });
    const handle = modal({
      title,
      body: el("label", { class: "field" }, el("span", {}, label || ""), input),
      footer: [
        el("button", { class: "btn", onclick: () => handle.close() }, "Cancel"),
        el("button", { class: "btn btn-primary", onclick: () => { answer = input.value; handle.close(); } }, okLabel),
      ],
      onClose: () => resolve(answer),
    });
    input.focus();
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { answer = input.value; handle.close(); }
    });
  });
}

/* ------------------------------------------------------------- widgets */
export function metricCard({ label, value, sub = "", kind = "", tip = "" }) {
  return el("div", { class: `card metric ${kind}` },
    el("div", { class: "label" },
      tip ? el("span", { class: "tip", title: tip }, label) : label),
    el("div", { class: "value" }, value),
    sub ? el("div", { class: "sub" }, sub) : null);
}

export function badge(text, kind = "") { return el("span", { class: `badge ${kind}` }, text); }

export function progressBar(fraction, indeterminate = false) {
  const bar = el("div", { class: "progress" + (indeterminate ? " indeterminate" : "") },
    el("div", { style: `width:${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%` }));
  return bar;
}

export const STATUS_KIND = {
  ready: "good", analyzing: "accent", ingesting: "accent", queued: "warn",
  error: "bad", cancelled: "warn",
};

export function statusPill(repo) {
  const status = repo.status || "unknown";
  return badge(status, STATUS_KIND[status] || "");
}

export function repoJobLine(repo) {
  const job = repo.job || {};
  if (!job.phase || job.finished) return null;
  const phases = { ingest: "ingesting", analyze: "analysing", diff: "reading line stats",
                   graph: "reading commit graph", normalise: "building tables", write: "writing index" };
  return el("div", { style: "display:flex;flex-direction:column;gap:5px" },
    el("div", { class: "dim", style: "font-size:11.5px" },
      (phases[job.phase] || job.phase) + (job.message ? ` · ${job.message}` : "")),
    progressBar(job.progress || 0, !job.progress));
}

export function emptyState(title, text, action = null) {
  return el("div", { class: "empty" }, el("h3", {}, title), el("p", {}, text), action);
}

export function pager({ page, pages, total, size, onPage }) {
  const from = total === 0 ? 0 : (page - 1) * size + 1;
  const to = Math.min(total, page * size);
  return el("div", { class: "pager" },
    el("span", { class: "info" }, `${fmtInt(from)}–${fmtInt(to)} of ${fmtInt(total)} commits`),
    el("button", { class: "btn btn-sm", disabled: page <= 1, onclick: () => onPage(page - 1) }, "‹ Prev"),
    el("span", { class: "dim" }, `page ${page} / ${pages}`),
    el("button", { class: "btn btn-sm", disabled: page >= pages, onclick: () => onPage(page + 1) }, "Next ›"));
}

/** bar + value, used for author leaderboards */
export function barRow({ label, value, max, colour = "#5b8cff", meta = "", onclick }) {
  const width = max > 0 ? Math.max(1.5, (value / max) * 100) : 0;
  return el("div", {
    class: "bar-row" + (onclick ? " clickable" : ""),
    onclick: onclick || undefined,
    title: label,
  },
    el("div", { class: "bar-label" }, label),
    el("div", { class: "bar-track" }, el("div", { class: "bar-fill", style: `width:${width}%;background:${colour}` })),
    el("div", { class: "bar-val" }, meta || fmtCompact(value)));
}

/* ------------------------------------------------------------ filter bar */
const MODE_LABEL = { all: "All history", range: "Time window", list: "Manual selection" };

export function filterBar({ repo, refs, filters, identities, commitSet, onChange, extra = [] }) {
  const bar = el("div", { class: "filterbar" });

  // ---- commit set mode
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Commit set" });
  for (const mode of ["all", "range", "list"]) {
    seg.append(el("button", {
      class: filters.mode === mode ? "on" : "",
      title: {
        all: "H̄ — every non-merge commit reachable from the reference",
        range: "H_i,j — commits whose committer date lies in a window",
        list: "H — an explicit list of commits chosen in the Commits view",
      }[mode],
      onclick: () => switchMode(mode),
    }, MODE_LABEL[mode]));
  }
  bar.append(seg);

  // ---- time window inputs
  if (filters.mode === "range") {
    const from = el("input", { type: "date", value: filters.from || "", title: "from (inclusive, UTC)" });
    const to = el("input", { type: "date", value: filters.to || "", title: "to (inclusive, UTC)" });
    from.addEventListener("change", () => onChange({ from: from.value || null }));
    to.addEventListener("change", () => onChange({ to: to.value || null }));
    bar.append(el("div", { class: "fgroup" }, el("span", { class: "dim" }, "from"), from,
                  el("span", { class: "dim" }, "to"), to));
  }

  // ---- manual selection note
  if (filters.mode === "list") {
    const count = filters.commits?.length || 0;
    bar.append(el("div", { class: "fgroup" },
      badge(count ? `${fmtInt(count)} commits` : "nothing selected", count ? "violet" : "warn"),
      el("a", { class: "hint", href: `#/r/${encodeURIComponent(repo.id)}/commits${hashQuery(filters)}`,
                onclick: (event) => { event.preventDefault(); navigate("commits", { rid: repo.id, params: filtersToQuery(filters) }); } },
        "pick commits →"),
      el("button", { class: "btn btn-sm btn-ghost", onclick: () => onChange({ mode: "all", commits: [] }) }, "clear")));
  }

  // ---- reference
  if (refs?.length) {
    const select = el("select", { title: "reference h_r the commit set must be reachable from" });
    for (const ref of refs) {
      select.append(el("option", { value: ref.name, selected: (filters.ref || "HEAD") === ref.name ? "" : null },
        ref.name === "HEAD" ? "HEAD" : ref.name.replace(/^refs\/(heads|tags|remotes)\//, "")));
    }
    select.addEventListener("change", () => onChange({ ref: select.value }));
    bar.append(el("div", { class: "fgroup" }, el("span", { class: "dim" }, "ref"), select));
  }

  // ---- authors multiselect
  const groups = identities?.groups || [];
  const ms = el("div", { class: "multiselect" });
  const selected = new Set((filters.authors || []).map(String));
  const button = el("button", { class: "ms-btn" },
    selected.size ? `${selected.size} author${selected.size > 1 ? "s" : ""}` : "All authors",
    el("span", { class: "caret" }, "▾"));
  const menu = el("div", { class: "ms-menu", hidden: true });
  const options = el("div", {});
  let open = false;
  const close = () => { open = false; menu.hidden = true; };
  document.addEventListener("click", (event) => { if (open && !ms.contains(event.target)) close(); });

  function renderOptions() {
    options.innerHTML = "";
    const filterText = "";
    const list = groups.slice().sort((a, b) => commitsOf(b) - commitsOf(a));
    for (const group of list) {
      const checkbox = el("input", { type: "checkbox" });
      checkbox.checked = selected.has(String(group.id));
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) selected.add(String(group.id)); else selected.delete(String(group.id));
        paint();
      });
      const label = group.merged ? group.label : (group.name || group.label);
      options.append(el("label", { class: "ms-item" },
        checkbox,
        el("span", { class: "ms-name" }, label),
        el("span", { class: "ms-meta" }, `${fmtInt(commitsOf(group))} commits${group.merged ? " · merged" : ""}`)));
    }
    if (!list.length) options.append(el("div", { class: "dim", style: "padding:6px 8px" }, "no authors"));
    void filterText;
  }
  const commitsOf = (group) => (group.members || []).reduce((sum, m) => sum + (m.commits || 0), 0);

  function paint() {
    button.firstChild.textContent = selected.size
      ? `${selected.size} author${selected.size > 1 ? "s" : ""}` : "All authors";
    renderOptions();
  }

  menu.append(el("div", { class: "ms-actions" },
    el("button", { class: "btn btn-sm", onclick: () => { groups.forEach(g => selected.add(String(g.id))); paint(); } }, "All"),
    el("button", { class: "btn btn-sm", onclick: () => { selected.clear(); paint(); } }, "None"),
    el("button", { class: "btn btn-sm", onclick: () => {
      selected.clear();
      groups.slice().sort((a, b) => commitsOf(b) - commitsOf(a)).slice(0, 10)
        .forEach(g => selected.add(String(g.id)));
      paint();
    } }, "Top 10")));
  menu.append(options);
  paint();

  button.addEventListener("click", () => {
    open = !open;
    menu.hidden = !open;
  });
  ms.append(button, menu);
  bar.append(ms);

  // apply authors only when it changes (no need for an Apply button)
  menu.addEventListener("change", () => {
    onChange({ authors: [...selected].map(Number) });
  });

  bar.append(el("span", { class: "sep" }));
  bar.append(el("span", { class: "filter-summary", html: summaryHtml(commitSet, filters) }));

  bar.append(el("span", { class: "grow" }));
  for (const node of extra) bar.append(node);
  if (filters.mode !== "all" || (filters.authors || []).length || filters.ref && filters.ref !== "HEAD") {
    bar.append(el("button", {
      class: "btn btn-sm btn-ghost", title: "reset all filters",
      onclick: () => onChange({ mode: "all", from: null, to: null, commits: [], authors: [], ref: "HEAD" }),
    }, "Reset"));
  }

  function switchMode(mode) {
    const patch = { mode };
    if (mode === "range" && !filters.from && !filters.to) {
      patch.from = commitSet?.first ? isoDay(commitSet.first) : null;
      patch.to = commitSet?.last ? isoDay(commitSet.last) : null;
    }
    if (mode !== "list") patch.mode = mode;
    onChange(patch);
  }

  return bar;
}

function isoDay(ts) { return new Date(ts * 1000).toISOString().slice(0, 10); }

export function summaryHtml(commitSet, filters) {
  if (!commitSet) return "";
  const parts = [];
  const notation = { all: "H̄", range: "H_i,j", list: "H" }[filters.mode] || "H";
  parts.push(`${notation} = <b>${fmtInt(commitSet.count)}</b> commits`);
  if (commitSet.first && commitSet.last) {
    parts.push(`${fmtDate(commitSet.first)} → ${fmtDate(commitSet.last)}`);
  }
  if (commitSet.unparsed) {
    parts.push(`<span style="color:var(--warn)">${fmtInt(commitSet.unparsed)} unparsed</span>`);
  }
  if (commitSet.dropped) {
    parts.push(`<span style="color:var(--warn)">${fmtInt(commitSet.dropped)} hashes unknown</span>`);
  }
  return parts.join(" · ");
}

export function filtersToQuery(filters) {
  const query = new URLSearchParams();
  if (filters.mode && filters.mode !== "all") query.set("mode", filters.mode);
  if (filters.ref && filters.ref !== "HEAD") query.set("ref", filters.ref);
  if (filters.mode === "range") {
    if (filters.from) query.set("from", filters.from);
    if (filters.to) query.set("to", filters.to);
  }
  if (filters.mode === "list" && filters.commits?.length) query.set("commits", filters.commits.join(","));
  if (filters.authors?.length) query.set("authors", filters.authors.join(","));
  return query;
}

export function filtersFromQuery(query) {
  return {
    mode: query.get("mode") || "all",
    ref: query.get("ref") || "HEAD",
    from: query.get("from") || null,
    to: query.get("to") || null,
    commits: (query.get("commits") || "").split(",").filter(Boolean),
    authors: (query.get("authors") || "").split(",").filter(Boolean).map(Number),
  };
}

const hashQuery = (filters) => {
  const query = filtersToQuery(filters).toString();
  return query ? `?${query}` : "";
};
void hashQuery;

/** Hidden inputs helper for building markup fragments safely. */
export function html(strings, ...values) {
  return strings.reduce((out, chunk, index) => {
    const value = index < values.length ? values[index] : "";
    return out + chunk + (typeof value === "string" && value.includes("<") ? value : esc(value));
  }, "");
}

export function parseRoute() { return parseHash(); }
