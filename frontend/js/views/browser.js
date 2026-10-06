/* Explorer: drill through the measured tree.  Left = directory listing with
   the spec's per-child metrics, right = details of the selected object
   (file metrics 2.1 / directory metrics 2.2, ownership ω 2.5, renames). */

import { api } from "../api.js";
import { donutChart, sparkChart } from "../charts.js";
import { openCommit } from "../commitModal.js";
import { badge, filterBar } from "../components.js";
import { el, fmtCompact, fmtDate, fmtInt, fmtPct, fmtRate, fmtSigned, navigate } from "../util.js";

export async function renderBrowser(main, ctx) {
  const { repo, filters, identities } = ctx.state;
  const path = ctx.state.path || "";
  main.append(el("div", { class: "loading" }, el("span", { class: "spinner" }), " exploring…"));

  let dirData, fileData = null;
  try {
    const first = await api.browse(repo.id, filters, path, undefined);
    if (first.target.kind === "file") {
      const parent = path.split("/").slice(0, -1).join("/");
      [dirData, fileData] = await Promise.all([
        api.browse(repo.id, filters, parent, "dir"),
        first,
      ]);
    } else {
      dirData = first;
    }
  } catch (error) {
    main.innerHTML = "";
    main.append(el("div", { class: "alert bad" }, error.message));
    return;
  }
  if (ctx.token !== ctx.state.token) return;

  main.innerHTML = "";
  ctx.state.commitSet = dirData.commit_set;

  main.append(filterBar({
    repo, refs: ctx.state.refs, filters, identities,
    commitSet: dirData.commit_set, onChange: ctx.setFilters,
    extra: [el("a", { class: "btn btn-sm", href: api.exportUrl(repo.id, "dirs", filters) }, "Export CSV")],
  }));

  main.append(breadcrumbs(repo, dirData, fileData, ctx));

  const tableCard = el("div", { class: "card pad-0" });
  const panel = el("div", { class: "side-panel" });
  main.append(el("div", { class: "explorer" }, tableCard, panel));

  buildListing(tableCard, dirData, fileData, ctx);
  buildPanel(panel, fileData || dirData, ctx);
}

/* ------------------------------------------------------------ chrome */
function breadcrumbs(repo, dirData, fileData, ctx) {
  const box = el("div", { class: "breadcrumbs" });
  const crumbs = fileData ? fileData.breadcrumbs : dirData.breadcrumbs;
  crumbs.forEach((crumb, index) => {
    if (index) box.append(el("span", { class: "sep" }, "/"));
    const isLast = index === crumbs.length - 1;
    box.append(el("a", {
      class: isLast ? "here" : "",
      href: "#",
      onclick: (event) => {
        event.preventDefault();
        navigate("browse", { rid: repo.id, path: crumb.path, params: ctx.filtersQuery() });
      },
    }, index === 0 ? `⌂ ${crumb.name}` : crumb.name));
  });
  box.append(el("span", { class: "grow" }));
  box.append(el("a", {
    class: "btn btn-sm", href: "#",
    onclick: (event) => { event.preventDefault(); navigate("overview", { rid: repo.id, params: ctx.filtersQuery() }); },
  }, "Dashboard"));
  const list = el("button", { class: "btn btn-sm", title: "copy this path", onclick: () => navigator.clipboard?.writeText(
    fileData ? fileData.target.path : dirData.target.path) }, "copy path");
  box.append(list);
  return box;
}

/* ----------------------------------------------------------- listing */
const COLUMNS = [
  ["name", "name", ""],
  ["churn", "λ", "num"],
  ["added", "l+", "num pos"],
  ["removed", "l−", "num neg"],
  ["growth", "δ", "num"],
  ["mods", "n", "num"],
  ["mod_freq", "η", "num"],
  ["churn_rate", "ρ", "num"],
  ["owner", "owner", ""],
  ["extra", "", "num"],
];

function buildListing(card, dirData, fileData, ctx) {
  const repo = dirData.repo || ctx.state.repo;
  const selectedPath = fileData ? fileData.target.path : null;
  let sortKey = "churn", sortDir = -1, filterText = "";

  card.append(el("div", { class: "card-head", style: "padding:14px 16px 0" },
    el("div", { class: "card-title" }, dirData.target.path || "/ (repository root)"),
    el("span", { class: "card-sub" }, `${fmtInt(dirData.children.length)} immediate children`),
    el("span", { class: "spacer" }),
    inputFilter()));

  const table = el("table", { class: "data", style: "margin-top:10px" });
  const thead = el("thead");
  const tbody = el("tbody");
  table.append(thead, tbody);
  card.append(table);

  function inputFilter() {
    const input = el("input", { type: "search", placeholder: "filter children…", style: "width:200px" });
    input.addEventListener("input", () => { filterText = input.value.toLowerCase(); paint(); });
    return input;
  }

  function buildHead() {
    thead.innerHTML = "";
    const row = el("tr");
    for (const [key, label, cls] of COLUMNS) {
      const th = el("th", { class: cls + " sortable" },
        label, sortKey === key ? el("span", { class: "arrow" }, sortDir > 0 ? "▲" : "▼") : null);
      th.addEventListener("click", () => {
        if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = key === "name" ? 1 : -1; }
        paint();
      });
      row.append(th);
    }
    thead.append(row);
  }

  function paint() {
    buildHead();
    tbody.innerHTML = "";
    const children = (dirData.children || []).filter(child =>
      !filterText || child.path.toLowerCase().includes(filterText));
    const value = (child) => {
      if (sortKey === "name") return child.name.toLowerCase();
      if (sortKey === "owner") return child.owner?.label || "~";
      if (sortKey === "extra") return child.type === "dir" ? child.files : (child.size ?? -1);
      return child[sortKey];
    };
    children.sort((a, b) => {
      const va = value(a), vb = value(b);
      if (typeof va === "string" || typeof vb === "string") {
        return sortDir * String(va).localeCompare(String(vb));
      }
      return sortDir * ((vb ?? -1) - (va ?? -1));
    });

    if (!children.length) {
      tbody.append(el("tr", {}, el("td", { colspan: COLUMNS.length },
        el("div", { class: "empty" }, filterText ? "no children match the filter" : "this directory has no measured children"))));
      return;
    }
    for (const child of children) {
      const isFile = child.type === "file";
      const row = el("tr", {
        class: "row-link" + (selectedPath === child.path ? " selected" : ""),
        onclick: () => navigate("browse", { rid: ctx.state.repo.id, path: child.path, params: ctx.filtersQuery() }),
      });
      row.append(el("td", {}, el("span", { class: "path", title: child.path },
        (isFile ? "📄 " : "📁 ") + child.name,
        isFile && child.present === false ? badge("deleted", "bad") : null,
        isFile && child.submodule ? badge("submodule", "warn") : null)));
      row.append(el("td", { class: "num" }, fmtInt(child.churn)));
      row.append(el("td", { class: "num pos" }, fmtInt(child.added)));
      row.append(el("td", { class: "num neg" }, fmtInt(child.removed)));
      row.append(el("td", { class: "num" }, fmtSigned(child.growth)));
      row.append(el("td", { class: "num" }, fmtInt(child.mods)));
      row.append(el("td", { class: "num" }, fmtRate(child.mod_freq, 3)));
      row.append(el("td", { class: "num" }, fmtRate(child.churn_rate, 2)));
      row.append(el("td", { class: "nowrap" },
        child.owner ? el("span", { title: `top owner: ${(child.owner.share * 100).toFixed(0)}% of churn` },
          child.owner.label, el("span", { class: "dim" }, ` ${(child.owner.share * 100).toFixed(0)}%`)) : "–"));
      row.append(el("td", { class: "num dim" },
        isFile ? `#${fmtInt(child.size)}` : `${fmtInt(child.files)} files · ${fmtInt(child.subdirs)} dirs`));
      tbody.append(row);
    }
  }
  paint();
}

/* ------------------------------------------------------------- panel */
function buildPanel(panel, data, ctx) {
  const obj = data.object;
  const isFile = data.target.kind === "file";

  const head = el("div", { class: "card" });
  head.append(el("div", { class: "card-head" },
    el("div", { class: "card-title" }, isFile ? "File metrics" : "Directory metrics"),
    badge(isFile ? "2.1 file" : "2.2 directory", "accent")));
  head.append(el("div", { class: "mono", style: "word-break:break-all;margin-bottom:10px" }, obj.path || "/"),
    obj.submodule ? badge("submodule", "warn") : null,
    isFile && obj.present ? badge("present in HEAD", "good") : null,
    isFile && !obj.present ? badge("deleted before HEAD", "bad") : null);
  head.append(el("div", { class: "grid cols-2", style: "gap:8px" },
    metric("l+", fmtInt(obj.added), "pos"),
    metric("l−", fmtInt(obj.removed), "neg"),
    metric("δ", fmtSigned(obj.growth)),
    metric("λ", fmtInt(obj.churn)),
    metric("n modifications", fmtInt(obj.mods)),
    metric("η frequency", fmtRate(obj.mod_freq, 3)),
    metric("ρ churn rate", fmtRate(obj.churn_rate, 3)),
    isFile ? metric("size", `${fmtCompact(obj.size)} B`) : metric("files", fmtInt(obj.files)),
    isFile ? metric("commits touching", fmtInt(obj.touching_commits)) : metric("subdirs", fmtInt(obj.subdirs)),
    isFile ? metric("first seen", fmtDate(obj.first)) : metric("dirs touched", fmtInt(obj.dirs_touched))));
  head.append(el("div", { class: "hint", style: "margin-top:8px" },
    `${fmtDate(obj.first)} → ${fmtDate(obj.last)} · ${fmtInt(obj.authors)} authors · ${fmtInt(obj.touching_commits ?? 0)} commits touched it`));
  panel.append(head);

  if (obj.series?.length) {
    const card = el("div", { class: "card" });
    card.append(el("div", { class: "card-head" },
      el("div", { class: "card-title" }, "History"),
      el("span", { class: "card-sub" }, "l+ / l− per commit"),
      el("span", { class: "spacer" }),
      el("span", { class: "hint" }, `${fmtInt(obj.series.length)} points`)));
    const host = el("div", {});
    card.append(host);
    panel.append(card);
    sparkChart(host, obj.series, { height: 70 });
  }

  if (obj.ownership?.length) {
    const card = el("div", { class: "card" });
    card.append(el("div", { class: "card-head" },
      el("div", { class: "card-title" }, "Ownership ω"),
      el("span", { class: "card-sub" }, "λH,o,a share per author")));
    const host = el("div", {});
    card.append(host);
    panel.append(card);
    donutChart(host, obj.ownership.map(o => ({ label: o.label, value: o.churn, id: o.id })),
      { size: 150, onSelect: (item) => ctx.setFilters({ authors: [item.id] }) });
  }

  if (isFile && obj.renames?.length) {
    const card = el("div", { class: "card" });
    card.append(el("div", { class: "card-head" },
      el("div", { class: "card-title" }, "Rename history"),
      el("span", { class: "card-sub" }, "detected at 50% similarity")));
    const list = el("ul", { class: "list-plain renames", style: "padding-left:16px;margin:0" });
    for (const rename of obj.renames) {
      list.append(el("li", {},
        el("span", { class: "mono" }, rename.old_path),
        " → ",
        el("a", { href: "#", onclick: (event) => { event.preventDefault(); openCommit(ctx.state.repo.id, rename.commit); } },
          `${rename.commit.slice(0, 8)}`),
        el("span", { class: "dim" }, ` · ${fmtDate(rename.ct)} · ${rename.subject}`)));
    }
    card.append(list);
    panel.append(card);
  }
}

function metric(label, value, kind = "") {
  return el("div", { class: "metric " + kind },
    el("div", { class: "label" }, label), el("div", { class: "value", style: "font-size:15px" }, value));
}

void fmtPct;
