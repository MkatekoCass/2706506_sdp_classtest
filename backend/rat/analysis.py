"""Repository extraction pipeline.

Turns a git repository into a compact, columnar, memory-mappable index that
the metric engine (:mod:`rat.metrics`) can aggregate in milliseconds.

Pipeline
--------
1. **Diff pass** -- ``git log <ref> --numstat -z -M50%`` extracts, for every
   commit reachable from the analysis reference, the added/removed line
   counts of every changed object.  Those five flags are chosen to match the
   spec exactly:

   ``-M50%``
       rename detection with a 50% similarity threshold (spec 2: "Rename
       detection is enabled with a threshold of 50%");
   ``--no-ext-diff / --no-textconv``
       keep git's raw view of the blobs so that git's own binary detection
       ("Git provides a definition and detection of binary files") is what
       decides whether a file is measured;
   ``--diff-merges=off``
       merge commits belong to :math:`\\bar H` as *nodes* but contribute no
       diff of their own (they are excluded from the commit set).

2. **Graph pass** -- ``git log --all`` collects parents/refs for the whole
   object graph (including merge commits), which lets the engine resolve
   "commit set reachable from h_r" for *any* reference, not just the one the
   repository was analysed from.

3. **Normalisation** -- paths become integer ids (``fid``/``did``); the tree
   is materialised as parent/child indexes and per-file ancestor chains so
   that every aggregation can be expressed as a vectorised bin-count.

4. **Persistence** -- numeric columns as ``.npy`` (loaded later with
   ``mmap_mode='r'``, so opening a 100k-commit repository is a few hundred
   milliseconds and costs almost no RSS until pages are touched), strings as
   JSON.

All identifiers are stable: ids are assigned by sorting the path sets, and
array rows are ordered by internal commit id, which makes binary search over
the change rows possible.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Callable

import numpy as np

from . import gitcmd
from .gitcmd import LOG_FORMAT
from .logparse import CommitStat, iter_commits, iter_fragments

#: Row flags stored in ``ch_flags.npy``.
FLAG_RENAME = 1
FLAG_BINARY = 2
FLAG_SUBMODULE = 4

#: Rows carrying these flags are never measured (spec: "Binary files are not
#: measured"; gitlinks are not files but submodule pointers).
FLAG_UNMEASURED = FLAG_BINARY | FLAG_SUBMODULE

ProgressFn = Callable[[str, int, int, str], None]

#: log commands are executed with these config overrides so that a user's
#: global git configuration cannot change the measurement (signatures would
#: corrupt the machine readable stream, external diff drivers would replace
#: git's own stats).
_LOG_CONFIG = [
    "-c", "log.showSignature=false",
    "-c", "diff.renames=true",
    "-c", "core.quotePath=false",
]

_DIFF_FLAGS = [
    "--numstat", "-z", "-M50%",
    "--diff-merges=off", "--no-ext-diff", "--no-textconv",
]


def _noop(phase: str, done: int, total: int, message: str) -> None:
    pass


class AnalysisError(RuntimeError):
    pass


def analyze(
    gitdir: str,
    index_dir: str,
    analysis_ref: str = "HEAD",
    *,
    progress: ProgressFn | None = None,
) -> dict:
    """Extract ``gitdir`` into ``index_dir`` and return summary statistics."""
    report = progress or _noop
    started = time.time()

    ref_hash = gitcmd.rev_parse(gitdir, analysis_ref)
    if ref_hash is None:
        raise AnalysisError(f"reference '{analysis_ref}' does not resolve to a commit")
    total = gitcmd.rev_list_count(gitdir, analysis_ref)
    report("diff", 0, max(total, 1), f"reading line statistics of {total} commits")

    commits, changes = _read_diff_log(gitdir, analysis_ref, total, report)
    report("graph", 0, 1, "reading commit graph and refs")
    _read_graph(gitdir, commits, report)
    refs = _collect_refs(gitdir)

    report("normalise", 0, 1, "building file/directory tables")
    tables = _build_tables(commits, changes, gitdir, analysis_ref)
    report("write", 0, 1, "writing index")
    stats = _write_index(index_dir, commits, changes, tables, refs, {
        "analysis_ref": analysis_ref,
        "analysis_ref_hash": ref_hash,
        "gitdir": os.path.abspath(gitdir),
        "generated_at": int(time.time()),
    })
    report("done", 1, 1, "index written")
    stats["duration"] = round(time.time() - started, 2)
    return stats


# --------------------------------------------------------------------------
# pass 1: diff statistics
# --------------------------------------------------------------------------

def _read_diff_log(
    gitdir: str,
    ref: str,
    total: int,
    report: ProgressFn,
) -> tuple[dict, list]:
    """Run the ``--numstat`` pass and return (commits, change rows).

    ``commits`` maps hash -> metadata dict (insertion ordered: the internal
    commit id ``cid`` is the position in this dict).
    """
    args = [*_LOG_CONFIG, "log", ref, *_DIFF_FLAGS,
            "--pretty=format:" + LOG_FORMAT]
    proc = gitcmd.open_stream(gitdir, args)
    commits: dict[str, dict] = {}
    changes: list[tuple] = []
    step = max(total // 100, 500)
    seen = 0
    try:
        for stat in iter_commits(proc.stdout, chunk_size=1 << 22):
            _add_commit(commits, stat)
            cid = len(commits) - 1
            for f in stat.files:
                changes.append((
                    cid,
                    f.path,
                    f.old_path,
                    f.added if not f.binary else 0,
                    f.removed if not f.binary else 0,
                    (FLAG_RENAME if f.is_rename else 0) | (FLAG_BINARY if f.binary else 0),
                ))
            seen += 1
            if seen % step == 0:
                report("diff", seen, max(total, 1),
                       f"reading line statistics ({seen}/{total} commits)")
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        code = proc.wait()
    if code != 0:
        raise AnalysisError(
            "git log failed: " + stderr.decode("utf-8", "replace").strip()[:400]
        )
    if not commits:
        raise AnalysisError("the repository has no commits on the analysis reference")
    report("diff", seen, max(total, 1), f"read {seen} commits")
    return commits, changes


def _add_commit(commits: dict, stat: CommitStat) -> None:
    if stat.hash in commits:
        return
    commits[stat.hash] = {
        "hash": stat.hash,
        "ct": stat.ct,
        "at": stat.at,
        "parents": list(stat.parents),
        "merge": 1 if stat.is_merge else 0,
        "diffed": 1,
        "mn": stat.mname,
        "me": stat.memail,
        "rn": stat.rname,
        "re": stat.remail,
        "subject": stat.subject,
    }


# --------------------------------------------------------------------------
# pass 2: commit graph (all refs, merges included)
# --------------------------------------------------------------------------

def _read_graph(gitdir: str, commits: dict, report: ProgressFn) -> None:
    """Add commits that exist on other refs but not on the analysed one.

    They carry metadata and parent edges (so reachability can be evaluated
    for any reference) but no diff rows, which the API surfaces as a
    "re-analyse to include N commits" warning rather than silently wrong
    numbers.
    """
    args = [*_LOG_CONFIG, "log", "--all", "--pretty=format:" + LOG_FORMAT]
    proc = gitcmd.open_stream(gitdir, args)
    added = 0
    try:
        for fragment in iter_fragments(proc.stdout, chunk_size=1 << 22):
            head, _ = _split_graph_fragment(fragment)
            parsed = _parse_graph_header(head)
            if parsed is None:
                continue
            commit_hash = parsed[0]
            if commit_hash in commits:
                continue
            commits[commit_hash] = {
                "hash": commit_hash,
                "ct": parsed[1],
                "at": parsed[2],
                "parents": list(parsed[3]),
                "merge": 1 if len(parsed[3]) > 1 else 0,
                "diffed": 0,
                "mn": parsed[4],
                "me": parsed[5],
                "rn": parsed[6],
                "re": parsed[7],
                "subject": parsed[8],
            }
            added += 1
    finally:
        proc.stdout.close()
        code = proc.wait()
    if code != 0:
        raise AnalysisError("git log --all failed while reading the commit graph")
    report("graph", 1, 1, f"commit graph complete ({added} additional commits)")


def _split_graph_fragment(fragment: bytes) -> tuple[bytes, bytes]:
    newline = fragment.find(b"\n")
    if newline < 0:
        return fragment, b""
    return fragment[:newline], fragment[newline + 1:]


def _parse_graph_header(head: bytes):
    from .logparse import _parse_header  # local import: private helper

    return _parse_header(head)


def _collect_refs(gitdir: str) -> dict[str, str]:
    refs = gitcmd.show_ref(gitdir)
    head = gitcmd.head_hash(gitdir)
    symbolic = gitcmd.symbolic_head(gitdir)
    if head:
        refs["HEAD"] = head
        if symbolic:
            refs[symbolic] = head
    # tags pointing at commits only -- annotated tags are peeled below
    peeled: dict[str, str] = {}
    for name, obj in refs.items():
        if not name.startswith("refs/tags/"):
            continue
        resolved = gitcmd.rev_parse(gitdir, f"{name}^{{commit}}")
        if resolved:
            peeled[name] = resolved
    refs.update(peeled)
    return refs


# --------------------------------------------------------------------------
# pass 3: path normalisation
# --------------------------------------------------------------------------

def _build_tables(commits: dict, changes: list, gitdir: str, ref: str) -> dict:
    """Assign integer ids and build the tree index structures."""
    # --- files: every path ever touched, plus the tree at the analysis ref
    present: dict[str, int] = {}
    gitlinks: set[str] = set()
    for path, size, kind in gitcmd.ls_tree(gitdir, ref):
        if kind == 1:
            gitlinks.add(path)
        present[path] = size

    path_pool: dict[str, None] = {}
    for _, path, old_path, _, _, _ in changes:
        path_pool.setdefault(path, None)
        if old_path:
            path_pool.setdefault(old_path, None)
    for path in present:
        path_pool.setdefault(path, None)
    files = sorted(path_pool)
    fid_of = {path: i for i, path in enumerate(files)}

    # --- directories: ancestors of every file + root
    dir_pool: dict[str, None] = {"": None}
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dir_pool.setdefault("/".join(parts[:i]), None)
    dirs = sorted(dir_pool)          # '' sorts first => root is did 0
    did_of = {path: i for i, path in enumerate(dirs)}

    n_dirs = len(dirs)
    n_files = len(files)
    file_did = np.zeros(n_files, dtype=np.int32)
    file_depth = np.zeros(n_files, dtype=np.int32)
    parent_did = np.zeros(n_dirs, dtype=np.int32)
    dir_depth = np.zeros(n_dirs, dtype=np.int32)
    child_count = np.zeros(n_dirs, dtype=np.int64)

    for path, did in did_of.items():
        if path:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            parent_did[did] = did_of[parent]
            dir_depth[did] = dir_depth[parent_did[did]] + 1
            child_count[parent_did[did]] += 1

    # --- per-file ancestor chain: chain[fid][k] for a file whose parent
    # directory has depth D is
    #   k = 0        -> the file itself (encoded as n_dirs + fid)
    #   k = 1..D+1   -> parent directory, grandparent, ..., root
    # so "the child of directory d on this file's path" is simply
    # chain[file_depth[fid] - dir_depth[d]] (spec 2.2's immediate-object relation)
    chain_lengths = np.zeros(n_files, dtype=np.int64)
    for i, path in enumerate(files):
        if "/" in path:
            dirpath = path.rsplit("/", 1)[0]
        else:
            dirpath = ""
        did = did_of[dirpath]
        file_did[i] = did
        file_depth[i] = dir_depth[did]
        chain_lengths[i] = dir_depth[did] + 2

    chain_start = np.zeros(n_files + 1, dtype=np.int64)
    np.cumsum(chain_lengths, out=chain_start[1:])
    chain_ids = np.zeros(int(chain_start[-1]), dtype=np.int32)
    for i, path in enumerate(files):
        base = int(chain_start[i])
        chain_ids[base] = n_dirs + i          # the file itself
        did = int(file_did[i])
        k = base + 1
        while True:
            chain_ids[k] = did
            if did == 0:
                break
            did = int(parent_did[did])
            k += 1

    # --- children CSR (immediate subdirectories of every directory)
    child_start = np.zeros(n_dirs + 1, dtype=np.int64)
    np.cumsum(child_count, out=child_start[1:])
    child_ids = np.zeros(int(child_start[-1]), dtype=np.int32)
    cursor = child_start[:-1].copy()
    for did in range(1, n_dirs):
        p = int(parent_did[did])
        child_ids[cursor[p]] = did
        cursor[p] += 1

    present_flags = np.zeros(n_files, dtype=np.uint8)
    sizes = np.full(n_files, -1, dtype=np.int64)
    for path, size in present.items():
        i = fid_of[path]
        present_flags[i] = 1
        sizes[i] = size

    file_kind = np.zeros(n_files, dtype=np.uint8)
    for path in gitlinks:
        file_kind[fid_of[path]] = 1

    return {
        "files": files,
        "fid_of": fid_of,
        "dirs": dirs,
        "did_of": did_of,
        "file_did": file_did,
        "file_depth": file_depth,
        "parent_did": parent_did,
        "dir_depth": dir_depth,
        "chain_start": chain_start,
        "chain_ids": chain_ids,
        "child_start": child_start,
        "child_ids": child_ids,
        "present": present_flags,
        "file_kind": file_kind,
        "sizes": sizes,
        "ref_hash": gitcmd.rev_parse(gitdir, ref),
    }


# --------------------------------------------------------------------------
# pass 4: persist
# --------------------------------------------------------------------------

def _write_index(
    index_dir: str,
    commits: dict,
    changes: list,
    tables: dict,
    refs: dict,
    meta: dict,
) -> dict:
    # Safety: never overwrite a git directory by mistake.
    if os.path.exists(os.path.join(index_dir, "HEAD")) and os.path.isdir(
            os.path.join(index_dir, "objects")):
        raise AnalysisError(
            f"refusing to write the index into '{index_dir}': it looks like a git directory")
    if os.path.isdir(index_dir):
        shutil.rmtree(index_dir)
    os.makedirs(index_dir, exist_ok=True)

    # ---- commits --------------------------------------------------------
    n = len(commits)
    hash_index = {meta_c["hash"]: i for i, meta_c in enumerate(commits.values())}
    hashes = [""] * n
    subjects = [""] * n
    ct = np.zeros(n, dtype=np.int64)
    at = np.zeros(n, dtype=np.int64)
    merge = np.zeros(n, dtype=np.uint8)
    diffed = np.zeros(n, dtype=np.uint8)

    ident_ids: dict[tuple[str, str], int] = {}
    identities: list[dict] = []
    raw_ids: dict[tuple[str, str], int] = {}
    rawids: list[dict] = []
    iid = np.zeros(n, dtype=np.int32)
    rid = np.zeros(n, dtype=np.int32)

    parent_lists: list[list[int]] = [[] for _ in range(n)]
    for cid, meta_c in enumerate(commits.values()):
        hashes[cid] = meta_c["hash"]
        subjects[cid] = meta_c["subject"]
        ct[cid] = meta_c["ct"]
        at[cid] = meta_c["at"]
        merge[cid] = meta_c["merge"]
        diffed[cid] = meta_c["diffed"]
        key = (meta_c["mn"], meta_c["me"])
        if key not in ident_ids:
            ident_ids[key] = len(identities)
            identities.append({"name": key[0], "email": key[1], "commits": 0,
                               "raw": []})
        iid[cid] = ident_ids[key]
        identities[ident_ids[key]]["commits"] += 1
        raw_key = (meta_c["rn"], meta_c["re"])
        if raw_key not in raw_ids:
            raw_ids[raw_key] = len(rawids)
            rawids.append({"name": raw_key[0], "email": raw_key[1],
                           "commits": 0, "identity": int(iid[cid])})
        rid[cid] = raw_ids[raw_key]
        rawids[raw_ids[raw_key]]["commits"] += 1

    for cid, meta_c in enumerate(commits.values()):
        for parent in meta_c["parents"]:
            pid = hash_index.get(parent)
            if pid is not None:
                parent_lists[cid].append(pid)

    p_start = np.zeros(n + 1, dtype=np.int64)
    np.cumsum([len(p) for p in parent_lists], out=p_start[1:])
    p_ids = np.array([i for pl in parent_lists for i in pl], dtype=np.int32)

    # ---- change rows ----------------------------------------------------
    fid_of = tables["fid_of"]
    if changes:
        ch_cid = np.array([c[0] for c in changes], dtype=np.int32)
        ch_fid = np.array([fid_of[c[1]] for c in changes], dtype=np.int32)
        ch_oldfid = np.array(
            [fid_of[c[2]] if c[2] else -1 for c in changes], dtype=np.int32)
        ch_added = np.array([c[3] for c in changes], dtype=np.int32)
        ch_removed = np.array([c[4] for c in changes], dtype=np.int32)
        ch_flags = np.array([c[5] for c in changes], dtype=np.uint8)
    else:
        ch_cid = np.zeros(0, dtype=np.int32)
        ch_fid = np.zeros(0, dtype=np.int32)
        ch_oldfid = np.full(0, -1, dtype=np.int32)
        ch_added = np.zeros(0, dtype=np.int32)
        ch_removed = np.zeros(0, dtype=np.int32)
        ch_flags = np.zeros(0, dtype=np.uint8)

    measured = (ch_flags & FLAG_UNMEASURED) == 0
    # gitlink (submodule) rows: never measured -- a submodule pointer is not a file
    if ch_fid.size:
        ch_flags = np.where(tables["file_kind"][ch_fid] == 1,
                            ch_flags | FLAG_SUBMODULE, ch_flags).astype(np.uint8)
        measured = (ch_flags & FLAG_UNMEASURED) == 0
    c_added = np.bincount(ch_cid[measured], weights=ch_added[measured],
                          minlength=n).astype(np.int64)
    c_removed = np.bincount(ch_cid[measured], weights=ch_removed[measured],
                            minlength=n).astype(np.int64)
    c_files = np.bincount(ch_cid[measured], minlength=n).astype(np.int32)
    c_binary = np.bincount(ch_cid[~measured], minlength=n).astype(np.int32)

    # ---- write ----------------------------------------------------------
    def save(name: str, arr: np.ndarray) -> None:
        np.save(os.path.join(index_dir, name), arr)

    def dump(name: str, obj) -> None:
        with open(os.path.join(index_dir, name), "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)

    dump("hashes.json", hashes)
    dump("subjects.json", subjects)
    dump("files.json", tables["files"])
    dump("dirs.json", tables["dirs"])
    dump("refs.json", refs)
    dump("identities.json", identities)
    dump("rawids.json", rawids)
    save("ct.npy", ct)
    save("at.npy", at)
    save("merge.npy", merge)
    save("diffed.npy", diffed)
    save("iid.npy", iid)
    save("rid.npy", rid)
    save("p_start.npy", p_start)
    save("p_ids.npy", p_ids)
    save("c_added.npy", c_added)
    save("c_removed.npy", c_removed)
    save("c_files.npy", c_files)
    save("c_binary.npy", c_binary)
    save("ch_cid.npy", ch_cid)
    save("ch_fid.npy", ch_fid)
    save("ch_oldfid.npy", ch_oldfid)
    save("ch_added.npy", ch_added)
    save("ch_removed.npy", ch_removed)
    save("ch_flags.npy", ch_flags)
    save("file_did.npy", tables["file_did"])
    save("file_depth.npy", tables["file_depth"])
    save("parent_did.npy", tables["parent_did"])
    save("dir_depth.npy", tables["dir_depth"])
    save("chain_start.npy", tables["chain_start"])
    save("chain_ids.npy", tables["chain_ids"])
    save("child_start.npy", tables["child_start"])
    save("child_ids.npy", tables["child_ids"])
    save("present.npy", tables["present"])
    save("file_kind.npy", tables["file_kind"])
    save("sizes.npy", tables["sizes"])

    non_merge = int((merge == 0).sum())
    stats = {
        "commits": n,
        "non_merge_commits": non_merge,
        "merge_commits": n - non_merge,
        "unparsed_commits": int((diffed == 0).sum()),
        "changes": int(ch_cid.size),
        "binary_rows": int((~measured).sum()),
        "files": len(tables["files"]),
        "dirs": len(tables["dirs"]) - 1,
        "identities": len(identities),
        "raw_identities": len(rawids),
        "added": int(c_added.sum()),
        "removed": int(c_removed.sum()),
        "present_files": int(tables["present"].sum()),
        "first_commit": int(ct.min()) if n else 0,
        "last_commit": int(ct.max()) if n else 0,
        "size_bytes": _dir_size(index_dir),
    }
    meta.update({"stats": stats, "schema": 1, "tool_version": "1.0.0"})
    dump("meta.json", meta)
    return stats


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
