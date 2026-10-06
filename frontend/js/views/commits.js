/* Commit browser: paged, searchable list of H with per-commit metrics and
   a selection mode that turns a hand-picked list of commits into the
   commit set the whole dashboard measures (spec: H built manually). */

import { api } from "../api.js";
import { openCommit } from "../commitModal.js";
import { badge, filterBar, pager, summaryHtml, toast, toastError } from "../components.js";
import { debounce, el, fmtDate, fmtDateTime, fmtInt, fmtSigned } from "../util.js";

export async function renderCommits(main, ctx) {
  const { repo, filters, identities } = ctx.state;
  let page = 1, size = 50, query = "";
  let selecting = filters.mode === "list";
  const picked = new Set(filters.mode === "list" ? filters.commits : []);

  main.append(el("div", { class: "loading" }, el("span", { class: "spinner" }), " loading commits…"));

  const searchInput = el("input", { type: "search", placeholder: "search subject or hash…", style: "width:230px" });
  const sizeSelect = el("select", {},
    ...[25, 50, 100, 200].map(n => el("option", { value: n, selected: n === size ? "" : null }, `${n} / page`)));
  const selectToggle = el("button", {
    class: "btn btn-sm" + (selecting ? " active" : ""),
    title: "pick commits by hand and use them as the commit set H",
    onclick: () => { selecting = !selecting; render(); },
  }, selecting ? "✕ cancel selection" : "☑ select commits");

  const head = el("div", { class: "card-head", style: "padding:14px 16px 0;flex-wrap:wrap" },
    el("div", { class: "card-title" }, "Commits"),
    el("span", { class: "card-sub" }, ""),
    el("span", { class: "spacer" }),
    searchInput, sizeSelect, selectToggle);

  const applyBar = el("div", { class: "alert info", style: "display:none;align-items:center;gap:10px;margin:10px 16px 0" });
  const tableWrap = el("div", { style: "overflow-x:auto" });
  const pagerHost = el("div", { style: "padding:0 16px 14px" });
  const card = el("div", { class: "card pad-0" });
  card.append(head, applyBar, tableWrap, pagerHost);

  main.append(filterBar({
    repo, refs: ctx.state.refs, filters, identities,
    commitSet: ctx.state.commitSet, onChange: ctx.setFilters,
    extra: [el("a", { class: "btn btn-sm", href: api.exportUrl(repo.id, "commits", filters) }, "Export CSV")],
  }));
  main.append(card);

  sizeSelect.addEventListener("change", () => { size = Number(sizeSelect.value); load(); });
  searchInput.addEventListener("input", debounce(() => { query = searchInput.value.trim(); page = 1; load(); }, 250));

  render();

  function render() {
    selectToggle.textContent = selecting ? "✕ cancel selection" : "☑ select commits";
    selectToggle.classList.toggle("active", selecting);
    applyBar.style.display = selecting ? "flex" : "none";
    if (selecting) {
      applyBar.innerHTML = "";
      applyBar.append(
        el("b", {}, `${fmtInt(picked.size)} commits selected`),
        el("span", { class: "dim" }, "the whole dashboard will measure exactly these commits"),
        el("span", { class: "grow", style: "flex:1" }),
        el("button", { class: "btn btn-sm btn-primary", disabled: picked.size === 0,
          onclick: () => {
            ctx.setFilters({ mode: "list", commits: [...picked].sort() });
            toast(`Commit set H now holds ${picked.size} selected commits`, { kind: "success" });
          } }, "Use as commit set"),
        el("button", { class: "btn btn-sm", onclick: () => { picked.clear(); render(); refreshTable(); } }, "clear"));
    }
  }

  async function load() {
    try {
      const data = await api.commits(repo.id, filters, { page, size, q: query });
      if (ctx.token !== ctx.state.token) return;
      ctx.state.commitSet = data.commit_set;
      const summary = document.querySelector(".filter-summary");
      if (summary) summary.innerHTML = summaryHtml(data.commit_set, filters);
      const sub = card.querySelector(".card-sub");
      if (sub) sub.textContent = `${fmtInt(data.total)} commits in H · ${data.commit_set.label}`;
      paintTable(data);
      pagerHost.innerHTML = "";
      pagerHost.append(pager({
        page: data.page, pages: data.pages, total: data.total, size: data.size,
        onPage: (next) => { page = next; load(); main.scrollIntoView({ behavior: "smooth", block: "start" }); },
      }));
    } catch (error) {
      toastError(error.message);
    }
  }

  function paintTable(data) {
    const table = el("table", { class: "data" });
    const headerCells = [];
    if (selecting) {
      const master = el("input", { type: "checkbox", title: "select all on this page" });
      master.addEventListener("change", () => {
        for (const item of data.items) {
          if (master.checked) picked.add(item.hash); else picked.delete(item.hash);
        }
        render(); paintTable(data);
      });
      headerCells.push(el("th", { style: "width:34px" }, master));
    }
    for (const [label, cls] of [["commit", "mono"], ["date", ""], ["author", ""], ["subject", ""],
                                ["files", "num"], ["l+", "num"], ["l−", "num"], ["δ", "num"], ["λ", "num"]]) {
      headerCells.push(el("th", { class: cls }, label));
    }
    table.append(el("thead", {}, el("tr", {}, ...headerCells)));

    const tbody = el("tbody");
    if (!data.items.length) {
      tbody.append(el("tr", {}, el("td", { colspan: headerCells.length },
        el("div", { class: "empty" }, "no commits match this filter"))));
    }
    for (const commit of data.items) {
      const row = el("tr", { class: "row-link" + (picked.has(commit.hash) ? " selected" : "") });
      if (selecting) {
        const checkbox = el("input", { type: "checkbox" });
        checkbox.checked = picked.has(commit.hash);
        checkbox.addEventListener("click", (event) => event.stopPropagation());
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) picked.add(commit.hash); else picked.delete(commit.hash);
          row.classList.toggle("selected", checkbox.checked);
          render();
        });
        row.append(el("td", {}, checkbox));
        row.addEventListener("click", () => {
          if (picked.has(commit.hash)) picked.delete(commit.hash); else picked.add(commit.hash);
          render(); paintTable(data);
        });
      } else {
        row.addEventListener("click", () => openCommit(repo.id, commit.hash));
      }
      row.append(
        el("td", { class: "mono", title: commit.hash }, commit.short),
        el("td", { class: "nowrap dim", title: fmtDateTime(commit.ct) }, fmtDate(commit.ct)),
        el("td", { class: "nowrap" }, commit.author.label, commit.author.merged ? badge("merged", "violet") : null),
        el("td", {}, el("span", { class: "path" }, commit.subject || "(no subject)"),
          !commit.diffed ? badge("not measured", "warn") : null),
        el("td", { class: "num" }, fmtInt(commit.files)),
        el("td", { class: "num pos" }, fmtInt(commit.added)),
        el("td", { class: "num neg" }, fmtInt(commit.removed)),
        el("td", { class: "num" }, fmtSigned(commit.growth)),
        el("td", { class: "num" }, fmtInt(commit.churn)));
      tbody.append(row);
    }
    table.append(tbody);
    tableWrap.innerHTML = "";
    tableWrap.append(table);
  }

  function refreshTable() { load(); }
  await load();
}
