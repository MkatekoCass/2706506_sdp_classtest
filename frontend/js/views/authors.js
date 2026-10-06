/* Author metrics (spec 2.5) + identity management: git's .mailmap is applied
   at extraction time; on top of that the user can merge identities by hand
   (e.g. a contributor with a broken laptop having three email addresses). */

import { api } from "../api.js";
import { barRow, badge, filterBar, modal, toast, toastError } from "../components.js";
import { el, fmtDate, fmtInt, fmtPct, fmtSigned, navigate } from "../util.js";

export async function renderAuthors(main, ctx) {
  const { repo, filters, identities } = ctx.state;
  main.append(el("div", { class: "loading" }, el("span", { class: "spinner" }), " aggregating authors…"));

  let data;
  try {
    data = await api.authors(repo.id, filters);
  } catch (error) {
    main.innerHTML = "";
    main.append(el("div", { class: "alert bad" }, error.message));
    return;
  }
  if (ctx.token !== ctx.state.token) return;

  main.innerHTML = "";
  const commitSet = data.commit_set;
  ctx.state.commitSet = commitSet;

  main.append(filterBar({
    repo, refs: ctx.state.refs, filters, identities, commitSet, onChange: ctx.setFilters,
    extra: [
      el("button", { class: "btn btn-sm", onclick: () => openMergeDialog(ctx) }, "⇥ merge identities"),
      el("a", { class: "btn btn-sm", href: api.exportUrl(repo.id, "authors", filters) }, "Export CSV"),
    ],
  }));

  const mailmap = identities?.mailmap;
  if (mailmap?.present) {
    main.append(el("div", { class: "alert info" },
      `.mailmap detected (${fmtInt(mailmap.entries)} entries): git already resolved those identities at extraction time. `,
      el("a", { href: "#", onclick: (event) => { event.preventDefault(); showMailmap(mailmap); } }, "view .mailmap")));
  }

  /* ------------------------------------------------------------ table */
  let sortKey = "churn", sortDir = -1;
  const columns = [
    ["label", "author", ""],
    ["commits", "nH,o,a", "num"],
    ["added", "l+", "num"],
    ["removed", "l−", "num"],
    ["growth", "δ", "num"],
    ["churn", "λH,o,a", "num"],
    ["share", "ω", ""],
    ["files", "files", "num"],
    ["dirs", "dirs", "num"],
    ["first", "first", ""],
    ["last", "last", ""],
  ];
  const card = el("div", { class: "card pad-0" });
  card.append(el("div", { class: "card-head", style: "padding:14px 16px 0" },
    el("div", { class: "card-title" }, "Authors in H"),
    el("span", { class: "card-sub" },
      `${fmtInt(data.authors.length)} authors · click a row to filter the whole dashboard by that author`),
    el("span", { class: "spacer" }),
    el("span", { class: "hint" }, "λH,o,a and ω are relative to the repository churn in this commit set")));
  const table = el("table", { class: "data", style: "margin-top:10px" });
  const thead = el("thead"), tbody = el("tbody");
  table.append(thead, tbody);
  card.append(table);
  main.append(card);

  const maxChurn = Math.max(1, ...data.authors.map(a => a.churn));

  function paint() {
    thead.innerHTML = "";
    const row = el("tr");
    for (const [key, label, cls] of columns) {
      const th = el("th", { class: cls + " sortable" }, label,
        sortKey === key ? el("span", { class: "arrow" }, sortDir > 0 ? "▲" : "▼") : null);
      th.addEventListener("click", () => {
        if (sortKey === key) sortDir = -sortDir; else { sortKey = key; sortDir = key === "label" ? 1 : -1; }
        paint();
      });
      row.append(th);
    }
    thead.append(row);
    tbody.innerHTML = "";
    const rows = [...data.authors].sort((a, b) => {
      const va = a[sortKey], vb = b[sortKey];
      if (typeof va === "string" || typeof vb === "string") {
        return sortDir * String(va ?? "").localeCompare(String(vb ?? ""));
      }
      return sortDir * ((vb ?? 0) - (va ?? 0));
    });
    for (const author of rows) {
      const active = (filters.authors || []).includes(author.id);
      const tr = el("tr", { class: "row-link" + (active ? " selected" : "") ,
        onclick: () => ctx.setFilters({ authors: active ? (filters.authors || []).filter(a => a !== author.id) : [...(filters.authors || []), author.id] }) });
      tr.append(
        el("td", {},
          el("b", {}, author.label),
          author.merged ? badge("merged", "violet") : null,
          el("div", { class: "dim", style: "font-size:11px" },
            author.emails.slice(0, 2).join(", ") + (author.emails.length > 2 ? ` +${author.emails.length - 2}` : ""))),
        el("td", { class: "num" }, fmtInt(author.commits)),
        el("td", { class: "num pos" }, fmtInt(author.added)),
        el("td", { class: "num neg" }, fmtInt(author.removed)),
        el("td", { class: "num" }, fmtSigned(author.growth)),
        el("td", { class: "num" }, fmtInt(author.churn)),
        el("td", { style: "min-width:140px" },
          el("div", { class: "bar-track", style: "height:14px" },
            el("div", { class: "bar-fill", style: `width:${Math.max(2, author.churn / maxChurn * 100)}%;background:#5b8cff` })),
          el("span", { class: "dim", style: "font-size:11px" }, fmtPct(author.share))),
        el("td", { class: "num" }, fmtInt(author.files)),
        el("td", { class: "num" }, fmtInt(author.dirs)),
        el("td", { class: "nowrap dim" }, fmtDate(author.first)),
        el("td", { class: "nowrap dim" }, fmtDate(author.last)));
      tbody.append(tr);
    }
    if (!rows.length) {
      tbody.append(el("tr", {}, el("td", { colspan: columns.length },
        el("div", { class: "empty" }, "no authors changed anything in this commit set"))));
    }
  }
  paint();

  appendIdentityPanel(main, ctx);
}

/* ------------------------------------------------------ identity panel */
function appendIdentityPanel(main, ctx) {
  const identities = ctx.state.identities;
  if (!identities) return;
  const card = el("div", { class: "card", style: "margin-top:14px" });
  card.append(el("div", { class: "card-head" },
    el("div", { class: "card-title" }, "Identities & manual merging"),
    el("span", { class: "card-sub" },
      `${fmtInt(identities.groups.length)} authors from ${fmtInt(identities.raw.length)} raw identities`),
    el("span", { class: "spacer" }),
    el("button", { class: "btn btn-sm btn-primary", onclick: () => openMergeDialog(ctx) }, "+ merge identities"),
    identities.merges.length
      ? el("button", { class: "btn btn-sm btn-danger", onclick: () => clearMerges(ctx, identities) }, "remove all manual merges")
      : null));

  if (identities.merges.length) {
    const list = el("div", { class: "grid", style: "gap:8px" });
    for (const group of identities.merges) {
      list.append(el("div", { class: "chip", style: "padding:6px 10px" },
        el("b", {}, group.label),
        el("span", { class: "dim" }, group.members.map(m => `${m[0]} <${m[1]}>`).join("  ·  ")),
        el("button", {
          title: "remove this merge",
          onclick: () => {
            const rest = identities.merges.filter(g => g !== group);
            saveMerges(ctx, rest);
          },
        }, "✕")));
    }
    card.append(el("div", { style: "margin-bottom:10px" }, list));
  }

  const suggestions = identities.suggestions || [];
  const sugBox = el("div", {});
  sugBox.append(el("div", { class: "hint", style: "margin-bottom:6px" },
    suggestions.length
      ? "Possible same-person identities (same email or same name). Merging is opt-in — check before accepting:"
      : "No obvious duplicate identities found."));
  if (suggestions.length) {
    sugBox.append(el("button", {
      class: "btn btn-sm", onclick: () => {
        const merged = [...identities.merges];
        for (const suggestion of suggestions) {
          merged.push({
            label: suggestion.members[0].name,
            members: suggestion.members.map(m => [m.name, m.email]),
          });
        }
        saveMerges(ctx, merged);
      },
    }, `accept all ${suggestions.length} suggestions`));
    const list = el("div", { class: "grid", style: "gap:6px;margin-top:8px" });
    for (const suggestion of suggestions.slice(0, 12)) {
      list.append(el("div", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap" },
        el("button", {
          class: "btn btn-sm", onclick: () => {
            saveMerges(ctx, [...identities.merges, {
              label: suggestion.members[0].name,
              members: suggestion.members.map(m => [m.name, m.email]),
            }]);
          },
        }, "merge"),
        el("span", { class: "dim" }, suggestion.reason),
        el("span", { class: "mono", style: "font-size:11.5px" },
          suggestion.members.map(m => `${m.name} <${m.email}> (${m.commits})`).join("  ·  "))));
    }
    sugBox.append(list);
  }
  card.append(sugBox);
  main.append(card);
}

async function saveMerges(ctx, groups) {
  try {
    await api.setMerges(ctx.state.repo.id, groups);
    await ctx.reloadIdentities();
    toast("Author identities updated", { kind: "success" });
  } catch (error) {
    toastError(error.message);
  }
}

async function clearMerges(ctx, identities) {
  if (!identities.merges.length) return;
  await saveMerges(ctx, []);
}

/* ------------------------------------------------------ merge dialog */
function openMergeDialog(ctx) {
  const identities = ctx.state.identities;
  if (!identities) { toastError("identities are still loading"); return; }
  const selected = new Set();
  const labelInput = el("input", { type: "text", placeholder: "display name for the merged author", style: "width:100%" });
  const search = el("input", { type: "search", placeholder: "filter identities…", style: "width:100%", });
  const list = el("div", { style: "max-height:44vh;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:8px" });

  function paintList() {
    list.innerHTML = "";
    const query = search.value.toLowerCase();
    const rows = identities.raw.filter(identity =>
      !query || identity.name.toLowerCase().includes(query) || identity.email.toLowerCase().includes(query));
    rows.sort((a, b) => b.commits - a.commits);
    for (const identity of rows.slice(0, 400)) {
      const key = `${identity.name}\u0000${identity.email}`;
      const checkbox = el("input", { type: "checkbox" });
      checkbox.checked = selected.has(key);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          selected.add(key);
          if (!labelInput.value) labelInput.value = identity.name;
        } else selected.delete(key);
        counter.textContent = `${selected.size} selected`;
      });
      list.append(el("label", { class: "ms-item" }, checkbox,
        el("span", { class: "ms-name" }, `${identity.name} <${identity.email}>`),
        el("span", { class: "ms-meta" }, `${fmtInt(identity.commits)} commits`)));
    }
    if (!rows.length) list.append(el("div", { class: "dim", style: "padding:10px" }, "no identities match"));
  }
  const counter = el("span", { class: "dim" }, "0 selected");
  search.addEventListener("input", paintList);
  paintList();

  const handle = modal({
    title: "Merge author identities",
    body: el("div", {},
      el("p", { class: "hint" },
        "Pick two or more identities that belong to the same person. The merge is applied on top of git's .mailmap and stored with the repository — every metric is recomputed for the merged author."),
      el("label", { class: "field" }, el("span", {}, "merged author name"), labelInput),
      search, list,
      el("div", { style: "margin-top:8px;display:flex;align-items:center;gap:8px" }, counter)),
    footer: [
      el("button", { class: "btn", onclick: () => handle.close() }, "Cancel"),
      el("button", {
        class: "btn btn-primary", onclick: async () => {
          const members = [...selected].map(key => key.split("\u0000"));
          if (members.length < 2) { toast("Select at least two identities", { kind: "warn" }); return; }
          const label = labelInput.value.trim() || members[0][0];
          handle.close();
          await saveMerges(ctx, [...identities.merges, { label, members }]);
        },
      }, "Merge"),
    ],
  });
}

function showMailmap(mailmap) {
  modal({
    title: ".mailmap (resolved by git during extraction)",
    body: el("pre", { class: "mono", style: "white-space:pre-wrap;margin:0" }, mailmap.text || "(empty)"),
  });
}

void barRow; void navigate;
