/* Dashboard: repository metrics, activity timeline, calendar, treemap,
   author leaderboard and recent commits — all for the current commit set H. */

import { api } from "../api.js";
import { calendarChart, timelineChart, treemapChart } from "../charts.js";
import { openCommit } from "../commitModal.js";
import { barRow, badge, filterBar, metricCard } from "../components.js";
import { el, fmtCompact, fmtDate, fmtInt, fmtPct, fmtRate, fmtSigned, navigate } from "../util.js";

export async function renderOverview(main, ctx) {
  const { repo, filters, identities } = ctx.state;
  main.append(el("div", { class: "loading" }, el("span", { class: "spinner" }), " computing metrics…"));

  let data;
  try {
    data = await api.overview(repo.id, filters, { tree_metric: ctx.state.treeMetric || "churn" });
  } catch (error) {
    main.innerHTML = "";
    main.append(el("div", { class: "alert bad" }, error.message));
    return;
  }
  if (ctx.token !== ctx.state.token) return;      // a newer render superseded this one

  main.innerHTML = "";
  const commitSet = data.commit_set;
  ctx.state.commitSet = commitSet;

  main.append(filterBar({
    repo, refs: ctx.state.refs, filters, identities, commitSet,
    onChange: ctx.setFilters,
    extra: [
      el("a", { class: "btn btn-sm", href: api.exportUrl(repo.id, "files", filters),
                title: "download the file metrics of this commit set as CSV" }, "Export CSV"),
    ],
  }));

  for (const warning of repo.warnings || []) {
    main.append(el("div", { class: "alert" }, warning));
  }
  if (commitSet.partial && !(repo.warnings || []).length) {
    main.append(el("div", { class: "alert" },
      `${fmtInt(commitSet.unparsed)} commits reachable from ${commitSet.ref} were not part of the analysed history; metrics cover only the remaining ${fmtInt(commitSet.count)} commits.`));
  }

  appendMetrics(main, data);
  appendCharts(main, data, ctx);
  appendBottom(main, data, ctx);
}

/* --------------------------------------------------------- metric cards */
function appendMetrics(main, data) {
  const m = data.metrics;
  const cs = data.commit_set;
  const repo = data.repo;

  main.append(el("div", { class: "grid cols-4", style: "margin-bottom:14px" },
    metricCard({ label: "l⁺ added lines", value: fmtInt(m.added), kind: "good",
                 sub: "l⁺H,r — lines added in H", tip: "Spec 2.3: repository l+ = sum over all files" }),
    metricCard({ label: "l⁻ removed lines", value: fmtInt(m.removed), kind: "bad",
                 sub: "l⁻H,r — lines removed in H", tip: "Spec 2.3: repository l−" }),
    metricCard({ label: "δ growth", value: fmtSigned(m.growth), kind: m.growth >= 0 ? "accent" : "bad",
                 sub: "δH,r = l⁺ − l⁻", tip: "Spec 2.3: net growth" }),
    metricCard({ label: "λ churn", value: fmtInt(m.churn), kind: "accent",
                 sub: `ρ = ${fmtRate(m.churn_rate)} lines / commit`, tip: "Spec 2.3/2.4: churn λ = l⁺ + l⁻; churn rate ρH,r = λH,r / |H|" })));

  main.append(el("div", { class: "grid cols-6", style: "margin-bottom:14px" },
    metricCard({ label: "|H| commits", value: fmtInt(cs.count),
                 sub: cs.first ? `${fmtDate(cs.first)} → ${fmtDate(cs.last)}` : "empty set",
                 tip: "Number of commits in the current commit set: non-merge commits reachable from h_r, restricted by the filters" }),
    metricCard({ label: "contributing authors", value: fmtInt(m.authors),
                 sub: `${fmtInt(repo.stats?.identities ?? 0)} identities in the repo`,
                 tip: "Authors who changed at least one measured file within H" }),
    metricCard({ label: "files touched", value: fmtInt(m.files),
                 sub: `of ${fmtInt(repo.stats?.files ?? 0)} measured files`,
                 tip: "Distinct files with a measured change in H" }),
    metricCard({ label: "dirs touched", value: fmtInt(m.dirs),
                 sub: `of ${fmtInt(repo.stats?.dirs ?? 0)} directories`,
                 tip: "Directories whose subtree changed in H" }),
    metricCard({ label: "modifications n", value: fmtInt(m.mods),
                 sub: `η = ${fmtRate(m.mods / Math.max(1, cs.count), 3)} / commit`,
                 tip: "Spec 2.4: nH,r — commits in H that modified the repository; η = n / |H|" }),
    metricCard({ label: "churn rate ρ", value: fmtRate(m.churn_rate),
                 sub: `λ / |H| · ${fmtInt(Math.round(m.churn / Math.max(1, cs.count)))} lines per commit` })));
}

/* --------------------------------------------------------------- charts */
function appendCharts(main, data, ctx) {
  const { filters } = ctx.state;
  const repo = data.repo;

  // --- timeline
  const timelineCard = el("div", { class: "card" });
  timelineCard.append(el("div", { class: "card-head" },
    el("div", { class: "card-title" }, "Activity over time"),
    el("span", { class: "card-sub" }, "lines per bucket · toggles in the legend"),
    el("span", { class: "spacer" })));
  const timelineHost = el("div", {});
  timelineCard.append(timelineHost);
  timelineChart(timelineHost, data.timeline, { height: 236 });

  // --- calendar
  const calendarHost = el("div", {});
  const calendarCard = el("div", { class: "card" });
  const calendarToggle = el("div", { class: "seg" });
  let calendarMetric = ctx.state.calendarMetric || "commits";
  for (const [key, label] of [["commits", "commits"], ["churn", "churn"]]) {
    calendarToggle.append(el("button", {
      class: key === calendarMetric ? "on" : "",
      onclick: () => { calendarMetric = key; ctx.state.calendarMetric = key; paint(); },
    }, label));
  }
  calendarCard.append(el("div", { class: "card-head" },
    el("div", { class: "card-title" }, "Commit calendar"),
    el("span", { class: "spacer" }), calendarToggle));
  calendarCard.append(calendarHost);

  main.append(el("div", { class: "two-col", style: "margin-bottom:14px" },
    timelineCard, calendarCard));

  // --- treemap + authors
  const treeHost = el("div", {});
  const treeCard = el("div", { class: "card" });
  const treeToggle = el("div", { class: "seg" });
  const metrics = [["churn", "λ"], ["added", "l⁺"], ["removed", "l⁻"], ["mods", "n"]];
  for (const [key, label] of metrics) {
    treeToggle.append(el("button", {
      class: (ctx.state.treeMetric || "churn") === key ? "on" : "",
      onclick: async () => {
        ctx.state.treeMetric = key;
        const fresh = await api.tree(repo.id, filters, key);
        treemapChart(treeHost, fresh.tree, { height: 330, onSelect: (node) => openNode(node) });
        for (const [index, button] of [...treeToggle.children].entries()) {
          button.classList.toggle("on", metrics[index][0] === key);
        }
      },
    }, label));
  }
  treeCard.append(el("div", { class: "card-head" },
    el("div", { class: "card-title" }, "Directory treemap"),
    el("span", { class: "card-sub" }, "area = metric on the subtree"),
    el("span", { class: "spacer" }), treeToggle));
  treeCard.append(treeHost);
  treemapChart(treeHost, data.tree, { height: 330, onSelect: (node) => openNode(node) });

  const authorCard = el("div", { class: "card" });
  authorCard.append(el("div", { class: "card-head" },
    el("div", { class: "card-title" }, "Authors by churn"),
    el("span", { class: "card-sub" }, "λH,r,a — click to filter"),
    el("span", { class: "spacer" }),
    el("a", { class: "btn btn-sm btn-ghost", href: "#/" }, "manage identities →")));
  const maxChurn = Math.max(1, ...data.authors.map(a => a.churn));
  const list = el("div", { class: "barlist" });
  for (const author of data.authors) {
    list.append(barRow({
      label: author.label,
      value: author.churn,
      max: maxChurn,
      meta: `${fmtCompact(author.churn)} · ${fmtPct(author.share)}`,
      onclick: () => ctx.setFilters({ authors: [author.id] }),
    }));
  }
  if (!data.authors.length) list.append(el("div", { class: "dim" }, "no authors in this commit set"));
  authorCard.append(list);
  authorCard.append(el("div", { class: "hint", style: "margin-top:8px" },
    "ω (ownership share) is relative to the repository churn λH,r of the current commit set."));

  main.append(el("div", { class: "two-col", style: "margin-bottom:14px" }, treeCard, authorCard));

  function openNode(node) {
    navigate("browse", { rid: repo.id, path: node.path, params: ctx.filtersQuery() });
  }
  function paint() {
    for (const [index, button] of [...calendarToggle.children].entries()) {
      button.classList.toggle("on", ["commits", "churn"][index] === calendarMetric);
    }
    calendarHost.innerHTML = "";
    calendarChart(calendarHost, data.calendar, { metric: calendarMetric });
  }
  paint();
}

/* ------------------------------------------------------- recent commits */
function appendBottom(main, data, ctx) {
  const repo = data.repo;
  const card = el("div", { class: "card pad-0" });
  card.append(el("div", { class: "card-head", style: "padding:14px 16px 0" },
    el("div", { class: "card-title" }, "Latest commits in H"),
    el("span", { class: "spacer" }),
    el("button", { class: "btn btn-sm", onclick: () => navigate("commits", { rid: repo.id, params: ctx.filtersQuery() }) },
      "open commit browser →")));
  const table = el("table", { class: "data", style: "margin-top:10px" });
  table.append(el("thead", {}, el("tr", {},
    el("th", {}, "commit"), el("th", {}, "date"), el("th", {}, "author"),
    el("th", {}, "subject"), el("th", { class: "num" }, "files"),
    el("th", { class: "num" }, "l+"), el("th", { class: "num" }, "l−"),
    el("th", { class: "num" }, "δ"), el("th", { class: "num" }, "λ"))));
  const tbody = el("tbody", {});
  for (const commit of data.recent) {
    const row = el("tr", { class: "row-link", onclick: () => openCommit(repo.id, commit.hash) },
      el("td", { class: "mono" }, commit.short),
      el("td", { class: "nowrap dim" }, fmtDate(commit.ct)),
      el("td", {}, commit.author.label, commit.author.merged ? badge("merged", "violet") : null),
      el("td", {}, el("span", { class: "path" }, commit.subject)),
      el("td", { class: "num" }, fmtInt(commit.files)),
      el("td", { class: "num pos" }, fmtInt(commit.added)),
      el("td", { class: "num neg" }, fmtInt(commit.removed)),
      el("td", { class: "num" }, fmtSigned(commit.growth)),
      el("td", { class: "num" }, fmtInt(commit.churn)));
    tbody.append(row);
  }
  table.append(tbody);
  card.append(table);
  main.append(card);
}
