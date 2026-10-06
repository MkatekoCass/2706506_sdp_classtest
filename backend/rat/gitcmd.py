"""Thin, well-behaved wrappers around the ``git`` command line.

Every observation the RAT makes about a repository is produced by git
itself.  That is deliberate: the spec defines the metrics in terms of git's
view of the world (rename detection at a 50% threshold, git's binary-file
detection, git's ``.mailmap`` author merging), so delegating to git is both
the *most correct* and the *fastest* option -- the heavy diffing happens in
C rather than in a Python per-commit loop.

Conventions used throughout the backend:

* ``gitdir`` is a path that can be handed to ``git --git-dir`` (either a
  bare repository or the ``.git`` directory of a normal checkout).
* Output is captured as raw bytes; callers decode explicitly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Iterable, Sequence

#: Separator bytes used to frame the custom ``--pretty=format`` header.
RS = "\x1e"  # record separator: starts a commit header
US = "\x1f"  # unit separator: separates the header fields

#: ``git log`` header emitted for every commit.  Field order matters: the
#: subject (``%s``) is last because it may contain most printable
#: characters, everything before it is a fixed-width or single-line value.
#: ``%aN``/``%aE`` are the ``.mailmap``-resolved author identity,
#: ``%an``/``%ae`` the raw one, ``%ct``/``%at`` the committer/author unix
#: timestamps and ``%P`` the parent hashes.
LOG_FORMAT = (
    f"{RS}%H{US}%ct{US}%at{US}%P{US}%aN{US}%aE{US}%an{US}%ae{US}%s"
)


class GitError(RuntimeError):
    """Raised when a git invocation fails unexpectedly."""


def _env() -> dict:
    env = dict(os.environ)
    # Never block waiting for credentials and keep messages stable.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_PAGER"] = "cat"
    env["LC_ALL"] = "C"
    return env


def run(
    gitdir: str,
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ``git --git-dir=<gitdir> <args>`` and return the result."""
    cmd = ["git", "--git-dir", gitdir, *args]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc


def text(gitdir: str, args: Sequence[str], *, check: bool = True) -> str:
    """Run a git command and return its stdout decoded as text."""
    return run(gitdir, args, check=check).stdout.decode("utf-8", "replace")


def rev_parse(gitdir: str, rev: str) -> str | None:
    """Resolve ``rev`` to a full commit hash, or ``None`` if unknown."""
    proc = run(gitdir, ["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
               check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("ascii", "replace").strip()


def head_hash(gitdir: str) -> str | None:
    return rev_parse(gitdir, "HEAD")


def symbolic_head(gitdir: str) -> str | None:
    proc = run(gitdir, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip()


def show_ref(gitdir: str) -> dict[str, str]:
    """Return every ref (heads, tags, remotes) mapped to its object hash."""
    proc = run(gitdir, ["show-ref"], check=False)
    refs: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            refs[parts[1].strip()] = parts[0].strip()
    return refs


def rev_list_count(gitdir: str, rev: str, *, no_merges: bool = False) -> int:
    args = ["rev-list", "--count"]
    if no_merges:
        args.append("--no-merges")
    args.append(rev)
    proc = run(gitdir, args, check=False)
    if proc.returncode != 0:
        return 0
    try:
        return int(proc.stdout.decode("ascii").strip())
    except ValueError:
        return 0


def is_bare(gitdir: str) -> bool:
    proc = run(gitdir, ["rev-parse", "--is-bare-repository"], check=False)
    return proc.stdout.decode("ascii", "replace").strip() == "true"


def is_shallow(gitdir: str) -> bool:
    proc = run(gitdir, ["rev-parse", "--is-shallow-repository"], check=False)
    return proc.stdout.decode("ascii", "replace").strip() == "true"


def cat_blob(gitdir: str, rev: str, path: str) -> bytes | None:
    """Return the contents of ``rev:path`` or ``None`` when it is absent."""
    proc = run(gitdir, ["cat-file", "blob", f"{rev}:{path}"], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def path_exists_in_tree(gitdir: str, rev: str, path: str) -> bool:
    proc = run(gitdir, ["cat-file", "-e", f"{rev}:{path}"], check=False)
    return proc.returncode == 0


def ls_tree(gitdir: str, rev: str) -> list[tuple[str, int, int]]:
    """List ``rev``'s entries as ``(path, size, kind)`` triples.

    ``kind`` is ``0`` for regular files (blobs) and ``1`` for gitlinks
    (submodule pointers, mode 160000).  Gitlinks must not be measured: the
    spec measures *files*, and git's own numstat reports a submodule bump as
    a one-line change which would otherwise pollute directory metrics.
    """
    proc = run(gitdir, ["ls-tree", "-r", "-l", "-z", rev], check=False)
    out: list[tuple[str, int, int]] = []
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            meta, path = entry.split(b"\t", 1)
        except ValueError:
            continue
        fields = meta.split()
        # <mode> <type> <object> <size>\t<path>
        if len(fields) < 4:
            continue
        if fields[1] == b"commit":
            out.append((_decode(path), -1, 1))
            continue
        if fields[1] != b"blob":
            continue
        try:
            size = int(fields[3])
        except ValueError:
            size = -1
        out.append((_decode(path), size, 0))
    return out


def open_stream(gitdir: str, args: Sequence[str]) -> subprocess.Popen:
    """Start a git command whose stdout is consumed incrementally."""
    cmd = ["git", "--git-dir", gitdir, *args]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
        bufsize=0,
    )


def clone(
    url: str,
    dest: str,
    *,
    on_progress: Callable[[str], None] | None = None,
    timeout: float | None = 3600.0,
) -> None:
    """Deep-clone ``url`` into ``dest`` as a *bare* repository.

    A bare clone carries the complete history (it is not shallow), which is
    all the RAT ever needs, and avoids materialising a second copy of every
    file in a working tree.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    cmd = ["git", "clone", "--bare", "--progress", url, dest]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
        bufsize=0,
    )
    tail: list[str] = []
    try:
        assert proc.stderr is not None
        buf = b""
        while True:
            chunk = proc.stderr.read(256)
            if not chunk:
                break
            buf += chunk
            pieces = buf.replace(b"\r", b"\n").split(b"\n")
            buf = pieces.pop()
            for piece in pieces:
                line = piece.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail.append(line)
                del tail[:-20]
                if on_progress:
                    on_progress(line)
        if buf.strip():
            tail.append(buf.decode("utf-8", "replace").strip())
        code = proc.wait(timeout=timeout)
    except Exception:
        proc.kill()
        raise
    if code != 0:
        detail = tail[-1] if tail else "unknown error"
        raise GitError(f"clone failed: {detail}")


def _decode(raw: bytes) -> str:
    """Decode a git path: UTF-8 when possible, otherwise byte-preserving."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def decode_path(raw: bytes) -> str:
    return _decode(raw)


def iter_lines(stream: Iterable[bytes]) -> Iterable[str]:
    for line in stream:
        yield line.decode("utf-8", "replace").rstrip("\n")
