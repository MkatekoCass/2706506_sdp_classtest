"""REST API for the RAT dashboard.

One Flask blueprint mounted at ``/api``.  Everything the browser needs is a
plain JSON ``GET`` (plus a handful of ``POST``/``DELETE`` for managing
repositories), so the frontend stays a static bundle of HTML/JS/CSS.

Filtering model
---------------
Every metric endpoint accepts the same query parameters, which together
describe *what is being measured*:

``mode``      ``all`` | ``range`` | ``list`` -- how the commit set H is built
``ref``       reference the set must be reachable from (default: the
              reference the repository was analysed from)
``from``/``to``  time window, inclusive/exclusive respectively (unix seconds
              or ISO-8601); matches :math:`H_t` and :math:`H_{i,j}`
``commits``   comma separated hashes/prefixes for ``mode=list``
``authors``   comma separated author-group ids (see ``/identities``)

``from`` is inclusive and ``to`` exclusive because that is exactly the
spec's :math:`H_{i,j} = \\{h : i \\le time(h) < j\\}`.  The UI adds one day to
the chosen end date so "up to 3 June" includes the 3rd.

The commit-set endpoints always report both what was requested and what the
reference actually contains (``dropped``), plus ``unparsed`` for commits that
exist in the graph but were not part of the analysed history, so the UI can
warn instead of showing silently wrong numbers.
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime, timezone

import numpy as np
from flask import Blueprint, Response, jsonify, request

from . import __version__, analysis, gitcmd, ingest, metrics
from .metrics import Selection
from .registry import RepoRegistry, run_job, slugify

#: export formats are capped so a pathological repository cannot OOM the
#: response serializer
MAX_EXPORT_ROWS = 100_000
MAX_PAGE_SIZE = 500


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class JobCancelled(Exception):
    pass


def create_api(registry: RepoRegistry, static_root: str | None = None) -> Blueprint:
    bp = Blueprint("api", __name__)

    # ------------------------------------------------------------------
    # error handling
    # ------------------------------------------------------------------
    @bp.errorhandler(ApiError)
    def _api_error(err: ApiError):
        return jsonify({"error": err.message}), err.status

    @bp.errorhandler(Exception)
    def _unexpected(err: Exception):  # noqa: BLE001 - the API never leaks HTML
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(err).__name__}: {err}"}), 500

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _split(value: str | None) -> list[str]:
        return [v.strip() for v in (value or "").replace("\n", ",").split(",")
                if v.strip()]

    def _parse_ts(value: str | None) -> int | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            pass
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            raise ApiError(400, f"invalid timestamp {text!r}: use unix seconds "
                                f"or an ISO-8601 date") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    def _repo(rid: str) -> dict:
        repo = registry.get(rid)
        if repo is None:
            raise ApiError(404, f"unknown repository '{rid}'")
        return repo

    def _index(rid: str):
        repo = _repo(rid)
        if repo["status"] != "ready":
            raise ApiError(409, f"repository '{rid}' is not ready "
                                f"(status: {repo['status']})")
        try:
            return registry.index(rid)
        except FileNotFoundError as exc:
            raise ApiError(409, f"index missing for '{rid}' -- re-analyse the "
                                f"repository ({exc})") from None
        except (OSError, ValueError, KeyError) as exc:
            raise ApiError(500, f"cannot open the index of '{rid}': {exc}") from None

    def _selection(idx):
        """Resolve the query parameters into (commit set, row selection)."""
        mode = (request.args.get("mode") or "all").strip().lower()
        if mode not in ("all", "range", "list"):
            raise ApiError(400, f"unknown mode '{mode}'")
        ref = (request.args.get("ref") or idx.meta.get("analysis_ref")
               or "HEAD").strip()
        cs = idx.resolve_commitset(
            mode=mode,
            ref=ref,
            t_from=_parse_ts(request.args.get("from")),
            t_to=_parse_ts(request.args.get("to")),
            commits=_split(request.args.get("commits")),
        )
        group_ids = []
        for token in _split(request.args.get("authors")):
            try:
                group_ids.append(int(token))
            except ValueError:
                raise ApiError(400, f"invalid author id {token!r}") from None
        authors = idx.author_mask(group_ids)
        sel = Selection(idx, idx.rows_for(cs, authors), cs)
        return cs, sel, group_ids, ref

    def _cs_payload(idx, cs) -> dict:
        if cs.mode == "all":
            notation = "H\u0304 (all non-merge commits reachable from the reference)"
        elif cs.mode == "range" and cs.t_from is not None and cs.t_to is not None:
            notation = "H_i,j (i <= committer date < j)"
        elif cs.mode == "range" and cs.t_from is not None:
            notation = "H_t (committer date >= t)"
        elif cs.mode == "range":
            notation = "H (committer date < j)"
        else:
            notation = "H (manually selected commits)"
        return {
            "mode": cs.mode,
            "ref": cs.ref,
            "label": cs.label,
            "notation": notation,
            "count": cs.count,
            "sum": cs.count,
            "t_from": cs.t_from,
            "t_to": cs.t_to,
            "requested": cs.requested,
            "dropped": cs.dropped,
            "unparsed": cs.unparsed,
            "partial": cs.unparsed > 0,
            "first": int(idx.ct[cs.mask].min()) if cs.count else None,
            "last": int(idx.ct[cs.mask].max()) if cs.count else None,
            "merge_commits": int((cs.mask & (idx.merge == 1)).sum()),
        }

    def _resolve_target(idx, path: str | None, kind: str | None):
        path = (path or "").strip().strip("/")
        if path == "":
            return "dir", 0, ""
        if kind == "file":
            if path in idx.fid_of:
                return "file", idx.fid_of[path], path
        elif kind == "dir":
            if path in idx.did_of:
                return "dir", idx.did_of[path], path
        else:
            if path in idx.fid_of and path not in idx.did_of:
                return "file", idx.fid_of[path], path
            if path in idx.did_of:
                return "dir", idx.did_of[path], path
            if path in idx.fid_of:
                return "file", idx.fid_of[path], path
        raise ApiError(404, f"no such path in the index: {path!r}")

    def _breadcrumbs(idx, kind: str, path: str) -> list[dict]:
        root = idx.meta.get("name") or "repository"
        crumbs = [{"name": root, "path": "", "kind": "dir"}]
        if kind == "file":
            parts = path.split("/")
            for i in range(1, len(parts)):
                crumbs.append({"name": parts[i - 1], "path": "/".join(parts[:i]),
                               "kind": "dir"})
            crumbs.append({"name": parts[-1], "path": path, "kind": "file"})
        elif path:
            parts = path.split("/")
            for i in range(1, len(parts) + 1):
                crumbs.append({"name": parts[i - 1], "path": "/".join(parts[:i]),
                               "kind": "dir"})
        return crumbs

    # ------------------------------------------------------------------
    # health & repository management
    # ------------------------------------------------------------------
    @bp.get("/health")
    def health():
        return jsonify({"ok": True, "version": __version__,
                        "time": int(time.time())})

    @bp.get("/repos")
    def list_repos():
        return jsonify({"repos": registry.list()})

    @bp.post("/repos")
    def add_repo():
        payload = request.get_json(silent=True) or {}
        source = (payload.get("source") or "").strip()
        if not source:
            raise ApiError(400, "body must contain 'source' (URL or local path)")
        kind = (payload.get("kind") or "").strip().lower()
        if not kind:
            kind = "clone" if ingest._looks_like_url(source) else "local"
        if kind not in ("clone", "local"):
            raise ApiError(400, f"unsupported kind '{kind}' (clone|local)")
        ref = (payload.get("ref") or "HEAD").strip() or "HEAD"
        name = (payload.get("name") or "").strip() or _name_from_source(source)
        repo = registry.create(name, kind, source, analysis_ref=ref,
                               url_name=slugify(source))
        _start_job(registry, repo["id"], kind=kind)
        return jsonify(registry.public(repo["id"])), 202

    @bp.post("/repos/zip")
    def add_repo_zip():
        upload = request.files.get("file")
        if upload is None:
            raise ApiError(400, "expected a multipart form with a file part "
                                "named 'file'")
        name = (request.form.get("name") or "").strip() or _name_from_source(
            upload.filename or "upload")
        ref = (request.form.get("ref") or "HEAD").strip() or "HEAD"
        repo = registry.create(name, "zip", upload.filename or "upload",
                               analysis_ref=ref, url_name=name)
        uploads = os.path.join(registry.data_root, "uploads")
        os.makedirs(uploads, exist_ok=True)
        archive = os.path.join(uploads, f"{repo['id']}-{int(time.time())}.zip")
        upload.save(archive)
        _start_job(registry, repo["id"], kind="zip", archive=archive)
        return jsonify(registry.public(repo["id"])), 202

    @bp.get("/repos/<rid>")
    def get_repo(rid: str):
        return jsonify(registry.public(rid) or _missing(rid))

    @bp.delete("/repos/<rid>")
    def delete_repo(rid: str):
        _repo(rid)
        registry.delete(rid)
        return jsonify({"deleted": rid})

    @bp.post("/repos/<rid>/reanalyze")
    def reanalyze(rid: str):
        repo = _repo(rid)
        payload = request.get_json(silent=True) or {}
        ref = (payload.get("ref") or repo.get("analysis_ref") or "HEAD").strip()
        gitdir, _ = registry.paths(rid)
        if not os.path.exists(gitdir):
            raise ApiError(409, "the git directory was removed; ingest the "
                                "repository again")
        registry.update(rid, analysis_ref=ref)
        _start_job(registry, rid, kind=None, ref=ref)
        return jsonify(registry.public(rid)), 202

    @bp.get("/repos/<rid>/refs")
    def repo_refs(rid: str):
        repo = _repo(rid)
        idx = registry.index(rid)
        gitdir, _ = registry.paths(rid)
        mailmap = gitcmd.cat_blob(gitdir, "HEAD", ".mailmap")
        warnings = list(repo.get("warnings") or [])
        if repo.get("status") != "ready":
            warnings.append(f"status: {repo['status']}")
        return jsonify({
            "repo": registry.public(rid),
            "analysis_ref": repo.get("analysis_ref"),
            "analysis_ref_hash": idx.meta.get("analysis_ref_hash"),
            "refs": [{"name": name, "hash": obj}
                     for name, obj in sorted(idx.refs.items())],
            "mailmap": {
                "present": mailmap is not None,
                "entries": _count_mailmap(mailmap),
                "text": _safe_text(mailmap, 16_384),
            },
            "stats": repo.get("stats"),
            "warnings": warnings,
        })

    # ------------------------------------------------------------------
    # dashboard payloads
    # ------------------------------------------------------------------
    @bp.get("/repos/<rid>/overview")
    def overview(rid: str):
        idx = _index(rid)
        cs, sel, _, _ = _selection(idx)
        dirs = metrics.dir_aggregates(idx, sel)
        metric = request.args.get("tree_metric") or "churn"
        if metric not in ("churn", "added", "removed", "growth", "mods"):
            raise ApiError(400, f"unknown tree metric '{metric}'")
        block = metrics.metric_block(idx, cs, sel, dirs)
        authors = _author_rows(idx, sel, cs, limit=12)
        order = idx.time_order()
        recent_cids = order[cs.mask[order]][:12]
        return jsonify({
            "repo": registry.public(rid),
            "commit_set": _cs_payload(idx, cs),
            "metrics": block,
            "timeline": metrics.timeline(idx, cs, sel,
                                         t_from=cs.t_from, t_to=cs.t_to),
            "calendar": metrics.calendar(idx, cs, sel),
            "tree": metrics.dir_tree(idx, sel, dirs, metric=metric),
            "authors": authors,
            "recent": [metrics.commit_row(idx, int(c)) for c in recent_cids],
        })

    @bp.get("/repos/<rid>/browse")
    def browse(rid: str):
        idx = _index(rid)
        cs, sel, _, _ = _selection(idx)
        kind, oid, path = _resolve_target(idx, request.args.get("path"),
                                          request.args.get("kind"))
        obj = metrics.object_stats(idx, cs, sel, (kind, oid), with_series=True)
        children = []
        if kind == "dir":
            limit = _int_arg("limit", 2000, minimum=1, maximum=10_000)
            children = metrics.children_payload(idx, sel, oid,
                                                total_commits=cs.count,
                                                limit=limit)
        return jsonify({
            "commit_set": _cs_payload(idx, cs),
            "target": {"kind": kind, "id": oid, "path": path},
            "breadcrumbs": _breadcrumbs(idx, kind, path),
            "object": obj,
            "children": children,
            "child_count": len(children),
        })

    @bp.get("/repos/<rid>/tree")
    def tree(rid: str):
        idx = _index(rid)
        cs, sel, _, _ = _selection(idx)
        metric = request.args.get("tree_metric") or "churn"
        if metric not in ("churn", "added", "removed", "growth", "mods"):
            raise ApiError(400, f"unknown tree metric '{metric}'")
        dirs = metrics.dir_aggregates(idx, sel)
        return jsonify({"commit_set": _cs_payload(idx, cs),
                        "tree": metrics.dir_tree(idx, sel, dirs, metric=metric)})

    @bp.get("/repos/<rid>/commits")
    def commits(rid: str):
        idx = _index(rid)
        cs, sel, group_ids, ref = _selection(idx)
        page = _int_arg("page", 1, minimum=1)
        size = _int_arg("size", 50, minimum=1, maximum=MAX_PAGE_SIZE)
        query = (request.args.get("q") or "").strip().lower()

        # The browser always lists the *whole* set H of the reference
        # (optionally time-windowed); in list mode the selected commits are
        # flagged instead of hidden, which is what manual selection needs.
        keep = idx.reachable(ref) & idx.non_merge()
        if cs.mode == "range":
            if cs.t_from is not None:
                keep &= idx.ct >= cs.t_from
            if cs.t_to is not None:
                keep &= idx.ct < cs.t_to
        if group_ids:
            keep &= idx.author_mask(group_ids)[idx.author_of_cid]
        if query:
            keep &= _subject_mask(idx, query)
        order = idx.time_order()
        ordered = order[keep[order]]
        total = int(ordered.size)
        window = ordered[(page - 1) * size: page * size]
        items = [metrics.commit_row(idx, int(c)) for c in window]
        if cs.mode == "list":
            for item in items:
                item["selected"] = bool(cs.mask[idx.hash_to_cid[item["hash"]]])
        return jsonify({
            "commit_set": _cs_payload(idx, cs),
            "total": total,
            "page": page,
            "size": size,
            "pages": max((total + size - 1) // size, 1),
            "items": items,
        })

    @bp.get("/repos/<rid>/commit/<rev>")
    def commit(rid: str, rev: str):
        idx = _index(rid)
        cid = _resolve_commit(idx, rev)
        detail = metrics.commit_detail(idx, cid)
        detail["url"] = {"hash": idx.hashes[cid]}
        return jsonify({"commit": detail})

    @bp.get("/repos/<rid>/authors")
    def authors(rid: str):
        idx = _index(rid)
        cs, sel, group_ids, _ = _selection(idx)
        return jsonify({
            "commit_set": _cs_payload(idx, cs),
            "authors": _author_rows(idx, sel, cs, limit=None),
        })

    @bp.get("/repos/<rid>/identities")
    def identities(rid: str):
        _repo(rid)
        idx = registry.index(rid)
        gitdir, _ = registry.paths(rid)
        mailmap = gitcmd.cat_blob(gitdir, "HEAD", ".mailmap")
        return jsonify({
            "groups": idx.group_meta,
            "raw": idx.rawids,
            "merges": (registry.get(rid) or {}).get("merges") or [],
            "suggestions": _merge_suggestions(idx),
            "mailmap": {
                "present": mailmap is not None,
                "entries": _count_mailmap(mailmap),
                "text": _safe_text(mailmap, 16_384),
            },
        })

    @bp.post("/repos/<rid>/merges")
    def set_merges(rid: str):
        _repo(rid)
        payload = request.get_json(silent=True) or {}
        groups = payload.get("groups")
        if groups is None:
            groups = []
        if not isinstance(groups, list):
            raise ApiError(400, "'groups' must be a list of "
                                "{label, members:[[name,email],...]}")
        clean = []
        for group in groups:
            if not isinstance(group, dict):
                raise ApiError(400, "every merge group must be an object")
            members = []
            for member in group.get("members", []):
                if (isinstance(member, (list, tuple)) and len(member) == 2
                        and all(isinstance(x, str) for x in member)):
                    members.append([member[0], member[1]])
                elif isinstance(member, dict) and "name" in member and "email" in member:
                    members.append([str(member["name"]), str(member["email"])])
                else:
                    raise ApiError(400, "members must be [name, email] pairs")
            if members:
                clean.append({"label": str(group.get("label") or members[0][0]),
                              "members": members})
        registry.set_merges(rid, clean)
        idx = registry.index(rid)
        return jsonify({"ok": True, "groups": idx.group_meta,
                        "merges": clean})

    @bp.get("/repos/<rid>/search")
    def search(rid: str):
        idx = _index(rid)
        q = (request.args.get("q") or "").strip().lower()
        if len(q) < 2:
            return jsonify({"query": q, "files": [], "dirs": [], "commits": []})
        files = [{"path": p, "id": i, "kind": "file"}
                 for i, p in enumerate(idx.files) if q in p.lower()][:40]
        dirs = [{"path": p, "id": i, "kind": "dir"}
                for i, p in enumerate(idx.dirs) if i and q in p.lower()][:20]
        found = []
        for i, h in enumerate(idx.hashes):
            if q in h[:12] or q in idx.subjects[i].lower():
                found.append(metrics.commit_row(idx, i))
                if len(found) >= 20:
                    break
        return jsonify({"query": q, "files": files, "dirs": dirs,
                        "commits": found})

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------
    @bp.get("/repos/<rid>/export.csv")
    def export_csv(rid: str):
        idx = _index(rid)
        cs, sel, _, _ = _selection(idx)
        what = (request.args.get("kind") or "files").lower()
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if what == "files":
            agg = metrics.file_aggregates(idx, sel)
            order = np.argsort(-agg["churn"], kind="stable")
            writer.writerow(["path", "added", "removed", "growth", "churn",
                             "mods", "mod_freq", "churn_rate", "first", "last",
                             "present", "size", "submodule"])
            emitted = 0
            for fid in order:
                if emitted >= MAX_EXPORT_ROWS:
                    break
                churn = int(agg["churn"][fid])
                if churn == 0 and agg["mods"][fid] == 0:
                    continue
                writer.writerow([
                    idx.files[int(fid)], int(agg["added"][fid]),
                    int(agg["removed"][fid]), int(agg["growth"][fid]), churn,
                    int(agg["mods"][fid]),
                    _rate(int(agg["mods"][fid]), cs.count),
                    _rate(churn, cs.count),
                    _fmt_ts(agg["first"][fid]), _fmt_ts(agg["last"][fid]),
                    int(idx.present[int(fid)]), int(idx.sizes[int(fid)]),
                    int(idx.file_kind[int(fid)] == 1),
                ])
                emitted += 1
        elif what == "dirs":
            dirs = metrics.dir_aggregates(idx, sel)
            order = np.argsort(-dirs["churn"], kind="stable")
            writer.writerow(["path", "added", "removed", "growth", "churn",
                             "mods", "mod_freq", "churn_rate", "files",
                             "subdirs"])
            emitted = 0
            for did in order:
                did = int(did)
                if emitted >= MAX_EXPORT_ROWS:
                    break
                if did != 0 and dirs["churn"][did] == 0 and dirs["mods"][did] == 0:
                    continue
                flo, fhi = idx.subtree_file_range(did)
                dlo, dhi = idx.subtree_dir_range(did)
                writer.writerow([
                    idx.dirs[did] or "/", int(dirs["added"][did]),
                    int(dirs["removed"][did]), int(dirs["growth"][did]),
                    int(dirs["churn"][did]), int(dirs["mods"][did]),
                    _rate(int(dirs["mods"][did]), cs.count),
                    _rate(int(dirs["churn"][did]), cs.count),
                    fhi - flo, dhi - dlo,
                ])
        elif what == "authors":
            agg = metrics.author_aggregates(idx, sel)
            writer.writerow(["author", "emails", "merged", "commits", "added",
                             "removed", "growth", "churn", "files", "dirs",
                             "first", "last", "share"])
            total_churn = int(agg["churn"].sum()) or 1
            for row in _author_rows(idx, sel, cs, limit=None):
                writer.writerow([
                    row["label"], "; ".join(row["emails"]),
                    int(row["merged"]), row["commits"], row["added"],
                    row["removed"], row["growth"], row["churn"],
                    row["files"], row["dirs"], _fmt_ts(row["first"]),
                    _fmt_ts(row["last"]),
                    round(row["churn"] / total_churn, 6),
                ])
        elif what == "commits":
            writer.writerow(["hash", "date", "author", "subject", "added",
                             "removed", "churn", "growth", "files", "binary"])
            order = idx.time_order()
            emitted = 0
            for cid in order[cs.mask[order]]:
                if emitted >= MAX_EXPORT_ROWS:
                    break
                row = metrics.commit_row(idx, int(cid))
                writer.writerow([
                    row["hash"], _fmt_ts(row["ct"]), row["author"],
                    row["subject"], row["added"], row["removed"],
                    row["churn"], row["growth"], row["files"],
                    row["binary"],
                ])
                emitted += 1
        else:
            raise ApiError(400, f"unknown export kind '{what}' "
                                f"(files|dirs|authors|commits)")
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="rat-{rid}-{what}.csv"'},
        )

    # ------------------------------------------------------------------
    # the background job
    # ------------------------------------------------------------------
    def _start_job(reg: RepoRegistry, rid: str, *, kind: str | None,
                   archive: str | None = None, ref: str | None = None) -> None:
        run_job(reg, rid, _work(reg, rid, kind=kind, archive=archive, ref=ref))

    return bp


# ----------------------------------------------------------------------
# module level helpers (kept out of the closure where they are reusable)
# ----------------------------------------------------------------------

_ANALYSIS_WEIGHTS = {
    "diff": (0.0, 0.85),
    "graph": (0.85, 0.90),
    "normalise": (0.90, 0.96),
    "write": (0.96, 1.0),
    "done": (1.0, 1.0),
}


def _work(registry: RepoRegistry, rid: str, *, kind: str | None,
          archive: str | None, ref: str | None):
    """Return the callable executed in the background thread."""

    def work() -> None:
        repo = registry.get(rid)
        if repo is None:
            return
        gitdir, index_dir = registry.paths(rid)
        analysis_ref = ref or repo.get("analysis_ref") or "HEAD"
        cancel = registry.cancel_event(rid)

        def check() -> None:
            if cancel.is_set():
                raise JobCancelled()

        def ingest_report(message: str) -> None:
            check()
            registry.set_job(rid, phase="ingest", message=message[:400],
                             finished=False)

        def analysis_report(phase: str, done: int, total: int, message: str) -> None:
            check()
            lo, hi = _ANALYSIS_WEIGHTS.get(phase, (0.0, 1.0))
            fraction = (done / total) if total else 1.0
            registry.set_job(
                rid, phase=phase, done=done, total=total,
                progress=round(lo + (hi - lo) * max(0.0, min(1.0, fraction)), 4),
                message=message[:400], finished=False)

        try:
            info: dict = {}
            if kind == "clone":
                registry.update(rid, status="ingesting")
                ingest_report(f"cloning {repo['source']}")
                info = ingest.ingest_clone(repo["source"], gitdir,
                                           progress=ingest_report)
            elif kind == "local":
                registry.update(rid, status="ingesting")
                info = ingest.ingest_local(repo["source"], gitdir,
                                           progress=ingest_report)
            elif kind == "zip":
                registry.update(rid, status="ingesting")
                assert archive is not None
                info = ingest.ingest_zip(archive, gitdir, progress=ingest_report)
            check()

            resolved = gitcmd.rev_parse(gitdir, analysis_ref)
            if resolved is None:
                refs = sorted(gitcmd.show_ref(gitdir).keys())[:12]
                hint = ", ".join(refs) if refs else "HEAD"
                raise analysis.AnalysisError(
                    f"reference '{analysis_ref}' does not exist in this "
                    f"repository (available: {hint})")

            registry.update(rid, status="analyzing")
            registry.set_job(rid, phase="analyze", message="starting analysis",
                             finished=False)
            warnings = list(info.get("warnings") or [])
            if info.get("has_mailmap"):
                warnings.append(
                    f".mailmap detected ({info.get('mailmap_entries', 0)} "
                    f"entries): author identities are resolved by git")
            stats = analysis.analyze(gitdir, index_dir, analysis_ref,
                                     progress=analysis_report)
            registry.update(rid, stats=stats, warnings=warnings)
            registry.invalidate(rid)
            registry.set_job(rid, phase="done", progress=1.0, finished=True,
                             message=(f"indexed {stats['commits']} commits, "
                                      f"{stats['files']} files in "
                                      f"{stats['duration']}s"))
            registry.finish_job(rid, status="ready")
        except JobCancelled:
            registry.update(rid, status="cancelled", error="cancelled by user")
            registry.set_job(rid, phase="cancelled", finished=True,
                             message="cancelled")
            registry.invalidate(rid)
        finally:
            if archive:
                try:
                    os.remove(archive)
                except OSError:
                    pass

    return work


def _name_from_source(source: str) -> str:
    name = source.rstrip("/").rsplit("/", 1)[-1]
    if ":" in name:                      # scp-like url: user@host:path
        name = name.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repository"


def _missing(rid: str):
    raise ApiError(404, f"unknown repository '{rid}'")


def _author_rows(idx, sel: Selection, cs, *, limit: int | None) -> list[dict]:
    agg = metrics.author_aggregates(idx, sel)
    total_churn = int(agg["churn"].sum())
    rows: list[dict] = []
    for gid in range(idx.group_count):
        churn = int(agg["churn"][gid])
        commits = int(agg["commits"][gid])
        if churn == 0 and commits == 0:
            continue
        meta = idx.group_meta[gid]
        rows.append({
            "id": gid,
            "label": meta["label"],
            "emails": meta["emails"],
            "merged": meta["merged"],
            "commits": commits,
            "added": int(agg["added"][gid]),
            "removed": int(agg["removed"][gid]),
            "growth": int(agg["growth"][gid]),
            "churn": churn,
            "files": int(agg["files"][gid]),
            "dirs": int(agg["dirs"][gid]),
            "first": int(agg["first"][gid]) if agg["first"][gid] >= 0 else None,
            "last": int(agg["last"][gid]) if agg["last"][gid] >= 0 else None,
            "share": round(churn / total_churn, 6) if total_churn else 0.0,
            "mods": commits,          # n_H,root,a -- every commit touches the repo
            "mod_freq": _rate(commits, cs.count),
        })
    rows.sort(key=lambda r: (-r["churn"], r["label"]))
    return rows[:limit] if limit else rows


def _merge_suggestions(idx) -> list[dict]:
    """Heuristic merge candidates: same email / same name, different identity."""
    by_email: dict[str, list[int]] = {}
    by_name: dict[str, list[int]] = {}
    for i, ident in enumerate(idx.identities):
        by_email.setdefault(ident["email"].lower(), []).append(i)
        by_name.setdefault(ident["name"].lower(), []).append(i)
    out: list[dict] = []
    seen: set[tuple] = set()
    for reason, table in (
        ("same email, different names", by_email),
        ("same name, different emails", by_name),
    ):
        for members in table.values():
            if len(members) < 2:
                continue
            members = sorted(members)[:8]
            key = (reason, tuple(members))
            if key in seen:
                continue
            seen.add(key)
            if len({int(idx.identity_group[i]) for i in members}) == 1:
                continue      # already one author
            out.append({
                "reason": reason,
                "members": [{"name": idx.identities[i]["name"],
                             "email": idx.identities[i]["email"],
                             "commits": idx.identities[i]["commits"]}
                            for i in members],
            })
    out.sort(key=lambda s: -sum(m["commits"] for m in s["members"]))
    return out[:30]


def _resolve_commit(idx, rev: str) -> int:
    token = rev.strip()
    cid = idx.hash_to_cid.get(token)
    if cid is None:
        matches = [h for h in idx.hashes if h.startswith(token)]
        if len(matches) == 1:
            cid = idx.hash_to_cid[matches[0]]
        elif len(matches) > 1:
            raise ApiError(409, f"ambiguous commit '{rev}' "
                                f"({len(matches)} matches)")
    if cid is None:
        raise ApiError(404, f"unknown commit '{rev}'")
    return cid


def _subject_mask(idx, query: str) -> np.ndarray:
    cached = getattr(idx, "_subject_lower", None)
    if cached is None:
        cached = [s.lower() for s in idx.subjects]
        idx._subject_lower = cached
    hits = np.fromiter((query in s for s in cached), dtype=bool,
                       count=len(cached))
    return hits


def _count_mailmap(blob: bytes | None) -> int:
    if not blob:
        return 0
    return sum(1 for line in blob.decode("utf-8", "replace").splitlines()
               if line.strip() and not line.lstrip().startswith("#"))


def _safe_text(blob: bytes | None, cap: int) -> str | None:
    if blob is None:
        return None
    text = blob.decode("utf-8", "replace")
    return text if len(text) <= cap else text[:cap] + "\n... (truncated)"


def _rate(value: int, total: int) -> float:
    return round(value / total, 6) if total else 0.0


def _fmt_ts(ts) -> str:
    ts = int(ts)
    if ts < 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S")


def _int_arg(name: str, default: int, *, minimum: int = 0,
             maximum: int | None = None) -> int:
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ApiError(400, f"parameter {name!r} must be an integer") from None
    if value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value
