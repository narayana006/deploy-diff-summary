"""Read-only access to a local git repository.

Only `rev-parse`, `merge-base`, `log`, `diff` and `show` are ever run. Nothing
here writes to the repository, fetches, or touches the working tree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

READ_ONLY_COMMANDS = {"rev-parse", "merge-base", "log", "diff", "show"}


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    subject: str


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # A (added), M (modified) or D (deleted)
    added: int = 0
    deleted: int = 0


class Repo:
    def __init__(self, path: str = "."):
        self.path = path

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        if args[0] not in READ_ONLY_COMMANDS:
            raise GitError(f"refusing to run non-read-only git command: {args[0]}")
        cmd = ["git", "-C", self.path, "-c", "core.quotepath=off", *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH") from exc
        if check and proc.returncode != 0:
            lines = proc.stderr.strip().splitlines()
            raise GitError(lines[-1] if lines else f"git {args[0]} failed")
        return proc

    def resolve(self, ref: str) -> str:
        """Return the full commit SHA for a ref, or raise GitError."""
        proc = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if proc.returncode != 0:
            inside = self._git("rev-parse", "--git-dir", check=False)
            if inside.returncode != 0:
                raise GitError(f"{self.path} is not a git repository")
            raise GitError(f"unknown ref {ref!r} (is the tag or branch fetched locally?)")
        return proc.stdout.strip()

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self._git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0

    def commits(self, old: str, new: str) -> list[Commit]:
        """Non-merge commits reachable from `new` but not `old`, newest first."""
        out = self._git("log", "--no-merges", "--format=%H%x1f%an%x1f%s", f"{old}..{new}").stdout
        return [Commit(*line.split("\x1f", 2)) for line in out.splitlines() if line.count("\x1f") >= 2]

    def changed_files(self, old: str, new: str) -> list[FileChange]:
        status = {}
        for line in self._git("diff", "--name-status", "--no-renames", old, new).stdout.splitlines():
            code, _, path = line.partition("\t")
            if path:
                status[path] = code[:1]
        files = []
        for line in self._git("diff", "--numstat", "--no-renames", old, new).stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                a, d, path = parts
                files.append(FileChange(path, status.get(path, "M"),
                                        int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0))
        return files

    def read(self, ref: str, path: str) -> str:
        """File content at a ref, or '' when the file does not exist there."""
        proc = self._git("show", f"{ref}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else ""
