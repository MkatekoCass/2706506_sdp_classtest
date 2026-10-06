"""Repository registry: multi-repository state, background jobs, caching.

The registry is the single source of truth for *which* repositories the RAT
knows about, where their git directory and columnar index live, and what the
background ingestion job (clone / unzip / analyse) is currently doing.

State layout on disk::

    data/
      registry.json            # repository metadata (paths relative to data/)
      repos/<id>/repo.git      # bare clone, extracted zip, or symlink to a
                               # local repository
      repos/<id>/index/        # columnar metric index (rat.analysis)

The columnar index is *never* placed inside the git directory: bare
repositories keep their staging ``index`` file there.

Loaded :class:`~rat.repoindex.RepoIndex` objects are cached in a small LRU
(each index is memory-mapped, so the cache is cheap) which is what makes
switching between repositories instant.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from typing import Callable

from .repoindex import RepoIndex

SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def slugify(value: str, fallback: str = "repo") -> str:
    slug = SLUG_RE.sub("-", (value or "").strip().lower()).strip("-._")
    return slug[:48] or fallback


class RepoRegistry:
    """Thread-safe registry of analysed repositories."""

    INDEX_CACHE_SIZE = 3

    def __init__(self, data_root: str):
        self.data_root = os.path.abspath(data_root)
        self.repos_root = os.path.join(self.data_root, "repos")
        self.registry_path = os.path.join(self.data_root, "registry.json")
        os.makedirs(self.repos_root, exist_ok=True)
        self.lock = threading.RLock()
        self.repos: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self._index_cache: "OrderedDict[str, RepoIndex]" = OrderedDict()
        self._cancels: dict[str, threading.Event] = {}
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        if not os.path.exists(self.registry_path):
            return
        try:
            with open(self.registry_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            self.repos = payload.get("repos", {})
        except (OSError, ValueError):
            self.repos = {}

    def _save(self) -> None:
        payload = {"schema": 1, "repos": self.repos}
        tmp = self.registry_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.registry_path)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def list(self) -> list[dict]:
        with self.lock:
            return [self._public(r) for r in
                    sorted(self.repos.values(), key=lambda r: r.get("created", 0))]

    def get(self, rid: str) -> dict | None:
        with self.lock:
            repo = self.repos.get(rid)
            return dict(repo) if repo else None

    def public(self, rid: str) -> dict | None:
        with self.lock:
            repo = self.repos.get(rid)
            return self._public(repo) if repo else None

    def _public(self, repo: dict) -> dict:
        job = self.jobs.get(repo["id"], {})
        out = {k: v for k, v in repo.items() if k not in ("merges",)}
        out["job"] = job
        out["merges"] = repo.get("merges", [])
        return out

    # ------------------------------------------------------------------
    # mutation
    # ------------------------------------------------------------------
    def create(self, name: str, kind: str, source: str, *, analysis_ref: str = "HEAD",
               full_history: bool = False, url_name: str | None = None) -> dict:
        base = slugify(url_name or name or source)
        with self.lock:
            rid = base
            while rid in self.repos:
                rid = f"{base}-{uuid.uuid4().hex[:4]}"
            repo = {
                "id": rid,
                "name": name or base,
                "kind": kind,               # clone | zip | local
                "source": source,
                "analysis_ref": analysis_ref,
                "full_history": bool(full_history),
                "created": int(time.time()),
                "analyzed_at": None,
                "status": "queued",
                "error": None,
                "gitdir": os.path.join("repos", rid, "repo.git"),
                "index": os.path.join("repos", rid, "index"),
                "stats": None,
                "merges": [],
                "warnings": [],
            }
            self.repos[rid] = repo
            self._save()
        return repo

    def update(self, rid: str, **fields) -> dict | None:
        with self.lock:
            repo = self.repos.get(rid)
            if repo is None:
                return None
            repo.update(fields)
            self._save()
            return dict(repo)

    def delete(self, rid: str) -> bool:
        self.cancel(rid)
        with self.lock:
            repo = self.repos.pop(rid, None)
            self._index_cache.pop(rid, None)
            self.jobs.pop(rid, None)
            if repo is None:
                return False
            self._save()
        path = os.path.join(self.repos_root, rid)
        shutil.rmtree(path, ignore_errors=True)
        return True

    # ------------------------------------------------------------------
    # job state / cancellation
    # ------------------------------------------------------------------
    def set_job(self, rid: str, **fields) -> None:
        with self.lock:
            job = self.jobs.setdefault(rid, {})
            job.update(fields)

    def job(self, rid: str) -> dict:
        with self.lock:
            return dict(self.jobs.get(rid, {}))

    def cancel_event(self, rid: str) -> threading.Event:
        with self.lock:
            return self._cancels.setdefault(rid, threading.Event())

    def cancel(self, rid: str) -> None:
        with self.lock:
            event = self._cancels.get(rid)
        if event:
            event.set()

    def finish_job(self, rid: str, *, status: str, error: str | None = None) -> None:
        with self.lock:
            self._cancels.pop(rid, None)
        self.update(rid, status=status, error=error,
                    analyzed_at=int(time.time()) if status == "ready" else None)

    # ------------------------------------------------------------------
    # paths & index access
    # ------------------------------------------------------------------
    def paths(self, rid: str) -> tuple[str, str]:
        repo = self.get(rid)
        if repo is None:
            raise KeyError(rid)
        gitdir = os.path.join(self.data_root, repo["gitdir"])
        index = os.path.join(self.data_root, repo["index"])
        return gitdir, index

    def index(self, rid: str) -> RepoIndex:
        """Load (and cache) the columnar index of a repository."""
        with self.lock:
            cached = self._index_cache.get(rid)
            if cached is not None:
                self._index_cache.move_to_end(rid)
                return cached
        _, index_dir = self.paths(rid)
        idx = RepoIndex(index_dir)
        idx.meta.setdefault("name", self.get(rid)["name"])
        groups = (self.get(rid) or {}).get("merges") or []
        idx.set_merge_groups(groups)
        with self.lock:
            self._index_cache[rid] = idx
            self._index_cache.move_to_end(rid)
            while len(self._index_cache) > self.INDEX_CACHE_SIZE:
                self._index_cache.popitem(last=False)
        return idx

    def invalidate(self, rid: str) -> None:
        with self.lock:
            self._index_cache.pop(rid, None)

    def set_merges(self, rid: str, groups: list[dict]) -> None:
        self.update(rid, merges=groups)
        with self.lock:
            idx = self._index_cache.get(rid)
        if idx is not None:
            idx.set_merge_groups(groups)


def run_job(registry: RepoRegistry, rid: str, work: Callable[[], None]) -> None:
    """Run ``work`` in a daemon thread, reporting failures into the registry."""
    def runner() -> None:
        try:
            work()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            registry.set_job(rid, finished=True, message=str(exc))
            registry.finish_job(rid, status="error", error=str(exc))
    thread = threading.Thread(target=runner, name=f"rat-job-{rid}", daemon=True)
    thread.start()
