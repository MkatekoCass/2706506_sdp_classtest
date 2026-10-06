"""Repository ingestion: zip archives, remote URLs and local paths.

Three ways in, all ending in a git directory the analysis pipeline can read:

``clone``
    ``git clone --bare`` -- a *deep* clone (full history, no shallow
    boundary) with the working tree omitted, because metrics only ever read
    committed data.
``zip``
    A ``.zip`` of a repository including its ``.git`` file *or* directory.
    Archive members are validated before extraction (no absolute paths, no
    ``..`` traversal, no symlinks escaping the sandbox) and the repository
    root is located afterwards, covering the three shapes people actually
    produce:

    * ``myproject/.git/``      -- a normal checkout zipped from its parent
    * ``myproject/.git`` (file) -- a worktree/submodule pointer
    * ``myproject.git/`` or a bare repo zipped directly -- ``HEAD``,
      ``objects/`` and ``refs/`` at the top level
``local``
    A path to an existing repository on the machine (offline-friendly, used
    by the tests and the demo).

After ingestion the git directory is *validated*: it must resolve a commit,
and the analysis reference must exist, otherwise the user gets a precise
error instead of an empty dashboard.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from typing import Callable

from . import gitcmd

ProgressFn = Callable[[str], None]

#: Refuse absurd archives before touching the disk (zip-bomb guard).
MAX_UNCOMPRESSED = 40 * 1024 ** 3  # 40 GiB
MAX_MEMBERS = 4_000_000


class IngestError(RuntimeError):
    pass


def _noop(_message: str) -> None:
    pass


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------

def ingest_clone(url: str, dest: str, progress: ProgressFn | None = None) -> dict:
    report = progress or _noop
    if not _looks_like_url(url):
        raise IngestError(
            "not a supported URL: use https://, http://, git://, ssh:// or "
            "user@host:path (or a local path / file:// URL)")
    report(f"cloning {url} (full history)")
    gitcmd.clone(url, dest, on_progress=report)
    return validate(dest)


def _looks_like_url(url: str) -> bool:
    if url.startswith(("https://", "http://", "git://", "ssh://", "file://")):
        return True
    # scp-like syntax: user@host:path
    if "@" in url and ":" in url.split("@", 1)[1]:
        return True
    return False


# --------------------------------------------------------------------------
# local path
# --------------------------------------------------------------------------

def ingest_local(path: str, dest: str, progress: ProgressFn | None = None) -> dict:
    report = progress or _noop
    source = path[7:] if path.startswith("file://") else path
    source = os.path.abspath(os.path.expanduser(source))
    if not os.path.exists(source):
        raise IngestError(f"path does not exist: {source}")
    gitdir = _locate_git_dir(source)
    if gitdir is None:
        raise IngestError(
            f"no git repository found at {source} (looked for .git, HEAD+objects/)")
    report(f"linking local repository {gitdir}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    try:
        os.symlink(gitdir, dest, target_is_directory=True)
    except OSError:
        # filesystem without symlink support: fall back to a copy
        shutil.copytree(gitdir, dest)
    return validate(dest)


# --------------------------------------------------------------------------
# zip
# --------------------------------------------------------------------------

def ingest_zip(archive_path: str, dest: str, progress: ProgressFn | None = None) -> dict:
    report = progress or _noop
    if not zipfile.is_zipfile(archive_path):
        raise IngestError("the uploaded file is not a valid zip archive")
    extract_root = dest + ".extract"
    if os.path.exists(extract_root):
        shutil.rmtree(extract_root, ignore_errors=True)
    os.makedirs(extract_root, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive_path) as zf:
        members = zf.infolist()
        if len(members) > MAX_MEMBERS:
            raise IngestError(f"archive has {len(members)} members (limit {MAX_MEMBERS})")
        for info in members:
            _validate_member(info)
            total += info.file_size
            if total > MAX_UNCOMPRESSED:
                raise IngestError("archive expands to more than 40 GiB -- refusing")
        report(f"extracting {len(members)} entries")
        for n, info in enumerate(members):
            zf.extract(info, extract_root)
            if n % 2000 == 0:
                report(f"extracting ({n}/{len(members)})")
    gitdir = _locate_git_dir(extract_root)
    if gitdir is None:
        raise IngestError(
            "the archive does not contain a git repository: expected a .git "
            "directory (or file), or a bare repository with HEAD/objects/refs")
    report(f"repository found at {os.path.relpath(gitdir, extract_root)}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    if _is_bare(gitdir):
        # a bare repository can simply be moved into place
        shutil.move(gitdir, dest)
    else:
        # a working tree: keep only the git directory (metrics never need
        # the checked out files, and they may be huge)
        shutil.move(gitdir, dest)
    shutil.rmtree(extract_root, ignore_errors=True)
    return validate(dest)


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if name.startswith(("/", "\\")) or ":" in name.split("/")[0][:3]:
        raise IngestError(f"archive member with an absolute path: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise IngestError(f"archive member escapes the extraction directory: {name!r}")
    # symlinks in the archive are extracted as plain files by zipfile, so a
    # link target cannot be used to escape; nothing else to check here.
    if info.file_size > MAX_UNCOMPRESSED:
        raise IngestError(f"archive member too large: {name!r}")


def _locate_git_dir(root: str) -> str | None:
    """Find the git directory inside an extracted archive."""
    candidates: list[str] = []

    def consider(directory: str) -> None:
        dot_git = os.path.join(directory, ".git")
        if os.path.isdir(dot_git):
            candidates.append(dot_git)
        elif os.path.isfile(dot_git):
            pointer = _read_gitfile(dot_git, directory)
            if pointer:
                candidates.append(pointer)
        if _is_bare(directory):
            candidates.append(directory)

    consider(root)
    for entry in sorted(os.listdir(root))[:200]:
        sub = os.path.join(root, entry)
        if os.path.isdir(sub):
            consider(sub)
            for entry2 in sorted(os.listdir(sub))[:200]:
                sub2 = os.path.join(sub, entry2)
                if os.path.isdir(sub2):
                    consider(sub2)
    return candidates[0] if candidates else None


def _read_gitfile(path: str, base: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            line = fh.readline().strip()
    except OSError:
        return None
    if not line.lower().startswith("gitdir:"):
        return None
    target = line.split(":", 1)[1].strip()
    resolved = os.path.normpath(os.path.join(base, target))
    root = os.path.abspath(base)
    if not os.path.abspath(resolved).startswith(root + os.sep):
        return None      # pointer escapes the archive: refuse
    return resolved if os.path.isdir(resolved) else None


def _is_bare(path: str) -> bool:
    return (os.path.isfile(os.path.join(path, "HEAD"))
            and os.path.isdir(os.path.join(path, "objects"))
            and os.path.isdir(os.path.join(path, "refs")))


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(gitdir: str) -> dict:
    """Check that ``gitdir`` is a usable repository and describe it."""
    gitdir = os.path.abspath(gitdir)
    if not os.path.exists(gitdir):
        raise IngestError(f"git directory missing after ingestion: {gitdir}")
    proc = gitcmd.run(gitdir, ["rev-parse", "--git-dir"], check=False)
    if proc.returncode != 0:
        raise IngestError(
            f"not a git repository: {gitdir} "
            f"({proc.stderr.decode('utf-8', 'replace').strip()[:200]})")
    head = gitcmd.head_hash(gitdir)
    if head is None:
        raise IngestError("the repository has no commits on HEAD")
    refs = gitcmd.show_ref(gitdir)
    bare = gitcmd.is_bare(gitdir)
    shallow = gitcmd.is_shallow(gitdir)
    mailmap = gitcmd.cat_blob(gitdir, "HEAD", ".mailmap")
    warnings = []
    if shallow:
        warnings.append(
            "this is a shallow clone: commits before the shallow boundary are "
            "absent, so metrics describe only the available history")
    if not refs:
        warnings.append("the repository has no refs besides HEAD")
    return {
        "gitdir": gitdir,
        "bare": bare,
        "shallow": shallow,
        "head": head,
        "refs": len(refs),
        "has_mailmap": mailmap is not None,
        "mailmap_entries": _count_mailmap(mailmap),
        "warnings": warnings,
    }


def _count_mailmap(blob: bytes | None) -> int:
    if not blob:
        return 0
    return sum(1 for line in blob.decode("utf-8", "replace").splitlines()
               if line.strip() and not line.lstrip().startswith("#"))
