/* Commit inspector modal, shared by the overview and commits views. */

import { api } from "./api.js";
import { badge, modal, toastError } from "./components.js";
import { copy, el, fmtDate, fmtDateTime, fmtInt, fmtSigned } from "./util.js";

export async function openCommit(rid, hash) {
  let detail;
  try {
    detail = (await api.commit(rid, hash)).commit;
  } catch (error) {
    toastError(error.message);
    return;
  }

  const body = el("div", {});
  if (detail.note) body.append(el("div", { class: "alert" }, detail.note));

  const meta = el("div", { class: "kv", style: "margin-bottom:12px" },
    el("dt", {}, "commit"), el("dd", {},
      el("span", { class: "commit-hash" }, detail.hash),
      el("button", { class: "btn btn-sm btn-ghost", style: "margin-left:6px", title: "copy full hash",
                     onclick: () => copy(detail.hash) }, "copy")),
    el("dt", {}, "author"), el("dd", {}, `${detail.author.label} <${detail.author.raw_email}>`),
    el("dt", {}, "authored"), el("dd", {}, fmtDateTime(detail.at)),
    el("dt", {}, "committed"), el("dd", {}, fmtDateTime(detail.ct)));
  if (detail.author.merged) {
    meta.append(el("dt", {}, "merging"), el("dd", {},
      el("span", { class: "dim" }, `${detail.author.raw_name} <${detail.author.raw_email}> merged into this author`)));
  }
  body.append(meta);

  body.append(el("div", { class: "grid cols-6", style: "margin-bottom:14px" },
    cell("files", fmtInt(detail.files)),
    cell("l+", fmtInt(detail.added), "pos"),
    cell("l−", fmtInt(detail.removed), "neg"),
    cell("δ", fmtSigned(detail.growth)),
    cell("λ", fmtInt(detail.churn)),
    cell("binary rows", fmtInt(detail.binary))));

  if (detail.changes?.length) {
    const table = el("table", { class: "data" });
    table.append(el("thead", {}, el("tr", {},
      el("th", {}, "path"), el("th", { class: "num" }, "l+"), el("th", { class: "num" }, "l−"),
      el("th", { class: "num" }, "δ"), el("th", { class: "num" }, "λ"), el("th", {}, ""))));
    const tbody = el("tbody", {});
    for (const change of detail.changes) {
      tbody.append(el("tr", {},
        el("td", {}, el("span", { class: "mono path", title: change.path }, change.path)),
        el("td", { class: "num pos" }, fmtInt(change.added)),
        el("td", { class: "num neg" }, fmtInt(change.removed)),
        el("td", { class: "num" }, fmtSigned(change.growth)),
        el("td", { class: "num" }, fmtInt(change.churn)),
        el("td", { class: "nowrap" },
          change.rename ? badge(`← ${change.old_path}`, "accent") : null,
          change.binary ? badge("binary", "warn") : null,
          change.submodule ? badge("submodule", "warn") : null)));
    }
    table.append(tbody);
    body.append(table);
  } else {
    body.append(el("div", { class: "empty" }, "this commit changed no measured files"));
  }

  modal({ title: detail.subject || "(no subject)", body, wide: true });

  function cell(label, value, kind = "") {
    return el("div", { class: "metric " + kind },
      el("div", { class: "label" }, label), el("div", { class: "value" }, value));
  }
}

void fmtDate;
