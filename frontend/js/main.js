/* App bootstrap: hash router, repository context (repo/refs/identities), the
   topbar chrome (repo switcher, tabs, global search) and job polling.

   Every route render gets a *fresh* ctx object carrying its own token.  A
   render that is superseded while awaiting the network compares its token to
   state.token and bails out before touching the DOM (views rely on this via
   the `ctx.token !== ctx.state.token` guards). */

import { api } from "./api.js";
import { openCommit } from "./commitModal.js";
import { emptyState, filtersFromQuery, filtersToQuery, statusPill, summaryHtml, toast, toastError } from "./components.js";
import { renderAdd, renderPreparing, renderRepos } from "./views/repos.js";
import { renderOverview } from "./views/overview.js";
import { renderBrowser } from "./views/browser.js";
import { renderCommits } from "./views/commits.js";
import { renderAuthors } from "./views/authors.js";
import { $, buildHash, debounce, el, parseHash } from "./util.js";

const VIEWS = { overview: renderOverview, browse: renderBrowser, commits: renderCommits, authors: renderAuthors };
const WORKING = new Set(["queued", "ingesting", "analyzing"]);   // non-terminal job states
const POLL_MS = 1500;

const state = {
  repos: [],              // repository list as served by GET /repos
  repo: null,             // repository rendered by the current route
  refs: [],               // refs of the current repository (cache)
  refsFor: null,
  identities: null,       // groups / raw / merges / suggestions / mailmap (cache)
  identitiesFor: null,
  filters: filtersFromQuery(new URLSearchParams()),
  commitSet: null,        // last commit-set summary, shown in the footer
  treeMetric: "churn",
  calendarMetric: "commits",
  path: "",               // browse path of the current route
  token: 0,               // bumped on every render; stale renders see a mismatch
};

let fromUrl = true;       // the next render should read filters from the hash
let pollTimer = null;
let pickerMenu = null;    // open repo-switcher menu (closed on any outside click)
let hideSearch = () => {};

/* ================================================================ context */

function makeCtx(token) {
  return {
    state,
    token,
    /** Merge a filter patch, keep the URL in sync and re-render in place. */
    setFilters(patch) {
      Object.assign(state.filters, patch || {});
      syncUrl();
      fromUrl = false;
      renderRoute();
    },
    filtersQuery: () => filtersToQuery(state.filters),
    refreshRepos: () => loadRepos(),
    reloadIdentities: async () => {
      await loadContext(state.repo?.id, { force: true });
      fromUrl = false;
      renderRoute();
    },
  };
}

function upsertRepo(repo) {
  if (!repo?.id) return;
  const index = state.repos.findIndex(item => item.id === repo.id);
  if (index >= 0) state.repos[index] = repo; else state.repos.push(repo);
}

async function loadRepos() {
  try {
    const data = await api.repos();
    state.repos = data.repos || [];
    paintRepoPicker(currentRid());
    ensurePolling();
  } catch (error) {
    toastError(error.message, "Cannot reach the server");
  }
  return state.repos;
}

/** refs + identities for a repository, cached per rid (force to re-fetch). */
async function loadContext(rid, { force = false } = {}) {
  if (!rid) return false;
  const wantRefs = force || state.refsFor !== rid;
  const wantIds = force || state.identitiesFor !== rid;
  if (!wantRefs && !wantIds) return true;
  try {
    const [refsRes, idsRes] = await Promise.all([
      wantRefs ? api.refs(rid) : Promise.resolve(null),
      wantIds ? api.identities(rid) : Promise.resolve(null),
    ]);
    if (refsRes) {
      state.refs = refsRes.refs || [];
      state.refsFor = rid;
      if (refsRes.repo) {
        upsertRepo(refsRes.repo);
        if (state.repo?.id === rid) state.repo = refsRes.repo;
      }
    }
    if (idsRes) {
      state.identities = idsRes;
      state.identitiesFor = rid;
    }
    return true;
  } catch (error) {
    toastError(error.message, "Cannot load the repository");
    return false;
  }
}

/* ================================================================ routing */

async function renderRoute() {
  const route = parseHash();
  const main = $("#view");
  const token = ++state.token;
  const ctx = makeCtx(token);

  if (fromUrl) state.filters = filtersFromQuery(route.query);
  fromUrl = true;

  hideSearch();
  main.innerHTML = "";
  window.scrollTo(0, 0);
  paintTabs(route);
  $("#global-search").disabled = !route.rid;

  try {
    if (route.view === "add") {
      paintRepoPicker(null);
      renderAdd(main, ctx);
      paintFooter();
      return;
    }

    if (!route.rid) {
      paintRepoPicker(null);
      await loadRepos();
      if (token !== state.token) return;
      renderRepos(main, ctx);
      paintFooter();
      return;
    }

    /* ---------------------------------------------- repository-scoped view */
    const rid = route.rid;
    const path = route.path || "";
    if (state.repo?.id !== rid) { state.repo = null; state.commitSet = null; }
    state.path = path;

    let repo = state.repos.find(item => item.id === rid) || null;
    if (!repo && state.repo?.id === rid) repo = state.repo;
    if (!repo) {
      try {
        repo = await api.repo(rid);
        upsertRepo(repo);
      } catch (error) {
        if (token !== state.token) return;
        main.append(emptyState("Repository not found", error.message,
          el("a", { class: "btn btn-primary", href: "#/" }, "← All repositories")));
        paintFooter();
        return;
      }
    }
    if (token !== state.token) return;
    state.repo = repo;
    paintRepoPicker(rid);

    if (repo.status !== "ready") {
      renderPreparing(main, ctx);
      ensurePolling();
      paintFooter();
      return;
    }

    const view = VIEWS[route.view] || renderOverview;
    if (state.refsFor !== rid || state.identitiesFor !== rid) {
      main.append(el("div", { class: "loading" }, el("span", { class: "spinner" }), " loading repository context…"));
      const ok = await loadContext(rid);
      if (token !== state.token) return;
      if (!ok) { paintFooter(); return; }
      main.innerHTML = "";
      if (state.repo?.status !== "ready") {         // re-analysis started meanwhile
        renderPreparing(main, ctx);
        ensurePolling();
        paintFooter();
        return;
      }
    }

    await view(main, ctx);
    if (token !== state.token) return;
    paintFooter();
  } catch (error) {
    if (token !== state.token) return;
    main.innerHTML = "";
    main.append(el("div", { class: "alert bad" }, error.message));
    paintFooter();
  }
}

/** keep the hash in sync with the current filters (no hashchange event). */
function syncUrl() {
  const route = parseHash();
  if (!route.rid) return;
  const hash = buildHash(route.view, {
    rid: route.rid,
    path: route.view === "browse" ? route.path : undefined,
    params: filtersToQuery(state.filters),
  });
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

const currentRid = () => parseHash().rid || null;

/* ================================================================= chrome */

function paintTabs(route) {
  const tabs = $("#tabs");
  tabs.hidden = !route.rid;
  if (!route.rid) return;
  const params = filtersToQuery(state.filters);
  for (const link of tabs.children) {
    const view = link.dataset.view;
    link.href = buildHash(view, { rid: route.rid, path: view === "browse" ? state.path : undefined, params });
    link.classList.toggle("active", view === route.view);
  }
}

function paintRepoPicker(activeRid) {
  const host = $("#repo-switch");
  pickerMenu = null;
  host.innerHTML = "";
  host.hidden = !activeRid;
  if (!activeRid) return;
  const repo = state.repos.find(item => item.id === activeRid) || state.repo;

  const menu = el("div", { class: "repo-menu", hidden: true });
  for (const item of state.repos) {
    menu.append(el("a", {
      class: item.id === activeRid ? "active" : "",
      href: buildHash("overview", { rid: item.id, params: filtersToQuery(state.filters) }),
    }, el("span", { style: "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, item.name),
       statusPill(item)));
  }
  if (!state.repos.length) menu.append(el("div", { class: "dim", style: "padding:8px" }, "no repositories yet"));
  menu.append(el("div", { class: "sep" }));
  menu.append(el("a", { href: "#/" }, "⇄ All repositories"));
  menu.append(el("a", { href: "#/add" }, "+ Add repository"));

  const button = el("button", {
    class: "repo-picker-btn", title: repo?.source || "",
    onclick: (event) => {
      event.stopPropagation();
      const willOpen = menu.hidden;
      pickerMenu = willOpen ? menu : null;
      menu.hidden = !willOpen;
    },
  },
    el("span", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, repo?.name || activeRid),
    repo ? statusPill(repo) : null,
    el("span", { class: "caret" }, "▾"));

  host.append(el("div", { class: "repo-picker" }, button, menu));
}

function paintFooter() {
  const route = parseHash();
  const filtersHost = $("#footer-filters");
  const statusHost = $("#footer-status");
  if (!route.rid) {
    filtersHost.innerHTML = "";
    statusHost.textContent = `${state.repos.length} repositories`;
    return;
  }
  const repo = state.repos.find(item => item.id === route.rid) || state.repo;
  filtersHost.innerHTML = state.commitSet ? summaryHtml(state.commitSet, state.filters) : "";
  const job = repo?.job || {};
  let text = repo ? `${repo.name} · ${repo.status}` : "";
  if (job.phase && !job.finished && job.progress !== undefined) text += ` · ${Math.round(job.progress * 100)}%`;
  statusHost.textContent = text;
}

/* ================================================================ polling */

function ensurePolling() {
  if (pollTimer) return;
  if (!state.repos.some(repo => WORKING.has(repo.status))) return;
  pollTimer = setInterval(poll, POLL_MS);
}

function stopPollingIfIdle() {
  if (pollTimer && !state.repos.some(repo => WORKING.has(repo.status))) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function poll() {
  const route = parseHash();
  if (!route.rid) {
    if (route.view === "repos") await renderRoute();      // repaint progress on the cards
    else await loadRepos();                               // never touch the add form
    stopPollingIfIdle();
    return;
  }
  const before = state.repos.find(item => item.id === route.rid)?.status;
  await loadRepos();
  const now = state.repos.find(item => item.id === route.rid)?.status;
  if (now === "ready" && before !== "ready") {
    if (state.repo?.id === route.rid) {
      state.repo = state.repos.find(item => item.id === route.rid);
      toast(`“${state.repo?.name}” is ready`, { kind: "success" });
    }
    renderRoute();
  } else if (now && now !== "ready") {
    renderRoute();                                        // still preparing: repaint progress
  } else {
    paintFooter();
  }
  stopPollingIfIdle();
}

/* ======================================================== global search */

function setupSearch() {
  const wrap = $("#global-search-wrap");
  const input = $("#global-search");
  const results = $("#search-results");

  const hide = () => { results.hidden = true; results.innerHTML = ""; };
  hideSearch = hide;

  const run = debounce(async () => {
    const rid = currentRid();
    const query = input.value.trim();
    if (!rid || query.length < 2) { hide(); return; }
    let data;
    try { data = await api.search(rid, query); } catch { hide(); return; }
    if (input.value.trim() !== query) return;             // stale response
    paint(data, rid, hide);
  }, 220);

  input.addEventListener("input", run);
  input.addEventListener("focus", run);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { input.blur(); hide(); }
  });
  document.addEventListener("click", (event) => { if (!wrap.contains(event.target)) hide(); });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    const tag = document.activeElement?.tagName || "";
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || document.activeElement?.isContentEditable) return;
    if (input.disabled) return;
    event.preventDefault();
    input.focus();
    input.select();
  });

  function paint(data, rid, close) {
    results.innerHTML = "";
    const total = data.files.length + data.dirs.length + data.commits.length;
    if (!total) {
      results.append(el("div", { class: "group" }, "no matches"));
      results.hidden = false;
      return;
    }
    const go = (event, fn) => {
      event.preventDefault();
      close();
      fn();
    };
    const link = (label, sub, onclick) => el("a", { href: "#", onclick: (event) => go(event, onclick) },
      el("span", {}, label), el("span", { class: "s-sub mono" }, sub));

    if (data.commits.length) {
      results.append(el("div", { class: "group" }, `commits (${data.commits.length})`));
      for (const commit of data.commits.slice(0, 8)) {
        results.append(link(`⌥ ${commit.short}`, commit.subject || "(no subject)",
          () => openCommit(rid, commit.hash)));
      }
    }
    if (data.files.length) {
      results.append(el("div", { class: "group" }, `files (${data.files.length})`));
      for (const file of data.files.slice(0, 12)) {
        results.append(link("📄", file.path, () =>
          (location.hash = buildHash("browse", { rid, path: file.path, params: filtersToQuery(state.filters) }))));
      }
    }
    if (data.dirs.length) {
      results.append(el("div", { class: "group" }, `directories (${data.dirs.length})`));
      for (const dir of data.dirs.slice(0, 10)) {
        results.append(link("📁", dir.path, () =>
          (location.hash = buildHash("browse", { rid, path: dir.path, params: filtersToQuery(state.filters) }))));
      }
    }
    results.hidden = false;
  }
}

/* =================================================================== boot */

window.addEventListener("hashchange", () => {
  fromUrl = true;
  renderRoute();
});
document.addEventListener("click", () => { if (pickerMenu) { pickerMenu.hidden = true; pickerMenu = null; } });

setupSearch();
renderRoute();
