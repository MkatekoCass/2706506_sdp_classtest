"""Metric engine -- the spec's formulas, vectorised.

Every function here is a reduction over a :class:`Selection` (the change rows
that a commit set and author filter resolve to).  The mapping to the spec is:

======================  =====================================================
spec                     implementation
======================  =====================================================
``l+h,f`` / ``l-h,f``    ``ch_added`` / ``ch_removed`` (one row per (commit,
                         object); renames already resolved by git at ``-M50%``
                         and attributed to the new path)
``delta_h,f``            ``+= l+ - l-``      (:func:`file_metrics`)
``lambda_h,f``           ``+= l+ + l-``      (:func:`file_metrics`)
directory metrics        subtree sums; a directory's metrics are the sums over
                         its immediate children, which by induction equal the
                         sums over every measured file below it, so the engine
                         adds each row to every ancestor directory
                         (:func:`dir_aggregates`)
repository metrics       directory metrics of the root (``did = 0``)
``l+H,o ... lambda_H,o`` sums over ``h in H`` (:func:`object_stats`)
``n_H,o``                rows with ``lambda > 0``, de-duplicated per commit so
                         a commit that touches two files of one directory
                         counts once for that directory (:func:`modifications`)
``eta_H,o``              ``n_H,o / |H|``
``rho_H,o``              ``lambda_H,o / |H|``
``n_H,o,a`` / ``lambda_H,o,a``  the same reductions restricted to the author's
                         commits (:func:`object_ownership`)
``omega_H,o,a``          ``lambda_H,o,a / lambda_H,o``
======================  =====================================================

All values are integers except the derived rates, which are returned as
floats rounded for display.  Because the sums are exact integer arithmetic
(numpy int64 / float64 with magnitudes far below 2^53) the numbers match a
naive per-commit implementation exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .analysis import FLAG_BINARY, FLAG_RENAME, FLAG_SUBMODULE
from .repoindex import CommitSet, RepoIndex

#: bucket sizes in seconds used by the timeline (hour .. year)
BUCKETS = [
    ("hour", 3600),
    ("day", 86400),
    ("week", 604800),
    ("month", 2629800),
    ("year", 31557600),
]

DAY = 86400


@dataclass
class Selection:
    """Materialised change rows of one query."""

    idx: RepoIndex
    mask: np.ndarray
    cs: CommitSet
    rows: np.ndarray = field(init=False)
    cid: np.ndarray = field(init=False)
    fid: np.ndarray = field(init=False)
    added: np.ndarray = field(init=False)
    removed: np.ndarray = field(init=False)
    churn: np.ndarray = field(init=False)
    flags: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        idx = self.idx
        self.rows = np.flatnonzero(self.mask)
        self.cid = idx.ch_cid[self.rows].astype(np.int64)
        self.fid = idx.ch_fid[self.rows].astype(np.int64)
        self.added = idx.ch_added[self.rows].astype(np.int64)
        self.removed = idx.ch_removed[self.rows].astype(np.int64)
        self.churn = self.added + self.removed
        self.flags = idx.ch_flags[self.rows]

    @property
    def size(self) -> int:
        return self.cid.size

    @property
    def active(self) -> np.ndarray:
        """Rows that actually changed lines (lambda > 0) -- spec I_n test."""
        return self.churn > 0

    @property
    def commits(self) -> np.ndarray:
        """Distinct commits that have at least one measured change."""
        return np.unique(self.cid)


# --------------------------------------------------------------------------
# per-object aggregates
# --------------------------------------------------------------------------

def file_aggregates(idx: RepoIndex, sel: Selection) -> dict[str, np.ndarray]:
    """Per-file l+, l-, delta and lambda over the selection."""
    n = idx.n_files
    added = np.bincount(sel.fid, weights=sel.added, minlength=n).astype(np.int64)
    removed = np.bincount(sel.fid, weights=sel.removed, minlength=n).astype(np.int64)
    mods = np.bincount(sel.fid[sel.active], minlength=n).astype(np.int64)
    first = np.full(n, -1, dtype=np.int64)
    last = np.full(n, -1, dtype=np.int64)
    if sel.active.any():
        fid = sel.fid[sel.active]
        times = idx.ct[sel.cid[sel.active]]
        order = np.lexsort((times, fid))
        fid_sorted, time_sorted = fid[order], times[order]
        boundaries = np.flatnonzero(np.diff(fid_sorted)) + 1
        starts = np.concatenate(([0], boundaries))
        ends = np.concatenate((boundaries, [fid_sorted.size]))
        first[fid_sorted[starts]] = time_sorted[starts]
        last[fid_sorted[ends - 1]] = time_sorted[ends - 1]
    return {
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": added + removed,
        "mods": mods,
        "first": first,
        "last": last,
    }


def dir_aggregates(
    idx: RepoIndex,
    sel: Selection,
    *,
    mods: bool = True,
) -> dict[str, np.ndarray]:
    """Directory (and hence repository) metrics over the selection.

    Directory metrics are defined recursively over immediate children; the
    engine distributes every file row over the file's full ancestor chain,
    which computes the same sums (:math:`l^+_{h,d} = \\sum_{f \\in d} l^+_{h,f}`)
    in a single pass.
    """
    n_dirs = idx.n_dirs
    n_files = idx.n_files
    if sel.size:
        row_pos, dids = idx.expand(sel.fid, include_self=False)
        added = np.bincount(dids, weights=sel.added[row_pos],
                            minlength=n_dirs).astype(np.int64)
        removed = np.bincount(dids, weights=sel.removed[row_pos],
                              minlength=n_dirs).astype(np.int64)
    else:
        added = np.zeros(n_dirs, dtype=np.int64)
        removed = np.zeros(n_dirs, dtype=np.int64)
    result = {
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": added + removed,
    }
    if mods:
        result["mods"] = dir_modifications(idx, sel)
    return result


def dir_modifications(idx: RepoIndex, sel: Selection) -> np.ndarray:
    """n_H,d -- commits in which a directory (any descendant file) changed.

    A commit must be counted once per directory, so the (directory, commit)
    pairs are de-duplicated before counting.
    """
    n_dirs, n_commits = idx.n_dirs, idx.n_commits
    counts = np.zeros(n_dirs, dtype=np.int64)
    active = sel.active
    if not active.any():
        return counts
    row_pos, dids = idx.expand(sel.fid[active], include_self=False)
    if dids.size == 0:
        return counts
    commits = sel.cid[active][row_pos]
    keys = dids.astype(np.int64) * n_commits + commits
    unique = np.unique(keys)
    return np.bincount((unique // n_commits).astype(np.int64),
                       minlength=n_dirs).astype(np.int64)


def author_aggregates(idx: RepoIndex, sel: Selection) -> dict[str, np.ndarray]:
    """Per-author l+, l-, lambda and commit counts over the selection."""
    n_groups = idx.group_count
    groups = idx.author_of_cid[sel.cid]
    added = np.bincount(groups, weights=sel.added, minlength=n_groups).astype(np.int64)
    removed = np.bincount(groups, weights=sel.removed, minlength=n_groups).astype(np.int64)
    commits = np.unique(sel.cid)
    commit_groups = idx.author_of_cid[commits]
    commit_counts = np.bincount(commit_groups, minlength=n_groups).astype(np.int64)
    active_files = np.unique(groups[sel.active] * idx.n_files + sel.fid[sel.active])
    file_counts = np.bincount(active_files // idx.n_files,
                              minlength=n_groups).astype(np.int64)
    dirs = np.zeros(n_groups, dtype=np.int64)
    active = sel.active
    if active.any():
        row_pos, dids = idx.expand(sel.fid[active], include_self=False)
        if dids.size:
            pairs = np.unique(groups[active][row_pos] * idx.n_dirs + dids)
            dirs = np.bincount(pairs // idx.n_dirs, minlength=n_groups).astype(np.int64)
    first = np.full(n_groups, -1, dtype=np.int64)
    last = np.full(n_groups, -1, dtype=np.int64)
    if commits.size:
        times = idx.ct[commits]
        order = np.lexsort((times, commit_groups))
        g_sorted = commit_groups[order]
        t_sorted = times[order]
        boundaries = np.flatnonzero(np.diff(g_sorted)) + 1
        starts = np.concatenate(([0], boundaries))
        ends = np.concatenate((boundaries, [g_sorted.size]))
        first[g_sorted[starts]] = t_sorted[starts]
        last[g_sorted[ends - 1]] = t_sorted[ends - 1]
    return {
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": added + removed,
        "commits": commit_counts,
        "files": file_counts,
        "dirs": dirs,
        "first": first,
        "last": last,
    }


# --------------------------------------------------------------------------
# object scoping helpers
# --------------------------------------------------------------------------

def subtree_mask(idx: RepoIndex, sel: Selection, target: tuple[str, int]) -> np.ndarray:
    """Rows belonging to a file or to a directory's whole subtree."""
    kind, oid = target
    if kind == "file":
        return sel.fid == oid
    lo, hi = idx.subtree_file_range(oid)
    return (sel.fid >= lo) & (sel.fid < hi)


def modifications(idx: RepoIndex, sel: Selection, target: tuple[str, int]) -> int:
    """n_H,o -- number of commits in H with a change on object o."""
    if not sel.active.any():
        return 0
    if target[0] == "file":
        rows = sel.active & (sel.fid == target[1])
        return int(np.unique(sel.cid[rows]).size)
    row_pos, dids = idx.expand(sel.fid[sel.active], include_self=False)
    commits = sel.cid[sel.active][row_pos]
    keys = np.unique(dids.astype(np.int64) * idx.n_commits + commits)
    dirs = keys // idx.n_commits
    return int((dirs == target[1]).sum())


# --------------------------------------------------------------------------
# time series
# --------------------------------------------------------------------------

def choose_bucket(span_seconds: int, target_points: int = 100) -> tuple[str, int]:
    for name, size in BUCKETS:
        if span_seconds / size <= target_points:
            return name, size
    return BUCKETS[-1]


def timeline(
    idx: RepoIndex,
    cs: CommitSet,
    sel: Selection,
    *,
    t_from: int | None = None,
    t_to: int | None = None,
) -> dict:
    """l+, l-, lambda and commit counts bucketed over time."""
    times = idx.ct[cs.mask]
    if times.size == 0:
        return {"bucket": "day", "bucket_seconds": DAY, "points": [],
                "start": t_from, "end": t_to}
    start = int(t_from) if t_from is not None else int(times.min())
    end = int(t_to) if t_to is not None else int(times.max()) + 1
    name, size = choose_bucket(max(end - start, 1))
    base = start // size * size
    n_buckets = int((end - base) // size) + 1

    def bucket_of(ts: np.ndarray) -> np.ndarray:
        return np.clip((ts - base) // size, 0, n_buckets - 1).astype(np.int64)

    added = np.bincount(bucket_of(idx.ct[sel.cid]), weights=sel.added,
                        minlength=n_buckets).astype(np.int64)
    removed = np.bincount(bucket_of(idx.ct[sel.cid]), weights=sel.removed,
                          minlength=n_buckets).astype(np.int64)
    commits = np.bincount(bucket_of(times), minlength=n_buckets).astype(np.int64)
    points = []
    cumulative = 0
    for i in range(n_buckets):
        growth = int(added[i] - removed[i])
        cumulative += growth
        points.append([base + i * size, int(added[i]), int(removed[i]),
                       int(added[i] + removed[i]), int(commits[i]), cumulative])
    return {"bucket": name, "bucket_seconds": size, "points": points,
            "start": start, "end": end}


def calendar(idx: RepoIndex, cs: CommitSet, sel: Selection) -> dict:
    """Per-day commit counts and churn (GitHub-style activity calendar)."""
    times = idx.ct[cs.mask]
    if times.size == 0:
        return {"start": 0, "end": 0, "cells": [], "origin_day": 0}
    commit_days = (times // DAY).astype(np.int64)
    row_days = (idx.ct[sel.cid] // DAY).astype(np.int64) if sel.size else None
    day_min = int(commit_days.min())
    day_max = int(commit_days.max())
    if row_days is not None and row_days.size:
        day_min = min(day_min, int(row_days.min()))
        day_max = max(day_max, int(row_days.max()))
    size = day_max - day_min + 1
    commits = np.zeros(size, dtype=np.int64)
    churn = np.zeros(size, dtype=np.int64)
    np.add.at(commits, commit_days - day_min, 1)
    if row_days is not None and row_days.size:
        np.add.at(churn, row_days - day_min, sel.churn)
    cells = np.stack([commits, churn], axis=1)
    return {"start": int(day_min * DAY), "end": int((day_max + 1) * DAY),
            "cells": cells.tolist(), "origin_day": int(day_min)}


# --------------------------------------------------------------------------
# payload builders (used directly by the REST layer)
# --------------------------------------------------------------------------

def metric_block(
    idx: RepoIndex,
    cs: CommitSet,
    sel: Selection,
    dirs: dict[str, np.ndarray] | None = None,
) -> dict:
    """Repository metrics (spec 2.3) plus context for the set H."""
    if dirs is None:
        dirs = dir_aggregates(idx, sel)
    total = cs.count
    active = sel.commits
    return {
        "added": int(dirs["added"][0]),
        "removed": int(dirs["removed"][0]),
        "growth": int(dirs["growth"][0]),
        "churn": int(dirs["churn"][0]),
        "commits": int(cs.count),
        "active_commits": int(active.size),
        "files": int(np.unique(sel.fid[sel.active]).size) if sel.size else 0,
        "dirs": int((dirs["churn"] > 0).sum() - 1) if sel.size else 0,
        "authors": int(np.unique(idx.author_of_cid[active]).size) if active.size else 0,
        "mods": int(dirs.get("mods", np.zeros(1))[0]) if sel.size else 0,
        "churn_rate": _rate(int(dirs["churn"][0]), total),
        "churn_per_commit": _rate(int(dirs["churn"][0]), max(total, 1)),
        "first": int(idx.ct[cs.mask].min()) if cs.count else None,
        "last": int(idx.ct[cs.mask].max()) if cs.count else None,
    }


def dir_tree(
    idx: RepoIndex,
    sel: Selection,
    dirs: dict[str, np.ndarray],
    *,
    max_dirs: int = 1500,
    max_files: int = 2500,
    metric: str = "churn",
) -> dict:
    """Nested directory tree for the treemap visualisation.

    Ancestor-closed selection: a directory's aggregate is always >= its
    descendants', so taking the top-N by the chosen metric keeps every
    included node's parents included.
    """
    churn = dirs[metric]
    candidate = np.flatnonzero(churn > 0)
    candidate = candidate[candidate != 0]
    order = candidate[np.argsort(-churn[candidate])]
    keep_dirs = set(int(d) for d in order[:max_dirs])
    nodes: dict[int, dict] = {}
    for did in keep_dirs:
        nodes[did] = {
            "name": idx.dirs[did].rsplit("/", 1)[-1],
            "path": idx.dirs[did],
            "id": did,
            "type": "dir",
            "value": int(churn[did]),
            "added": int(dirs["added"][did]),
            "removed": int(dirs["removed"][did]),
            "mods": int(dirs["mods"][did]) if "mods" in dirs else 0,
            "children": [],
        }
    # files inside kept directories
    if sel.size:
        active_files = np.unique(sel.fid[sel.active])
        if active_files.size:
            file_churn = np.bincount(sel.fid[sel.active], weights=sel.churn[sel.active],
                                     minlength=idx.n_files)
            file_added = np.bincount(sel.fid, weights=sel.added, minlength=idx.n_files)
            file_removed = np.bincount(sel.fid, weights=sel.removed, minlength=idx.n_files)
            file_mods = np.bincount(sel.fid[sel.active], minlength=idx.n_files)
            ranked = active_files[np.argsort(-file_churn[active_files])][:max_files]
            for fid in ranked:
                did = int(idx.file_did[fid])
                if did not in keep_dirs:
                    continue
                parent = nodes.get(did)
                if parent is None:
                    continue
                parent["children"].append({
                    "name": idx.files[fid].rsplit("/", 1)[-1],
                    "path": idx.files[fid],
                    "id": int(fid),
                    "type": "file",
                    "value": int(file_churn[fid]),
                    "added": int(file_added[fid]),
                    "removed": int(file_removed[fid]),
                    "mods": int(file_mods[fid]),
                    "children": [],
                })
    # attach directories to their nearest kept ancestor
    roots: list[dict] = []
    for did in sorted(keep_dirs):
        node = nodes[did]
        parent = int(idx.parent_did[did])
        while parent != did and parent not in keep_dirs and parent != 0:
            parent = int(idx.parent_did[parent])
        if parent in keep_dirs and parent != did:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    def sort_rec(children: list[dict]) -> None:
        children.sort(key=lambda c: -c["value"])
        for child in children:
            sort_rec(child["children"])

    sort_rec(roots)
    root = {
        "name": idx.meta.get("name") or "repository",
        "path": "",
        "id": 0,
        "type": "dir",
        "value": int(churn[0]),
        "added": int(dirs["added"][0]),
        "removed": int(dirs["removed"][0]),
        "mods": int(dirs["mods"][0]) if "mods" in dirs else 0,
        "children": roots,
    }
    return root


def children_payload(
    idx: RepoIndex,
    sel: Selection,
    did: int,
    *,
    total_commits: int,
    owners: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """Immediate children of ``did`` with their own metrics (spec 2.2).

    A directory's metrics are the sums over its *immediate* children, which
    is exactly what the file tree shows: every row in the subtree is mapped
    to the child of ``did`` on its path, then reduced per child.
    """
    child_ids = _child_ids(idx, did)
    n_children = len(child_ids)
    slot = np.full(idx.n_dirs + idx.n_files, -1, dtype=np.int64)
    slot[child_ids] = np.arange(n_children)
    added = np.zeros(n_children, dtype=np.int64)
    removed = np.zeros(n_children, dtype=np.int64)
    mods = np.zeros(n_children, dtype=np.int64)
    binary = np.zeros(n_children, dtype=np.int64)
    top_owner = np.full(n_children, -1, dtype=np.int64)
    owner_share = np.zeros(n_children, dtype=np.float64)

    lo, hi = idx.subtree_file_range(did)
    subtree = (sel.fid >= lo) & (sel.fid < hi)
    if subtree.any():
        keys = slot[idx.child_of_dir(sel.fid[subtree], did)]
        added = np.bincount(keys, weights=sel.added[subtree],
                            minlength=n_children).astype(np.int64)
        removed = np.bincount(keys, weights=sel.removed[subtree],
                              minlength=n_children).astype(np.int64)
        bin_rows = subtree & ((sel.flags & FLAG_BINARY) > 0)
        if bin_rows.any():
            binary = np.bincount(slot[idx.child_of_dir(sel.fid[bin_rows], did)],
                                 minlength=n_children).astype(np.int64)
        active = subtree & sel.active
        if active.any():
            keys_a = slot[idx.child_of_dir(sel.fid[active], did)]
            cids_a = sel.cid[active]
            pairs = np.unique(keys_a * idx.n_commits + cids_a)
            mods = np.bincount(pairs // idx.n_commits,
                               minlength=n_children).astype(np.int64)
            if owners:
                groups = idx.author_of_cid[cids_a]
                pair_owner = keys_a * idx.group_count + groups
                by_owner = np.bincount(
                    pair_owner, weights=sel.churn[active].astype(np.float64),
                    minlength=n_children * idx.group_count
                ).reshape(n_children, idx.group_count)
                totals = by_owner.sum(axis=1)
                best = by_owner.max(axis=1)
                top_owner = by_owner.argmax(axis=1)
                owner_share = np.divide(best, totals,
                                        out=np.zeros(n_children, dtype=np.float64),
                                        where=totals > 0)

    items: list[dict] = []
    for i, child in enumerate(child_ids):
        is_dir = child < idx.n_dirs
        churn = int(added[i] + removed[i])
        path = idx.dirs[child] if is_dir else idx.files[child - idx.n_dirs]
        entry = {
            "id": int(child if is_dir else child - idx.n_dirs),
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "type": "dir" if is_dir else "file",
            "added": int(added[i]),
            "removed": int(removed[i]),
            "growth": int(added[i] - removed[i]),
            "churn": churn,
            "mods": int(mods[i]),
            "mod_freq": _rate(int(mods[i]), total_commits),
            "churn_rate": _rate(churn, total_commits),
            "owner": _owner_entry(idx, int(top_owner[i]), float(owner_share[i])),
        }
        if is_dir:
            flo, fhi = idx.subtree_file_range(child)
            dlo, dhi = idx.subtree_dir_range(child)
            entry["files"] = int(fhi - flo)
            entry["subdirs"] = int(dhi - dlo)
        else:
            fid = child - idx.n_dirs
            entry["present"] = bool(idx.present[fid])
            entry["submodule"] = bool(idx.file_kind[fid] == 1)
            entry["binary_changes"] = int(binary[i])
            entry["size"] = int(idx.sizes[fid])
        items.append(entry)
    items.sort(key=lambda e: (-e["churn"], e["path"]))
    if limit:
        items = items[:limit]
    return items


def _child_ids(idx: RepoIndex, did: int) -> np.ndarray:
    subs = idx.child_ids[idx.child_start[did]:idx.child_start[did + 1]]
    lo, hi = idx.subtree_file_range(did)
    fids = np.flatnonzero(idx.file_did[lo:hi] == did) + lo
    return np.concatenate([subs.astype(np.int64), (idx.n_dirs + fids).astype(np.int64)])


def _owner_entry(idx: RepoIndex, group: int, share: float) -> dict | None:
    if group is None or group < 0 or group >= idx.group_count:
        return None
    meta = idx.group_meta[group]
    return {"id": int(group), "label": meta["label"], "share": round(float(share), 6)}


def object_ownership(
    idx: RepoIndex,
    sel: Selection,
    target: tuple[str, int],
) -> list[dict]:
    """omega_H,o,a -- churn share of every author that touched object o."""
    mask = subtree_mask(idx, sel, target)
    if not mask.any():
        return []
    groups = idx.author_of_cid[sel.cid[mask]]
    churn = sel.churn[mask].astype(np.float64)
    total = churn.sum()
    by_group = np.bincount(groups, weights=churn, minlength=idx.group_count)
    active = mask & sel.active
    mods = np.zeros(idx.group_count, dtype=np.int64)
    if active.any():
        pairs = np.unique(idx.author_of_cid[sel.cid[active]] * idx.n_commits
                          + sel.cid[active])
        mods = np.bincount(pairs // idx.n_commits, minlength=idx.group_count)
    out = []
    for gid in np.flatnonzero(by_group > 0):
        meta = idx.group_meta[int(gid)]
        out.append({
            "id": int(gid),
            "label": meta["label"],
            "emails": meta["emails"],
            "merged": meta["merged"],
            "churn": int(by_group[gid]),
            "mods": int(mods[gid]),
            "share": round(float(by_group[gid] / total), 6) if total else 0.0,
        })
    out.sort(key=lambda e: -e["churn"])
    return out


def object_stats(
    idx: RepoIndex,
    cs: CommitSet,
    sel: Selection,
    target: tuple[str, int],
    *,
    with_series: bool = True,
) -> dict:
    """Everything the object detail panel shows for one file or directory."""
    kind, oid = target
    mask = subtree_mask(idx, sel, target)
    scope = Selection(idx, np.where(mask, sel.mask, False), cs)
    added = int(scope.added.sum())
    removed = int(scope.removed.sum())
    churn = added + removed
    n_mods = modifications(idx, sel, target)
    hops = cs.count
    stats = {
        "added": added,
        "removed": removed,
        "growth": added - removed,
        "churn": churn,
        "mods": n_mods,
        "mod_freq": _rate(n_mods, hops),
        "churn_rate": _rate(churn, hops),
        "commit_count": int(cs.count),
        "touching_commits": int(np.unique(sel.cid[mask]).size) if mask.any() else 0,
        "first": int(idx.ct[scope.cid].min()) if scope.size else None,
        "last": int(idx.ct[scope.cid].max()) if scope.size else None,
        "ownership": object_ownership(idx, sel, target),
        "renames": [],
        "authors": int(np.unique(idx.author_of_cid[scope.cid]).size) if scope.size else 0,
    }
    if kind == "file":
        name = idx.files[oid]
        stats.update({
            "path": name,
            "name": name.rsplit("/", 1)[-1],
            "dir": idx.dirs[int(idx.file_did[oid])],
            "present": bool(idx.present[oid]),
            "size": int(idx.sizes[oid]),
            "submodule": bool(idx.file_kind[oid] == 1),
            "binary_changes": int((scope.flags & FLAG_BINARY).sum()),
            "renames": _rename_history(idx, scope, oid),
            "delete_commits": int((scope.removed > 0).sum()),
        })
    else:
        lo, hi = idx.subtree_file_range(oid)
        stats.update({
            "path": idx.dirs[oid],
            "name": idx.dirs[oid].rsplit("/", 1)[-1] if oid else "/",
            "files": int(hi - lo),
            "subdirs": int(idx.subtree_dir_range(oid)[1] - idx.subtree_dir_range(oid)[0]),
            "dirs_touched": int(_touched_dirs(idx, scope, oid)),
        })
    if with_series:
        stats["series"] = _object_series(idx, scope)
    return stats


def _rename_history(idx: RepoIndex, scope: Selection, fid: int) -> list[dict]:
    flags = scope.flags
    picked = np.flatnonzero(flags & FLAG_RENAME)
    out = []
    for row in picked:
        old = int(idx.ch_oldfid[scope.rows[row]])
        if old < 0:
            continue
        cid = int(scope.cid[row])
        out.append({
            "commit": idx.hashes[cid],
            "ct": int(idx.ct[cid]),
            "old_path": idx.files[old],
            "subject": idx.subjects[cid],
        })
    out.sort(key=lambda r: r["ct"])
    return out


def _touched_dirs(idx: RepoIndex, scope: Selection, did: int) -> int:
    if not scope.active.any():
        return 0
    row_pos, dids = idx.expand(scope.fid[scope.active], include_self=False)
    if dids.size == 0:
        return 0
    commits = scope.cid[scope.active][row_pos]
    keys = dids.astype(np.int64) * idx.n_commits + commits
    unique = np.unique(keys)
    dirs = unique // idx.n_commits
    lo, hi = idx.subtree_dir_range(did)
    return int(((dirs >= lo) & (dirs < hi)).sum() if did else np.unique(dirs).size)


def _object_series(idx: RepoIndex, scope: Selection) -> list[dict]:
    """Per-commit history of an object (capped), for sparklines/detail charts."""
    if scope.size == 0:
        return []
    order = np.argsort(scope.cid, kind="stable")
    cids = scope.cid[order]
    added = np.bincount(cids, weights=scope.added[order]).astype(np.int64)
    removed = np.bincount(cids, weights=scope.removed[order]).astype(np.int64)
    present = np.unique(cids)
    times = idx.ct[present]
    points = [[int(times[i]), int(added[present[i]]), int(removed[present[i]])]
              for i in range(present.size)]
    points.sort(key=lambda p: p[0])
    if len(points) > 4000:
        # keep the shape but bound the payload
        stride = math.ceil(len(points) / 4000)
        reduced: list[list[int]] = []
        for i in range(0, len(points), stride):
            chunk = points[i:i + stride]
            reduced.append([chunk[0][0], sum(p[1] for p in chunk),
                            sum(p[2] for p in chunk)])
        points = reduced
    return points


def commit_row(idx: RepoIndex, cid: int) -> dict:
    return {
        "hash": idx.hashes[cid],
        "short": idx.hashes[cid][:10],
        "ct": int(idx.ct[cid]),
        "at": int(idx.at[cid]),
        "subject": idx.subjects[cid],
        "author": _author_of_commit(idx, cid),
        "merge": bool(idx.merge[cid]),
        "diffed": bool(idx.diffed[cid]),
        "files": int(idx.c_files[cid]),
        "binary": int(idx.c_binary[cid]),
        "added": int(idx.c_added[cid]),
        "removed": int(idx.c_removed[cid]),
        "churn": int(idx.c_added[cid] + idx.c_removed[cid]),
        "growth": int(idx.c_added[cid] - idx.c_removed[cid]),
    }


def _author_of_commit(idx: RepoIndex, cid: int) -> dict:
    default = idx.group_meta[int(idx.author_of_cid[cid])]
    raw = idx.rawids[int(idx.rid[cid])]
    return {
        "id": int(idx.author_of_cid[cid]),
        "label": default["label"],
        "email": (default["emails"][0] if default["emails"] else ""),
        "merged": default["merged"],
        "raw_name": raw["name"],
        "raw_email": raw["email"],
    }


def commit_detail(idx: RepoIndex, cid: int) -> dict:
    """Per-commit file metrics (spec 2.1) for the commit inspector."""
    row = commit_row(idx, cid)
    lo, hi = idx.commit_slice(cid)
    svc = slice(lo, hi)
    files = []
    for k in range(lo, hi):
        fid = int(idx.ch_fid[k])
        flags = int(idx.ch_flags[k])
        old = int(idx.ch_oldfid[k])
        added = int(idx.ch_added[k])
        removed = int(idx.ch_removed[k])
        files.append({
            "path": idx.files[fid],
            "name": idx.files[fid].rsplit("/", 1)[-1],
            "dir": idx.dirs[int(idx.file_did[fid])],
            "added": added,
            "removed": removed,
            "growth": added - removed,
            "churn": added + removed,
            "rename": bool(flags & FLAG_RENAME),
            "old_path": idx.files[old] if old >= 0 else None,
            "binary": bool(flags & FLAG_BINARY),
            "submodule": bool(flags & FLAG_SUBMODULE),
        })
    files.sort(key=lambda f: (-f["churn"], f["path"]))
    row["changes"] = files
    row["rows"] = int(hi - lo)
    if row["merge"]:
        row["note"] = ("merge commit: excluded from H-bar by the spec, so it "
                       "carries no diff of its own; its parents are still "
                       "traversed when resolving reachability")
    elif not row["diffed"]:
        row["note"] = ("this commit was not part of the analysed history "
                       "(it is reachable only from another reference) -- "
                       "re-analyse the repository from that reference to "
                       "measure it")
    return row


def _rate(value: int, total: int) -> float:
    if not total:
        return 0.0
    return round(value / total, 6)
