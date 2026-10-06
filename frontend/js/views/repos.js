/* Repository list + "add repository" page (clone / zip / local path). */

import { api } from "../api.js";
import { badge, confirmDialog, promptDialog, repoJobLine, statusPill, toast, toastError } from "../components.js";
import { el, fmtCompact, fmtDate, fmtDuration, fmtInt, navigate, timeAgo } from "../util.js";

const KIND_LABEL = { clone: "remote clone", zip: "zip upload", local: "local path" };

export function renderRepos(main, ctx) {
  const repos = ctx.state.repos || [];
  main.append(el("div", { class: "page-head" },
    el("div", {},
      el("h1", {}, "Repositories"),
      el("div", { class: "sub" }, `${repos.length} analysed ${repos.length === 1 ? "repository" : "repositories"} · click a card to open its dashboard`)),
    el("div", { class: "spacer" }),
    el("a", { class: "btn btn-primary", href: "#/add" }, "+ Add repository")));

  if (!repos.length) {
    main.append(el("div", { class: "empty" },
      el("h3", {}, "No repositories yet"),
      el("p", {}, "Ingest a repository from a remote URL (full clone), a .zip archive containing its .git directory, or a local path."),
      el("a", { class: "btn btn-primary", href: "#/add" }, "+ Add repository")));
    return;
  }

  const grid = el("div", { class: "repo-cards" });
  for (const repo of repos) grid.append(repoCard(repo, ctx));
  main.append(grid);
}

function repoCard(repo, ctx) {
  const stats = repo.stats || {};
  const card = el("div", { class: "card repo-card" });

  card.append(el("div", { class: "rc-top" },
    el("a", { class: "rc-name", href: `#/r/${encodeURIComponent(repo.id)}/overview` }, repo.name),
    statusPill(repo),
    badge(KIND_LABEL[repo.kind] || repo.kind)));

  card.append(el("div", { class: "rc-src", title: repo.source }, repo.source));

  card.append(el("div", { class: "rc-stats" },
    stat("commits", stats.non_merge_commits ?? "–"),
    stat("files", stats.files ?? "–"),
    stat("dirs", stats.dirs ?? "–"),
    stat("λ churn", (stats.added ?? 0) + (stats.removed ?? 0))));

  const job = repoJobLine(repo);
  if (job) card.append(job);
  if (repo.status === "error" && repo.error) {
    card.append(el("div", { class: "alert bad", style: "margin:0" }, repo.error));
  }
  if (repo.warnings?.length && repo.status === "ready") {
    card.append(el("div", { class: "hint" }, repo.warnings[0]));
  }

  const footer = el("div", { class: "rc-actions" });
  const opened = repo.analyzed_at
    ? `analysed ${timeAgo(repo.analyzed_at)}${stats.duration ? ` · ${fmtDuration(stats.duration)}` : ""}`
    : "";
  footer.append(el("span", { class: "hint" },
    opened || (repo.status === "ready" ? "ready" : repo.status)));
  footer.append(el("span", { class: "spacer" }));

  if (repo.status === "ready") {
    footer.append(el("a", { class: "btn btn-sm btn-primary", href: `#/r/${encodeURIComponent(repo.id)}/overview` }, "Open"));
  }
  footer.append(el("button", { class: "btn btn-sm", title: "re-run the analysis (optionally from another reference)", onclick: () => reanalyse(repo, ctx) }, "Re-analyse"));
  footer.append(el("button", { class: "btn btn-sm btn-danger", onclick: () => remove(repo, ctx) }, "Delete"));
  card.append(footer);
  return card;

  function stat(label, value) {
    return el("div", { class: "metric" },
      el("div", { class: "label" }, label),
      el("div", { class: "value" }, typeof value === "number" ? fmtCompact(value) : value));
  }
}

async function reanalyse(repo, ctx) {
  const ref = await promptDialog({
    title: `Re-analyse “${repo.name}”`,
    label: "reference to analyse (contents of this ref define H̄)",
    value: repo.analysis_ref || "HEAD",
    placeholder: "HEAD, main, v1.0, …",
    okLabel: "Re-analyse",
  });
  if (ref === null) return;
  try {
    await api.reanalyze(repo.id, ref.trim() || "HEAD");
    toast(`Re-analysing from ${ref.trim() || "HEAD"} — progress appears on the card`, { kind: "success" });
    ctx.refreshRepos();
  } catch (error) {
    toastError(error.message);
  }
}

async function remove(repo, ctx) {
  const ok = await confirmDialog({
    title: `Delete “${repo.name}”?`,
    message: "The ingested repository and its metric index are removed from disk. This cannot be undone.",
    okLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.deleteRepo(repo.id);
    toast(`Deleted ${repo.name}`, { kind: "success" });
    if (ctx.state.repo?.id === repo.id) navigate("repos");
    ctx.refreshRepos();
  } catch (error) {
    toastError(error.message);
  }
}

/* ------------------------------------------------------------------ add */

export function renderAdd(main, ctx) {
  let tab = "clone";
  const nameInput = el("input", { type: "text", placeholder: "optional display name" });
  const refInput = el("input", { type: "text", value: "HEAD", placeholder: "HEAD" });
  const urlInput = el("input", { type: "text", placeholder: "https://github.com/owner/project.git", style: "width:100%" });
  const pathInput = el("input", { type: "text", placeholder: "/home/you/projects/project  (or file:///…)", style: "width:100%" });
  let zipFile = null;

  main.append(el("div", { class: "page-head" },
    el("div", {},
      el("h1", {}, "Add repository"),
      el("div", { class: "sub" }, "Ingestion runs in the background — you can keep using the dashboard while it clones and analyses.")),
    el("div", { class: "spacer" }),
    el("a", { class: "btn", href: "#/" }, "← All repositories")));

  const pane = el("div", {});
  const seg = el("div", { class: "seg", style: "margin-bottom:14px" });
  const tabs = [["clone", "Remote URL"], ["zip", "Zip upload"], ["local", "Local path"]];
  for (const [key, label] of tabs) {
    seg.append(el("button", { class: key === tab ? "on" : "", onclick: () => { tab = key; paint(); } }, label));
  }

  const body = el("div", { class: "card" });
  const submit = el("button", { class: "btn btn-primary", onclick: () => send() }, "Ingest repository");

  const wrap = el("div", { style: "max-width:760px" }, seg, body,
    el("div", { class: "form-row", style: "margin-top:14px" },
      el("label", { class: "field" }, el("span", {}, "name"), nameInput),
      el("label", { class: "field" }, el("span", {}, "analyse ref"), refInput),
      el("span", { class: "grow" }),
      submit));
  main.append(wrap);

  function paint() {
    for (const [index, button] of [...seg.children].entries()) {
      button.classList.toggle("on", tabs[index][0] === tab);
    }
    body.innerHTML = "";
    if (tab === "clone") {
      body.append(el("label", { class: "field" }, el("span", {}, "git URL"), urlInput),
        el("p", { class: "hint" },
          "Cloned bare and in full (no --depth): the whole history is fetched so every metric sees every commit. " +
          "https, ssh and scp-like URLs are all supported."));
    } else if (tab === "zip") {
      const zone = el("div", { class: "dropzone" },
        el("div", { class: "big" }, "⇪"),
        el("div", {}, zipFile ? zipFile.name : "Drop a .zip here, or click to choose"),
        el("div", { class: "hint", style: "margin-top:6px" },
          "The archive must contain the repository's .git directory (zip the parent folder) " +
          "or a bare repository (HEAD/objects/refs at the top level)."));
      const file = el("input", { type: "file", accept: ".zip", style: "display:none" });
      zone.addEventListener("click", () => file.click());
      file.addEventListener("change", () => { zipFile = file.files[0]; paint(); });
      zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("over"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("over"));
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("over");
        const dropped = [...event.dataTransfer.files].filter(f => f.name.toLowerCase().endsWith(".zip"));
        if (dropped.length) { zipFile = dropped[0]; paint(); }
        else toast("Please drop a .zip archive", { kind: "warn" });
      });
      body.append(zone, file);
    } else {
      body.append(el("label", { class: "field" }, el("span", {}, "path on this machine"), pathInput),
        el("p", { class: "hint" },
          "The path may point at a working tree (its .git is used) or at a bare repository. " +
          "Nothing is copied when the filesystem supports symlinks."));
    }
  }

  async function send() {
    submit.disabled = true;
    try {
      let repo;
      const ref = refInput.value.trim() || "HEAD";
      const name = nameInput.value.trim();
      if (tab === "zip") {
        if (!zipFile) { toast("Choose a .zip file first", { kind: "warn" }); return; }
        const form = new FormData();
        form.append("file", zipFile);
        if (name) form.append("name", name);
        form.append("ref", ref);
        repo = await api.uploadZip(form);
      } else {
        const source = (tab === "clone" ? urlInput.value : pathInput.value).trim();
        if (!source) { toast("Enter a URL or path first", { kind: "warn" }); return; }
        repo = await api.createRepo({ source, name: name || undefined, ref, kind: tab });
      }
      toast(`Ingestion of ${repo.name} started`, { kind: "success" });
      ctx.refreshRepos();
      location.hash = `#/r/${encodeURIComponent(repo.id)}/overview`;
    } catch (error) {
      toastError(error.message);
    } finally {
      submit.disabled = false;
    }
  }

  paint();
}

/* --------------------------------------------- "repo not ready yet" view */
export function renderPreparing(main, ctx) {
  const repo = ctx.state.repo;
  main.append(el("div", { class: "page-head" }, el("h1", {}, repo?.name || "Repository")));
  const job = repo?.job || {};
  const phases = { ingest: "fetching the repository", analyze: "starting analysis",
                   diff: "reading line statistics", graph: "reading commit graph",
                   normalise: "building file/directory tables", write: "writing metric index" };
  const card = el("div", { class: "card", style: "max-width:640px" });
  card.append(el("div", { class: "card-head" }, el("div", { class: "card-title" }, "Preparing")),
    el("p", {}, `${phases[job.phase] || job.phase || "working"}…`),
    el("div", { style: "height:6px" }),
    job.progress !== undefined ? null : el("div", { class: "dim" }, job.message || ""));
  const progress = el("div", { class: "progress" },
    el("div", { style: `width:${Math.round((job.progress || 0) * 100)}%` }));
  card.append(progress, el("div", { class: "hint", style: "margin-top:8px" }, job.message || ""));
  if (repo?.error) card.append(el("div", { class: "alert bad", style: "margin-top:12px" }, repo.error));
  main.append(card);
}

void fmtInt; void fmtDate; void fmtCompact;
