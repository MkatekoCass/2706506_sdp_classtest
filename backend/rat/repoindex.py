"""In-memory columnar projection of a repository index.

:class:`RepoIndex` loads the ``.npy`` columns written by
:mod:`rat.analysis` (memory-mapped, so opening a large repository is cheap)
together with the string tables, and exposes:

* **commit sets** -- :math:`\\bar H` (non-merge commits reachable from a
  reference), :math:`H_t` (from timestamp *t*), :math:`H_{i,j}` (timestamp
  window) and arbitrary manually selected subsets, exactly as the spec
  defines them;
* **row selections** -- the boolean mask over the change rows that a commit
  set *and* an author filter produce; every metric in :mod:`rat.metrics` is
  a vectorised reduction over such a mask;
* **tree helpers** -- file/directory lookup, subtree ranges (a directory's
  files form a contiguous range because ids are assigned in lexicographic
  order) and ancestor chains.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

#: sentinel for "no file" in id columns
NO_FID = -1


def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class CommitSet:
    """A resolved commit set (:math:`H`) with provenance for the UI."""

    mask: np.ndarray                  #: bool over commits
    mode: str                         #: all | range | list
    ref: str                          #: reference the set was derived from
    label: str
    t_from: int | None = None
    t_to: int | None = None
    requested: int = 0                #: commits requested before reachability
    dropped: int = 0                  #: requested commits outside the reference
    unparsed: int = 0                 #: reachable but not analysed (graph only)
    features: dict = field(default_factory=dict)

    @property
    def cids(self) -> np.ndarray:
        return np.flatnonzero(self.mask)

    @property
    def count(self) -> int:
        return int(self.mask.sum())

    @property
    def signature(self) -> str:
        return f"{self.mode}|{self.ref}|{self.t_from}|{self.t_to}|{self.count}"


class RepoIndex:
    """Columnar view over one analysed repository."""

    def __init__(self, index_dir: str):
        self.index_dir = index_dir
        load = lambda name: np.load(os.path.join(index_dir, name))  # noqa: E731
        self.hashes: list[str] = _load_json(os.path.join(index_dir, "hashes.json"))
        self.subjects: list[str] = _load_json(os.path.join(index_dir, "subjects.json"))
        self.files: list[str] = _load_json(os.path.join(index_dir, "files.json"))
        self.dirs: list[str] = _load_json(os.path.join(index_dir, "dirs.json"))
        self.refs: dict[str, str] = _load_json(os.path.join(index_dir, "refs.json"))
        self.identities: list[dict] = _load_json(os.path.join(index_dir, "identities.json"))
        self.rawids: list[dict] = _load_json(os.path.join(index_dir, "rawids.json"))
        self.meta: dict = _load_json(os.path.join(index_dir, "meta.json"))

        self.ct = load("ct.npy")
        self.at = load("at.npy")
        self.merge = load("merge.npy")
        self.diffed = load("diffed.npy")
        self.iid = load("iid.npy")
        self.rid = load("rid.npy")
        self.p_start = load("p_start.npy")
        self.p_ids = load("p_ids.npy")
        self.c_added = load("c_added.npy")
        self.c_removed = load("c_removed.npy")
        self.c_files = load("c_files.npy")
        self.c_binary = load("c_binary.npy")
        self.ch_cid = load("ch_cid.npy")
        self.ch_fid = load("ch_fid.npy")
        self.ch_oldfid = load("ch_oldfid.npy")
        self.ch_added = load("ch_added.npy")
        self.ch_removed = load("ch_removed.npy")
        self.ch_flags = load("ch_flags.npy")
        self.file_did = load("file_did.npy")
        self.file_depth = load("file_depth.npy")
        self.parent_did = load("parent_did.npy")
        self.dir_depth = load("dir_depth.npy")
        self.chain_start = load("chain_start.npy")
        self.chain_ids = load("chain_ids.npy")
        self.child_start = load("child_start.npy")
        self.child_ids = load("child_ids.npy")
        self.present = load("present.npy")
        self.file_kind = load("file_kind.npy")
        self.sizes = load("sizes.npy")

        self.n_commits = len(self.hashes)
        self.n_changes = int(self.ch_cid.size)
        self.n_files = len(self.files)
        self.n_dirs = len(self.dirs)
        self.hash_to_cid = {h: i for i, h in enumerate(self.hashes)}
        self.fid_of = {p: i for i, p in enumerate(self.files)}
        self.did_of = {p: i for i, p in enumerate(self.dirs)}

        # subtree ranges: ids are lexicographic, so a directory's files are
        # a contiguous id range [lo, hi)
        self.dir_file_lo = np.zeros(self.n_dirs, dtype=np.int64)
        self.dir_file_hi = np.zeros(self.n_dirs, dtype=np.int64)
        self.dir_dir_lo = np.zeros(self.n_dirs, dtype=np.int64)
        self.dir_dir_hi = np.zeros(self.n_dirs, dtype=np.int64)
        for did, path in enumerate(self.dirs):
            if did == 0:
                self.dir_file_lo[did], self.dir_file_hi[did] = 0, self.n_files
                self.dir_dir_lo[did], self.dir_dir_hi[did] = 1, self.n_dirs
                continue
            prefix = path + "/"
            upper = path + "0"
            self.dir_file_lo[did] = bisect.bisect_left(self.files, prefix)
            self.dir_file_hi[did] = bisect.bisect_left(self.files, upper)
            self.dir_dir_lo[did] = bisect.bisect_left(self.dirs, prefix)
            self.dir_dir_hi[did] = bisect.bisect_left(self.dirs, upper)

        self.lock = threading.RLock()
        self._reach_cache: dict[str, np.ndarray] = {}
        self._author_state: dict | None = None
        self.merge_groups: list[dict] = []
        self.set_merge_groups([])
        self._total_churn_rows: np.ndarray | None = None
        self._time_order: np.ndarray | None = None

    # ------------------------------------------------------------------
    # authors
    # ------------------------------------------------------------------
    def set_merge_groups(self, groups: Sequence[dict]) -> None:
        """Install manual author-merge groups.

        Mailmap resolution already happened during analysis (git's own
        ``%aN/%aE``); this layer merges distinct *identities* into one author
        on the user's request.  ``groups`` is a list of
        ``{"label": str, "members": [[name, email], ...]}``; members that do
        not exist in the index are ignored (re-analysis friendly).
        """
        by_key = {(ident["name"], ident["email"]): i
                  for i, ident in enumerate(self.identities)}
        member_to_group: dict[int, int] = {}
        resolved: list[dict] = []
        for group in groups:
            members = [by_key[tuple(m)] for m in group.get("members", [])
                       if tuple(m) in by_key]
            if len(members) < 1:
                continue
            gid = len(resolved) + self.n_identities_base
            for iid in members:
                member_to_group[iid] = gid
            resolved.append({
                "id": gid,
                "label": group.get("label") or self.identities[members[0]]["name"],
                "members": members,
            })
        self.merge_groups = resolved

        group_of_iid = np.arange(len(self.identities), dtype=np.int64)
        for iid, gid in member_to_group.items():
            group_of_iid[iid] = gid
        # dense re-index
        unique_groups = np.unique(group_of_iid)
        dense = np.searchsorted(unique_groups, group_of_iid).astype(np.int32)
        self.identity_group = dense
        self.group_count = int(unique_groups.size)
        # per-group metadata
        self.group_meta: list[dict] = []
        for g in range(self.group_count):
            members = np.flatnonzero(dense == g)
            merged = next((mg for mg in resolved
                           if mg["id"] == unique_groups[g]), None)
            names = [self.identities[m]["name"] for m in members]
            emails = [self.identities[m]["email"] for m in members]
            self.group_meta.append({
                "id": g,
                "label": merged["label"] if merged else (names[0] if names else "?"),
                "name": names[0] if names else "?",
                "emails": emails,
                "members": [{"name": self.identities[m]["name"],
                             "email": self.identities[m]["email"],
                             "commits": self.identities[m]["commits"]}
                            for m in members],
                "merged": bool(merged) or len(members) > 1,
            })
        self.author_of_cid = dense[self.iid]
        self._author_state = None

    # ------------------------------------------------------------------
    # commit sets
    # ------------------------------------------------------------------
    @property
    def n_identities_base(self) -> int:
        return len(self.identities)

    def reachable(self, ref: str = "HEAD") -> np.ndarray:
        """H-bar: bool mask of every commit reachable from ``ref``."""
        target = self.refs.get(ref, ref)
        cid = self.hash_to_cid.get(target, self.hash_to_cid.get(ref))
        with self.lock:
            if ref in self._reach_cache:
                return self._reach_cache[ref]
        mask = np.zeros(self.n_commits, dtype=bool)
        if cid is None:
            with self.lock:
                self._reach_cache[ref] = mask
            return mask
        stack = [cid]
        mask[cid] = True
        p_start, p_ids = self.p_start, self.p_ids
        while stack:
            current = stack.pop()
            for k in range(p_start[current], p_start[current + 1]):
                parent = int(p_ids[k])
                if not mask[parent]:
                    mask[parent] = True
                    stack.append(parent)
        with self.lock:
            self._reach_cache[ref] = mask
        return mask

    def non_merge(self) -> np.ndarray:
        return self.merge == 0

    def resolve_commitset(
        self,
        mode: str = "all",
        ref: str = "HEAD",
        t_from: int | None = None,
        t_to: int | None = None,
        commits: Iterable[str] | None = None,
    ) -> CommitSet:
        """Build a :class:`CommitSet` from the filter parameters."""
        reach = self.reachable(ref)
        base = reach & self.non_merge()
        unparsed = int((reach & self.non_merge() & (self.diffed == 0)).sum())
        if mode == "range":
            mask = base.copy()
            if t_from is not None:
                mask &= self.ct >= int(t_from)
            if t_to is not None:
                mask &= self.ct < int(t_to)
            label = _range_label(t_from, t_to)
            result = CommitSet(mask=mask, mode=mode, ref=ref, label=label,
                               t_from=t_from, t_to=t_to, unparsed=unparsed)
        elif mode == "list":
            wanted = list(commits or [])
            mask = np.zeros(self.n_commits, dtype=bool)
            dropped = 0
            for token in wanted:
                token = token.strip()
                if not token:
                    continue
                cid = self.hash_to_cid.get(token)
                if cid is None:
                    # allow abbreviated hashes
                    matches = [h for h in self.hashes if h.startswith(token)]
                    if len(matches) == 1:
                        cid = self.hash_to_cid[matches[0]]
                if cid is None or not base[cid]:
                    dropped += 1
                    continue
                mask[cid] = True
            result = CommitSet(mask=mask, mode=mode, ref=ref,
                               label=f"{int(mask.sum())} selected commits",
                               requested=len(wanted), dropped=dropped,
                               unparsed=unparsed)
        else:
            result = CommitSet(mask=base, mode="all", ref=ref,
                               label=f"all history of {ref}", unparsed=unparsed)
        result.features = {
            "unparsed": result.unparsed,
            "partial": result.unparsed > 0,
        }
        return result

    def commit_times(self, cs: CommitSet) -> np.ndarray:
        return self.ct[cs.mask]

    def time_order(self) -> np.ndarray:
        """Commit ids sorted by committer time, newest first (cached).

        Used by the paged commit browser so that re-requesting a page never
        re-sorts 100k commits.
        """
        if self._time_order is None:
            self._time_order = np.argsort(-self.ct, kind="stable")
        return self._time_order

    # ------------------------------------------------------------------
    # row selections
    # ------------------------------------------------------------------
    def rows_for(
        self,
        cs: CommitSet,
        authors: np.ndarray | None = None,
    ) -> np.ndarray:
        """Boolean mask over the change rows of commit set ``cs``.

        ``authors`` is an optional bool mask over author *groups*; when given,
        only rows whose commit belongs to a selected author survive.
        """
        if self.n_changes == 0:
            return np.zeros(0, dtype=bool)
        sel = cs.mask[self.ch_cid]
        if authors is not None:
            sel &= authors[self.author_of_cid[self.ch_cid]]
        return sel

    def author_mask(self, group_ids: Sequence[int] | None) -> np.ndarray | None:
        if not group_ids:
            return None
        mask = np.zeros(self.group_count, dtype=bool)
        for gid in group_ids:
            if 0 <= int(gid) < self.group_count:
                mask[int(gid)] = True
        return mask

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def commit_slice(self, cid: int) -> tuple[int, int]:
        """Half-open row range of ``cid`` in the change arrays (rows are sorted)."""
        lo = int(np.searchsorted(self.ch_cid, cid, side="left"))
        hi = int(np.searchsorted(self.ch_cid, cid, side="right"))
        return lo, hi

    def ancestors_of_fid(self, fid: int) -> np.ndarray:
        """Directory ids on the path of file ``fid`` (deepest first, root last)."""
        lo, hi = int(self.chain_start[fid]), int(self.chain_start[fid + 1])
        return self.chain_ids[lo + 1:hi]

    def expand(self, fids: np.ndarray, *, include_self: bool = False) -> tuple[np.ndarray, np.ndarray]:
        """Expand an array of file ids into (row positions, object ids).

        Every file is repeated once per entry of its ancestor chain, so a
        reduction over the returned object ids (``np.bincount``) yields the
        subtree aggregation that the directory metrics require: a row is
        added to its own file and to *every* ancestor directory.

        With ``include_self=True`` the chain starts with the file itself
        (encoded as ``n_dirs + fid``), which is what the per-child grouping
        of a directory listing needs; otherwise it starts at the file's
        parent directory.
        """
        starts = self.chain_start[fids] + (0 if include_self else 1)
        ends = self.chain_start[fids + 1]
        lengths = (ends - starts).astype(np.int64)
        total = int(lengths.sum())
        rows = np.repeat(np.arange(fids.size, dtype=np.int64), lengths)
        offsets = np.arange(total, dtype=np.int64) - np.repeat(
            np.cumsum(lengths) - lengths, lengths)
        positions = np.repeat(starts, lengths) + offsets
        return rows, self.chain_ids[positions]

    def encoded_chain(self, fids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Like :meth:`expand` with ``include_self=True``."""
        return self.expand(fids, include_self=True)

    def child_of_dir(self, fids: np.ndarray, did: int) -> np.ndarray:
        """The immediate child of directory ``did`` for each file id.

        ``did`` must be an ancestor of every file (callers restrict by
        subtree first).  Returns encoded object ids (dirs as ``did``, files
        as ``n_dirs + fid``).
        """
        base_depth = int(self.dir_depth[did])
        index = self.chain_start[fids] + (self.file_depth[fids] - base_depth)
        return self.chain_ids[index]

    def subtree_file_range(self, did: int) -> tuple[int, int]:
        return int(self.dir_file_lo[did]), int(self.dir_file_hi[did])

    def subtree_dir_range(self, did: int) -> tuple[int, int]:
        return int(self.dir_dir_lo[did]), int(self.dir_dir_hi[did])

    def dir_is_descendant(self, did: int, ancestor: int) -> bool:
        lo, hi = self.subtree_dir_range(ancestor)
        return lo <= did < hi

    def file_url_path(self, fid: int) -> str:
        return self.files[fid]

    def dir_url_path(self, did: int) -> str:
        return self.dirs[did]

    def total_churn_per_file(self) -> np.ndarray:
        """Total churn per file over the whole analysed history (cached)."""
        if self._total_churn_rows is None:
            self._total_churn_rows = (
                np.bincount(self.ch_fid, weights=self.ch_added + self.ch_removed,
                            minlength=self.n_files).astype(np.int64))
        return self._total_churn_rows


def _range_label(t_from: int | None, t_to: int | None) -> str:
    import datetime as _dt

    def fmt(ts: int) -> str:
        return _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")

    if t_from is not None and t_to is not None:
        return f"{fmt(t_from)} .. {fmt(t_to - 1)}"
    if t_from is not None:
        return f"from {fmt(t_from)}"
    if t_to is not None:
        return f"until {fmt(t_to - 1)}"
    return "all history"
