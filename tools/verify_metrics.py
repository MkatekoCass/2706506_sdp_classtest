#!/usr/bin/env python3
"""Independent cross-check of the RAT metric engine.

The engine (:mod:`rat.metrics`) computes metrics with vectorised numpy
reductions over a columnar index.  This script recomputes the *same* metrics
with a deliberately naive, independent implementation and compares them:

* every commit is diffed with its own ``git diff-tree --root -r -M50%
  --numstat -z`` invocation (the engine uses one streaming ``git log``);
* directory metrics are accumulated by the spec's literal recursion over
  immediate children (the engine distributes rows over ancestor chains);
* commit sets are resolved from ``git rev-list`` output (the engine walks
  the stored parent graph);
* everything is plain Python dictionaries and integers (the engine is numpy).

Usage::

    python3 tools/verify_metrics.py data/repos/<id>/repo.git [--index DIR]
                                [--commits N] [--json]

Exit status is non-zero when any check fails, so it doubles as a test.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from rat.repoindex import RepoIndex  # noqa: E402
from rat import metrics as M  # noqa: E402


# --------------------------------------------------------------------------
# naive reference implementation
# --------------------------------------------------------------------------

def git(gitdir: str, *args: str) -> bytes:
    proc = subprocess.run(["git", "--git-dir", gitdir, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode()}")
    return proc.stdout


def commit_metadata(gitdir: str) -> dict[str, dict]:
    """hash -> {ct, author, email} for every commit, via one git log call."""
    out = git(gitdir, "log", "--all", "--format=%H%x1f%ct%x1f%aN%x1f%aE%x1e")
    meta: dict[str, dict] = {}
    for record in out.split(b"\x1e"):
        record = record.strip(b"\n")
        if not record:
            continue
        parts = record.split(b"\x1f")
        if len(parts) != 4:
            continue
        meta[parts[0].decode()] = {
            "ct": int(parts[1]),
            "author": parts[2].decode("utf-8", "replace"),
            "email": parts[3].decode("utf-8", "replace"),
        }
    return meta


def commit_rows(gitdir: str, commit: str) -> list[tuple[str, str | None, int, int]]:
    """(new path, old path, added, removed) for one commit, via git diff-tree."""
    raw = git(gitdir, "diff-tree", "--root", "-r", "-M50%", "--numstat", "-z",
              "--no-ext-diff", "--no-textconv", "--no-commit-id", commit)
    tokens = raw.split(b"\0")
    rows: list[tuple[str, str | None, int, int]] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if i == 0:
            token = token.lstrip(b"\n\0")
        if not token:
            i += 1
            continue
        parts = token.split(b"\t", 2)
        if len(parts) < 3:
            i += 1
            continue
        added, removed, path = parts
        if path:
            if added != b"-":                # '-' -> git flagged the blob binary
                rows.append((path.decode("utf-8", "replace"), None,
                             int(added), int(removed)))
            i += 1
        else:
            old, new = tokens[i + 1], tokens[i + 2]
            if added != b"-":
                rows.append((new.decode("utf-8", "replace"),
                             old.decode("utf-8", "replace"),
                             int(added), int(removed)))
            i += 3
    return rows


class Reference:
    """Literal (slow) implementation of the spec over plain dictionaries."""

    def __init__(self, gitdir: str, index: RepoIndex, limit: int | None = None):
        self.gitdir = gitdir
        self.meta = commit_metadata(gitdir)
        self.rows: dict[str, list] = {}
        self.calls = 0
        # gitlinks (submodules) are not files -- same exclusion rule as engine
        self.gitlinks = set()
        for line in git(gitdir, "ls-tree", "-r", "-l", "HEAD").split(b"\n"):
            meta, _tab, path = line.partition(b"\t")
            if path and meta.split()[0] == b"160000":
                self.gitlinks.add(path.decode("utf-8", "replace"))

    def load(self, hashes: list[str], on_step=None) -> None:
        for n, commit in enumerate(hashes):
            self.rows[commit] = [
                r for r in commit_rows(self.gitdir, commit)
                if r[0] not in self.gitlinks
            ]
            self.calls += 1
            if on_step and n % 200 == 0:
                on_step(n, len(hashes))

    def non_merge(self, ref: str = "HEAD") -> list[str]:
        out = git(self.gitdir, "rev-list", "--no-merges", ref)
        return [h.strip() for h in out.decode().splitlines() if h.strip()]

    # -- spec 2.1 / 2.2 ---------------------------------------------------
    def per_commit_files(self, commit: str) -> dict[str, tuple[int, int]]:
        files: dict[str, tuple[int, int]] = {}
        for path, _old, added, removed in self.rows[commit]:
            prev = files.get(path, (0, 0))
            files[path] = (prev[0] + added, prev[1] + removed)
        return files

    def dir_tree(self, files: dict[str, tuple[int, int]]) -> dict[str, dict]:
        """Build directory metrics by the spec's immediate-children recursion."""
        tree: dict[str, dict] = {"": {"files": {}, "dirs": {}}}

        def ensure(path: str) -> dict:
            if path in tree:
                return tree[path]
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            node = {"files": {}, "dirs": {}}
            tree[path] = node
            ensure(parent)["dirs"][path] = True
            return node

        for path, stats in files.items():
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            ensure(parent)["files"][path] = stats
        sums: dict[str, tuple[int, int]] = {}

        def total(path: str) -> tuple[int, int]:
            if path in sums:
                return sums[path]
            node = tree[path]
            added = removed = 0
            for _f, (a, r) in node["files"].items():
                added += a
                removed += r
            for sub in list(node["dirs"]):
                a, r = total(sub)
                added += a
                removed += r
            sums[path] = (added, removed)
            return sums[path]

        for path in list(tree):
            total(path)
        return {path: {"added": a, "removed": r} for path, (a, r) in sums.items()}

    def aggregate(self, commits: list[str], author: str | None = None
                  ) -> dict:
        """Metrics over a commit set (optionally restricted to one author)."""
        repo = [0, 0]        # added, removed of the root
        dir_totals: dict[str, list[int]] = {}
        dir_mods: dict[str, int] = {}
        file_totals: dict[str, list[int]] = {}
        file_mods: dict[str, int] = {}
        author_churn: dict[str, int] = {}
        author_file_churn: dict[tuple[str, str], int] = {}
        author_file_mods: dict[tuple[str, str], int] = {}
        commits_with_changes = 0
        for commit in commits:
            info = self.meta.get(commit)
            if info is None:
                continue
            rows = self.rows.get(commit, [])
            files = self.per_commit_files(commit)
            if not rows:
                continue
            dirs = self.dir_tree(files)
            active = {p: s for p, s in files.items() if s[0] + s[1] > 0}
            active_dirs = {p: s for p, s in dirs.items()
                           if s["added"] + s["removed"] > 0}
            if not active and not any(v["added"] + v["removed"] for v in dirs.values()):
                continue
            commits_with_changes += 1
            for path, (a, r) in files.items():
                entry = file_totals.setdefault(path, [0, 0, 0])
                entry[0] += a
                entry[1] += r
            for path, stats in dirs.items():
                entry = dir_totals.setdefault(path, [0, 0, 0])
                entry[0] += stats["added"]
                entry[1] += stats["removed"]
            for path in active:
                file_mods[path] = file_mods.get(path, 0) + 1
            for path in active_dirs:
                dir_mods[path] = dir_mods.get(path, 0) + 1
            repo[0] += dirs[""]["added"]
            repo[1] += dirs[""]["removed"]
            who = f"{info['author']} <{info['email']}>"
            # author churn per object (spec 2.5)
            churn = sum(a + r for a, r in files.values())
            author_churn[who] = author_churn.get(who, 0) + churn
            for path, (a, r) in files.items():
                key = (who, path)
                author_file_churn[key] = author_file_churn.get(key, 0) + a + r
                if a + r > 0:
                    author_file_mods[key] = author_file_mods.get(key, 0) + 1
            for path, stats in dirs.items():
                key = (who, path)
                author_file_churn[key] = author_file_churn.get(key, 0) + stats["added"] + stats["removed"]
                if stats["added"] + stats["removed"] > 0:
                    author_file_mods[key] = author_file_mods.get(key, 0) + 1
        return {
            "repo": repo,
            "dir_totals": dir_totals,
            "dir_mods": dir_mods,
            "file_totals": file_totals,
            "file_mods": file_mods,
            "author_churn": author_churn,
            "author_object_churn": author_file_churn,
            "author_object_mods": author_file_mods,
            "commits_with_changes": commits_with_changes,
        }


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def check(self, name: str, expected, actual, detail: str = "") -> None:
        ok = expected == actual
        self.checks.append({"name": name, "ok": ok, "expected": expected,
                            "actual": actual, "detail": detail})
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}: expected={expected} actual={actual}"
              + (f" ({detail})" if detail and not ok else ""))

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


def author_key(idx: RepoIndex, cid: int) -> str:
    group = idx.group_meta[int(idx.author_of_cid[cid])]
    email = group["emails"][0] if group["emails"] else ""
    return f"{group['name']} <{email}>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gitdir", help="path passed to git --git-dir")
    parser.add_argument("--index", help="index directory (default <gitdir>/index)")
    parser.add_argument("--commits", type=int, default=0,
                        help="limit the number of commits to cross-check (0 = all)")
    parser.add_argument("--samples", type=int, default=12,
                        help="objects sampled for comparison")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    gitdir = os.path.abspath(args.gitdir)
    index_dir = args.index or os.path.join(gitdir, "index")
    idx = RepoIndex(index_dir)
    report = Report()

    print(f"repository : {idx.meta.get('name', os.path.basename(gitdir))}")
    print(f"index      : {index_dir}")
    print(f"commits    : {idx.n_commits} ({int((idx.merge == 0).sum())} non-merge), "
          f"{idx.n_changes} change rows, {idx.n_files} files, {idx.n_dirs - 1} dirs")

    ref = idx.meta.get("analysis_ref", "HEAD")
    t0 = time.time()
    reference = Reference(gitdir, idx)
    all_commits = reference.non_merge(ref)
    if args.commits:
        all_commits = all_commits[:args.commits]
    print(f"reference  : diffing {len(all_commits)} commits one by one ...")
    reference.load(all_commits, on_step=lambda n, total: (
        print(f"             {n}/{total}", end="\r", flush=True) if n % 400 == 0 else None))
    print(f"             done in {time.time() - t0:.1f}s "
          f"({reference.calls} git invocations)")

    # ---- commit set: all history of the analysis reference --------------
    cs = idx.resolve_commitset("all", ref=ref)
    sel = M.Selection(idx, idx.rows_for(cs), cs)
    dirs = M.dir_aggregates(idx, sel)
    files = M.file_aggregates(idx, sel)
    authors = M.author_aggregates(idx, sel)

    # the naive side only knows commits that are in the index (parsed history)
    known = [h for h in all_commits if h in idx.hash_to_cid]
    expected = reference.aggregate(known)

    print("\n== commit-set resolution ==")
    engine_analysed = {idx.hashes[c]
                       for c in np_flat(cs.mask & (idx.diffed == 1))}
    expected_analysed = {h for h in known if h in idx.hash_to_cid}
    report.check("H-bar(ref) analysed commits == git rev-list --no-merges",
                 True, engine_analysed == expected_analysed,
                 detail=f"engine={len(engine_analysed)} ref={len(expected_analysed)}")
    report.check("|H| = analysed + unparsed(not analysed)",
                 cs.count, len(expected_analysed) + cs.unparsed)

    print("\n== repository metrics (root directory, spec 2.3) ==")
    report.check("added", expected["repo"][0], int(dirs["added"][0]))
    report.check("removed", expected["repo"][1], int(dirs["removed"][0]))
    report.check("growth", expected["repo"][0] - expected["repo"][1],
                 int(dirs["growth"][0]))
    report.check("churn", expected["repo"][0] + expected["repo"][1],
                 int(dirs["churn"][0]))
    report.check("modifications", expected["dir_mods"].get("", 0),
                 int(dirs["mods"][0]))

    print("\n== directory metrics (sampled) ==")
    rng = random.Random(20260101)
    dir_paths = [p for p in expected["dir_totals"] if p and p in idx.did_of]
    for path in rng.sample(dir_paths, min(args.samples, len(dir_paths))):
        did = idx.did_of[path]
        add, rem = expected["dir_totals"][path][0], expected["dir_totals"][path][1]
        report.check(f"dir {path!r} added", add, int(dirs["added"][did]))
        report.check(f"dir {path!r} removed", rem, int(dirs["removed"][did]))
        report.check(f"dir {path!r} churn", add + rem, int(dirs["churn"][did]))
        report.check(f"dir {path!r} mods", expected["dir_mods"].get(path, 0),
                     int(dirs["mods"][did]))

    print("\n== file metrics (sampled) ==")
    file_paths = [p for p in expected["file_totals"] if p in idx.fid_of]
    for path in rng.sample(file_paths, min(args.samples, len(file_paths))):
        fid = idx.fid_of[path]
        add, rem = expected["file_totals"][path][0], expected["file_totals"][path][1]
        report.check(f"file {path!r} added", add, int(files["added"][fid]))
        report.check(f"file {path!r} removed", rem, int(files["removed"][fid]))
        report.check(f"file {path!r} churn", add + rem, int(files["churn"][fid]))
        report.check(f"file {path!r} mods", expected["file_mods"].get(path, 0),
                     int(files["mods"][fid]))

    print("\n== author metrics (sampled) ==")
    author_groups: dict[str, int] = {}
    for cid in np_flat(cs.mask):
        author_groups.setdefault(author_key(idx, cid), int(idx.author_of_cid[cid]))
    sample_authors = rng.sample(sorted(author_groups), min(6, len(author_groups)))
    for who in sample_authors:
        gid = author_groups[who]
        report.check(f"author {who!r} churn", expected["author_churn"].get(who, 0),
                     int(authors["churn"][gid]))

    print("\n== ownership (spec 2.5, sampled objects) ==")
    for path in rng.sample(file_paths, min(5, len(file_paths))):
        fid = idx.fid_of[path]
        ownership = M.object_ownership(idx, sel, ("file", fid))
        naive = {who: churn for (who, p), churn in expected["author_object_churn"].items()
                 if p == path}
        total = sum(naive.values())
        engine = {entry["label"] + " <" + (entry["emails"][0] if entry["emails"] else "") + ">":
                  entry["churn"] for entry in ownership}
        report.check(f"ownership {path!r} (lambda per author)",
                     dict(sorted(naive.items())), dict(sorted(engine.items())))
        for entry in ownership:
            who = entry["label"] + " <" + (entry["emails"][0] if entry["emails"] else "") + ">"
            expect_share = round(naive.get(who, 0) / total, 4) if total else 0.0
            # the engine reports 6 decimals; compare at display precision
            report.check(f"omega({path!r}, {who!r})", expect_share,
                         round(entry["share"], 4))

    print("\n== time-windowed commit sets (H_i,j, spec 2) ==")
    times = sorted(idx.ct[cs.mask])
    split = times[len(times) // 2]
    cs_range = idx.resolve_commitset("range", ref=ref, t_from=int(times[0]),
                                     t_to=int(split))
    sel_range = M.Selection(idx, idx.rows_for(cs_range), cs_range)
    dirs_range = M.dir_aggregates(idx, sel_range)
    naive_range = reference.aggregate(
        [h for h in known if reference.meta[h]["ct"] < split])
    report.check(f"window [{times[0]}, {split}) size", cs_range.count,
                 len([h for h in known if reference.meta[h]["ct"] < split]))
    report.check("window added", naive_range["repo"][0], int(dirs_range["added"][0]))
    report.check("window removed", naive_range["repo"][1], int(dirs_range["removed"][0]))

    print("\n== manual commit list (spec 2) ==")
    picked = sorted(rng.sample(known, min(40, len(known))))
    cs_list = idx.resolve_commitset("list", ref=ref, commits=picked)
    sel_list = M.Selection(idx, idx.rows_for(cs_list), cs_list)
    dirs_list = M.dir_aggregates(idx, sel_list)
    naive_list = reference.aggregate(picked)
    report.check("list size", len(picked), cs_list.count)
    report.check("list added", naive_list["repo"][0], int(dirs_list["added"][0]))
    report.check("list removed", naive_list["repo"][1], int(dirs_list["removed"][0]))

    print("\n== per-commit metrics (spec 2.1, sampled commits) ==")
    for commit in rng.sample(known, min(5, len(known))):
        cid = idx.hash_to_cid[commit]
        detail = M.commit_detail(idx, cid)
        files_map = reference.per_commit_files(commit)
        expected_files = {p: (a, r) for p, (a, r) in files_map.items()
                          if p not in reference.gitlinks}
        actual_files = {c["path"]: (c["added"], c["removed"]) for c in detail["changes"]
                        if not c["binary"] and not c["submodule"]}
        report.check(f"commit {commit[:10]} file stats",
                     dict(sorted(expected_files.items())),
                     dict(sorted(actual_files.items())))
        dirs_naive = reference.dir_tree(expected_files)
        report.check(f"commit {commit[:10]} root added/removed",
                     (dirs_naive[""]["added"], dirs_naive[""]["removed"]),
                     (detail["added"], detail["removed"]))

    print(f"\n{len(report.checks)} checks, {len(report.failures)} failures")
    if args.json:
        print(json.dumps({"checks": report.checks,
                          "failures": report.failures}, indent=2))
    return 1 if report.failures else 0


def np_flat(mask):
    import numpy as np
    return np.flatnonzero(mask).tolist()


if __name__ == "__main__":
    sys.exit(main())
