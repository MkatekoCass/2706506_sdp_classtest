"""Streaming parser for ``git log --numstat -z`` output.

git is asked for a machine-readable stream (see :data:`rat.gitcmd.LOG_FORMAT`
and the ``-z`` flag) which this module turns into :class:`CommitStat`
records.  The stream looks like this::

    \\x1e<hash>\\x1f<ct>\\x1f<at>\\x1f<parents>\\x1f<aN>\\x1f<aE>\\x1f<an>\\x1f<ae>\\x1f<subject>
    12\\t3\\tsrc/foo.c\\0                      # normal change
    -\\t-\\tassets/logo.png\\0                  # binary file (never measured)
    4\\t0\\t\\0src/old.c\\0src/new.c\\0          # rename: old path \\0 new path \\0
    \\0                                        # end of this commit's diffstat
    \\x1e<hash>...
    0\\t0\\tsrc/mode-only.c\\0

Design notes
------------
* ``\\x1e`` / ``\\x1f`` are control bytes outside the printable range; git
  writes path names verbatim in ``-z`` mode, so a separator that cannot
  collide with real content is what makes the stream unambiguous.
* The parser is *incremental*: a repository with 100k commits and millions
  of changed-file rows is processed in constant memory.
* Commits without a diffstat (merge commits, ``--allow-empty`` commits) end
  their header immediately (no newline, just the block terminator), so the
  header terminator is *the first of* ``\n`` or ``\0``.
* If a path ever did contain a record separator the fragment would not look
  like a header (a header must start with a 40-char hex hash and carry
  exactly eight unit separators), so such fragments are re-attached to the
  previous commit instead of corrupting the stream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from .gitcmd import RS, US, decode_path


@dataclass(slots=True)
class FileStat:
    """Line statistics for one object in one commit (see spec 2.1/2.2)."""

    path: str                       #: path the change is attributed to
    added: int                      #: l+h,f  -- added lines
    removed: int                    #: l-h,f  -- removed lines
    old_path: str | None = None     #: set when git detected a rename
    binary: bool = False            #: git flagged the blob as binary

    @property
    def is_rename(self) -> bool:
        return self.old_path is not None

    @property
    def churn(self) -> int:
        """lambda_h,f = l+ + l- (spec 2.1)."""
        return self.added + self.removed


@dataclass(slots=True)
class CommitStat:
    """One commit plus its diffstat against its (single) parent."""

    hash: str
    ct: int                              #: committer date (unix seconds)
    at: int                              #: author date (unix seconds)
    parents: tuple[str, ...]             #: parent hashes (empty for root)
    mname: str                           #: .mailmap-resolved author name
    memail: str                          #: .mailmap-resolved author email
    rname: str                           #: raw author name
    remail: str                          #: raw author email
    subject: str
    files: list[FileStat] = field(default_factory=list)

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def churn(self) -> int:
        return self.added + self.removed


def _parse_header(line: bytes) -> tuple | None:
    """Parse a header line, returning its fields or ``None`` if malformed."""
    fields = line.split(US.encode(), 8)
    if len(fields) != 9:
        return None
    commit_hash = fields[0]
    if len(commit_hash) != 40 or not all(
        c in b"0123456789abcdef" for c in commit_hash
    ):
        return None
    try:
        ct = int(fields[1])
        at = int(fields[2])
    except ValueError:
        return None
    parents = tuple(p.decode("ascii") for p in fields[3].split() if p)
    decode = lambda b: b.decode("utf-8", "replace")  # noqa: E731
    return (
        commit_hash.decode("ascii"),
        ct,
        at,
        parents,
        decode(fields[4]),
        decode(fields[5]),
        decode(fields[6]),
        decode(fields[7]),
        decode(fields[8]),
    )


def _to_int(raw: bytes) -> int:
    try:
        return int(raw)
    except ValueError:
        return 0


def _parse_block(block: bytes, commit: CommitStat) -> None:
    """Parse the NUL-separated diffstat block that follows a commit header."""
    tokens = block.split(b"\0")
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if i == 0:
            # the block may start with the newline that separated it from
            # the header, or with stray NULs from git's framing
            token = token.lstrip(b"\n\0")
        if not token:
            i += 1
            continue
        added_raw, sep, rest = token.partition(b"\t")
        if not sep:
            i += 1
            continue
        removed_raw, sep, path = rest.partition(b"\t")
        if not sep:
            i += 1
            continue
        binary = added_raw == b"-"
        if path:
            commit.files.append(
                FileStat(
                    path=decode_path(path),
                    added=_to_int(added_raw),
                    removed=_to_int(removed_raw),
                    binary=binary,
                )
            )
            i += 1
        else:
            # rename / copy: <added>\t<removed>\t\0<old>\0<new>\0
            old = tokens[i + 1] if i + 1 < n else b""
            new = tokens[i + 2] if i + 2 < n else b""
            commit.files.append(
                FileStat(
                    path=decode_path(new),
                    added=_to_int(added_raw),
                    removed=_to_int(removed_raw),
                    old_path=decode_path(old),
                    binary=binary,
                )
            )
            i += 3


def iter_fragments(stream, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    """Yield record fragments split on the record separator byte."""
    buf = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        buf += chunk
        pieces = buf.split(RS.encode())
        buf = pieces.pop()
        for piece in pieces:
            if piece:
                yield piece
    if buf.strip(b"\n\0"):
        yield buf


def _split_header(fragment: bytes) -> tuple[bytes, bytes]:
    """Split a fragment into (header, diffstat block).

    The header is terminated by a newline when a diffstat follows and by the
    NUL block terminator (or end of fragment) when it does not -- which is
    exactly what happens for merge commits.
    """
    newline = fragment.find(b"\n")
    nul = fragment.find(b"\0")
    if newline >= 0 and (nul < 0 or newline < nul):
        return fragment[:newline], fragment[newline + 1:]
    if nul >= 0:
        return fragment[:nul], fragment[nul + 1:]
    return fragment, b""


def iter_commits(
    stream,
    chunk_size: int = 1 << 20,
    on_commit: Callable[[CommitStat], None] | None = None,
) -> Iterator[CommitStat]:
    """Parse a ``git log --numstat -z`` stream into :class:`CommitStat`."""
    current: CommitStat | None = None
    for fragment in iter_fragments(stream, chunk_size):
        head, block = _split_header(fragment)
        header = _parse_header(head)
        if header is not None:
            if current is not None:
                yield current
                if on_commit:
                    on_commit(current)
            current = CommitStat(*header)
            _parse_block(block, current)
        elif current is not None:
            # Continuation of the previous commit's block (a path that
            # contains the record separator byte, or a stray fragment).
            _parse_block(fragment, current)
    if current is not None:
        yield current
        if on_commit:
            on_commit(current)
