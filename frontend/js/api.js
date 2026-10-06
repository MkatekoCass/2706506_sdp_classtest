/* REST client. Every call returns parsed JSON or throws ApiError with the
   server's message, so views can simply try/catch and toast. */

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request(path, { method = "GET", params, body, form } = {}) {
  let url = "/api" + path;
  if (params) {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      qs.set(key, value);
    }
    const query = qs.toString();
    if (query) url += "?" + query;
  }
  const init = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  if (form) init.body = form;

  let response;
  try {
    response = await fetch(url, init);
  } catch (cause) {
    throw new ApiError("cannot reach the server", 0, null);
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!response.ok) {
    throw new ApiError(data?.error || `${response.status} ${response.statusText}`,
                      response.status, data);
  }
  return data;
}

/** Filters (view state) -> query parameters for the API. */
export function filterParams(filters) {
  const params = { mode: filters.mode || "all" };
  if (filters.ref) params.ref = filters.ref;
  if (filters.mode === "range") {
    const from = filters.from ? Math.floor(Date.parse(filters.from + "T00:00:00Z") / 1000) : null;
    const to = filters.to ? Math.floor(Date.parse(filters.to + "T00:00:00Z") / 1000) + 86400 : null;
    if (from) params.from = from;
    if (to) params.to = to;
  }
  if (filters.mode === "list" && filters.commits?.length) {
    params.commits = filters.commits.join(",");
  }
  if (filters.authors?.length) params.authors = filters.authors.join(",");
  return params;
}

export const api = {
  health: () => request("/health"),

  // repositories
  repos: () => request("/repos"),
  repo: (rid) => request(`/repos/${encodeURIComponent(rid)}`),
  createRepo: (body) => request("/repos", { method: "POST", body }),
  uploadZip: (form) => request("/repos/zip", { method: "POST", form }),
  deleteRepo: (rid) => request(`/repos/${encodeURIComponent(rid)}`, { method: "DELETE" }),
  reanalyze: (rid, ref) => request(`/repos/${encodeURIComponent(rid)}/reanalyze`,
                                   { method: "POST", body: { ref } }),
  refs: (rid) => request(`/repos/${encodeURIComponent(rid)}/refs`),

  // metrics
  overview: (rid, filters, extra = {}) =>
    request(`/repos/${encodeURIComponent(rid)}/overview`,
            { params: { ...filterParams(filters), ...extra } }),
  browse: (rid, filters, path, kind) =>
    request(`/repos/${encodeURIComponent(rid)}/browse`,
            { params: { ...filterParams(filters), path: path || "", kind } }),
  tree: (rid, filters, metric) =>
    request(`/repos/${encodeURIComponent(rid)}/tree`,
            { params: { ...filterParams(filters), tree_metric: metric } }),
  commits: (rid, filters, { page = 1, size = 50, q = "" } = {}) =>
    request(`/repos/${encodeURIComponent(rid)}/commits`,
            { params: { ...filterParams(filters), page, size, q } }),
  commit: (rid, hash) =>
    request(`/repos/${encodeURIComponent(rid)}/commit/${encodeURIComponent(hash)}`),
  authors: (rid, filters) =>
    request(`/repos/${encodeURIComponent(rid)}/authors`,
            { params: filterParams(filters) }),

  // identities / author merging
  identities: (rid) => request(`/repos/${encodeURIComponent(rid)}/identities`),
  setMerges: (rid, groups) =>
    request(`/repos/${encodeURIComponent(rid)}/merges`,
            { method: "POST", body: { groups } }),

  // search + export
  search: (rid, q) =>
    request(`/repos/${encodeURIComponent(rid)}/search`, { params: { q } }),
  exportUrl: (rid, kind, filters) => {
    const params = new URLSearchParams({ ...filterParams(filters), kind });
    return `/api/repos/${encodeURIComponent(rid)}/export.csv?${params}`;
  },
};
