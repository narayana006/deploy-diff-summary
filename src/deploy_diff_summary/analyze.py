"""Collect everything that changed between two refs into one Summary."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from .gitrepo import Commit, FileChange, Repo
from .parsers import (DepChange, ImageChange, KeyChange, dependency_parser, diff_deps, diff_images,
                      diff_keys, find_images, parse_structured)

# A pattern without "/" is matched against the file name, otherwise against the
# whole path. "*" matches any run of characters, including "/".
MIGRATION_PATTERNS = ("*migrations/*", "*migrate/*", "*alembic/versions/*", "db/schema.rb",
                      "db/structure.sql", "*.sql")
LOCKFILES = ("poetry.lock", "uv.lock", "Pipfile.lock", "package-lock.json", "yarn.lock",
             "pnpm-lock.yaml", "go.sum", "Cargo.lock", "Gemfile.lock", "composer.lock")
OTHER_MANIFESTS = ("pyproject.toml", "Pipfile", "Cargo.toml", "Gemfile", "pom.xml", "build.gradle*",
                   "composer.json")
CONFIG_PATTERNS = (".env*", "*.env", "*.properties", "*.ini", "*.conf", "*.cfg", "values*.yaml",
                   "values*.yml", "application*.yml", "application*.yaml", "*settings*.py",
                   "*config/*", "*conf/*", "config.*", "*configmap*")
INFRA_PATTERNS = ("*.tf", "*.tfvars", "Dockerfile*", "*.dockerfile", "docker-compose*.yml",
                  "docker-compose*.yaml", "*terraform/*", "*k8s/*", "*kubernetes/*", "*manifests/*",
                  "*helm/*", "*charts/*", "*deploy/*", "*ansible/*", ".github/workflows/*",
                  ".gitlab-ci.yml", "Jenkinsfile")
ATTENTION = re.compile(r"\b(revert|hotfix|rollback|roll back|breaking|urgent|workaround)\b|^\w+(\([^)]*\))?!:",
                       re.I)
STATUS_WORDS = {"A": "added", "M": "modified", "D": "removed"}


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path if "/" in p else name, p) for p in patterns)


@dataclass
class ConfigFile:
    path: str
    status: str
    added: int
    deleted: int
    keys: list[KeyChange] | None  # None: not a key/value format we can compare


@dataclass
class DependencyFile:
    path: str
    status: str
    changes: list[DepChange] | None  # None: changed, but the format is not parsed


@dataclass
class ImageUpdate:
    change: ImageChange
    files: list[str]


@dataclass
class Summary:
    old: str
    new: str
    old_sha: str
    new_sha: str
    commits: list[Commit] = field(default_factory=list)
    files: list[FileChange] = field(default_factory=list)
    migrations: list[FileChange] = field(default_factory=list)
    config: list[ConfigFile] = field(default_factory=list)
    dependencies: list[DependencyFile] = field(default_factory=list)
    images: list[ImageUpdate] = field(default_factory=list)
    infra: list[FileChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def attention_commits(self) -> list[Commit]:
        return [c for c in self.commits if ATTENTION.search(c.subject)]

    @property
    def risky_dependencies(self) -> list[DepChange]:
        return [c for d in self.dependencies for c in d.changes or []
                if c.kind in ("major upgrade", "downgrade", "removed")]


def summarize(repo: Repo, old: str, new: str) -> Summary:
    """Compare two refs. Reads the repository; never changes it."""
    s = Summary(old, new, repo.resolve(old), repo.resolve(new))
    if s.old_sha == s.new_sha:
        s.notes.append("Both refs point at the same commit; nothing changed.")
        return s
    if not repo.is_ancestor(s.old_sha, s.new_sha):
        if repo.is_ancestor(s.new_sha, s.old_sha):
            s.notes.append(f"`{new}` is older than `{old}`: this is a rollback, so the commit list is "
                           "empty and the file changes below are the ones being undone.")
        else:
            s.notes.append(f"`{old}` is not an ancestor of `{new}`: the histories have diverged. Commits "
                           f"listed are those only in `{new}`; file changes compare the two trees directly.")

    s.commits = repo.commits(s.old_sha, s.new_sha)
    s.files = repo.changed_files(s.old_sha, s.new_sha)
    images: dict[ImageChange, list[str]] = {}

    for f in s.files:
        before = after = None

        def content() -> tuple[str, str]:
            nonlocal before, after
            if before is None:
                before = "" if f.status == "A" else repo.read(s.old_sha, f.path)
                after = "" if f.status == "D" else repo.read(s.new_sha, f.path)
            return before, after

        if _may_hold_images(f.path):
            for change in diff_images(*(find_images(f.path, text) for text in content())):
                images.setdefault(change, []).append(f.path)

        if matches(f.path, MIGRATION_PATTERNS):
            s.migrations.append(f)
        elif dependency_parser(f.path):
            parse = dependency_parser(f.path)
            s.dependencies.append(DependencyFile(f.path, f.status, diff_deps(*(parse(t) for t in content()))))
        elif matches(f.path, LOCKFILES + OTHER_MANIFESTS):
            s.dependencies.append(DependencyFile(f.path, f.status, None))
        elif matches(f.path, CONFIG_PATTERNS):
            old_keys, new_keys = (parse_structured(f.path, t) for t in content())
            keys = None if old_keys is None or new_keys is None else diff_keys(old_keys, new_keys)
            s.config.append(ConfigFile(f.path, f.status, f.added, f.deleted, keys))
        elif matches(f.path, INFRA_PATTERNS):
            s.infra.append(f)

    s.images = [ImageUpdate(change, files) for change, files in sorted(images.items(), key=lambda i: i[0].image)]
    return s


def _may_hold_images(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.endswith((".yml", ".yaml", ".dockerfile")) or name.startswith("dockerfile")
