"""Render a Summary as Markdown or JSON, most impactful sections first."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .analyze import STATUS_WORDS, Summary
from .registry import ImageComparison

MAX_COMMITS = 50
MAX_KEYS_PER_FILE = 25


@dataclass
class Section:
    rank: int  # higher = more likely to matter in a bad deploy
    title: str
    headline: str
    lines: list[str]


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _code(value: str | None) -> str:
    return f"`{value}`" if value not in (None, "") else "(empty)"


def _cell(value: str | None) -> str:
    return "-" if value is None else f"`{value}`".replace("|", "\\|")


def _migrations(s: Summary) -> Section | None:
    if not s.migrations:
        return None
    counts = {st: sum(f.status == st for f in s.migrations) for st in "AMD"}
    parts = [f"{counts[st]} {STATUS_WORDS[st]}" for st in "AMD" if counts[st]]
    headline = f"{_n(len(s.migrations), 'migration file')} ({', '.join(parts)})"
    if counts["M"] or counts["D"]:
        headline += "; existing migrations were edited or removed, check they were not already applied"
    lines = [f"- {STATUS_WORDS[f.status]} `{f.path}` (+{f.added} / -{f.deleted})"
             for f in sorted(s.migrations, key=lambda f: ("MDA".index(f.status), f.path))]
    return Section(100, "Database migrations", headline, lines)


def _config(s: Summary) -> Section | None:
    if not s.config:
        return None
    lines, key_total = [], 0
    for cf in s.config:
        lines.append(f"- `{cf.path}` ({STATUS_WORDS[cf.status]}, +{cf.added} / -{cf.deleted})")
        if cf.keys is None:
            lines.append("  - not a key/value format; review the diff directly")
            continue
        key_total += len(cf.keys)
        for k in cf.keys[:MAX_KEYS_PER_FILE]:
            if k.kind == "changed":
                lines.append(f"  - changed `{k.key}`: {_code(k.old)} -> {_code(k.new)}")
            elif k.kind == "added":
                lines.append(f"  - added `{k.key}` = {_code(k.new)}")
            else:
                lines.append(f"  - removed `{k.key}` (was {_code(k.old)})")
        if len(cf.keys) > MAX_KEYS_PER_FILE:
            lines.append(f"  - ... and {len(cf.keys) - MAX_KEYS_PER_FILE} more keys")
        if not cf.keys:
            lines.append("  - no key or value differences (comments or formatting only)")
    headline = f"{_n(len(s.config), 'config file')} changed"
    if key_total:
        headline += f", {_n(key_total, 'setting')} added, removed or changed"
    return Section(90, "Configuration and environment", headline, lines)


def _images(s: Summary) -> Section | None:
    if not s.images:
        return None
    lines = ["| Image | From | To | Where |", "|---|---|---|---|"]
    for u in s.images:
        where = ", ".join(f"`{p}`" for p in u.files)
        lines.append(f"| `{u.change.image}` | {_cell(u.change.old)} | {_cell(u.change.new)} | {where} |")
    return Section(80, "Container images", f"{_n(len(s.images), 'container image reference')} changed", lines)


def _dependencies(s: Summary) -> Section | None:
    if not s.dependencies:
        return None
    order = {"major upgrade": 0, "downgrade": 1, "removed": 2, "added": 3, "changed": 4, "upgrade": 5}
    rows = sorted(((c, d.path) for d in s.dependencies for c in d.changes or []),
                  key=lambda r: (order[r[0].kind], r[0].name))
    lines = []
    if rows:
        lines += ["| Package | From | To | Change | File |", "|---|---|---|---|---|"]
        lines += [f"| `{c.name}` | {_cell(c.old)} | {_cell(c.new)} | {c.kind} | `{path}` |" for c, path in rows]
    unparsed = [d.path for d in s.dependencies if d.changes is None]
    if unparsed:
        lines += ([""] if lines else []) + ["Also changed (not parsed package by package): "
                                             + ", ".join(f"`{p}`" for p in unparsed)]
    quiet = [d.path for d in s.dependencies if d.changes == []]
    if quiet:
        lines += ([""] if lines else []) + ["No version changes found in: " + ", ".join(f"`{p}`" for p in quiet)]
    risky = s.risky_dependencies
    headline = f"{_n(len(rows), 'dependency change')} in {_n(len(s.dependencies), 'file')}"
    if len(risky) == 1:
        headline += ", one of them a major upgrade, downgrade or removal"
    elif risky:
        headline += f", {len(risky)} of them major upgrades, downgrades or removals"
    return Section(75 if risky else 50, "Dependencies", headline, lines)


def _infra(s: Summary) -> Section | None:
    if not s.infra:
        return None
    lines = [f"- {STATUS_WORDS[f.status]} `{f.path}` (+{f.added} / -{f.deleted})" for f in s.infra]
    return Section(60, "Infrastructure and deployment files", f"{_n(len(s.infra), 'infrastructure file')} changed", lines)


def _commits(s: Summary) -> Section | None:
    if not s.commits:
        return None
    flagged = s.attention_commits
    ordered = flagged + [c for c in s.commits if c not in flagged]
    lines = [f"- `{c.sha[:7]}` {c.subject} ({c.author}){' **<- check this one**' if c in flagged else ''}"
             for c in ordered[:MAX_COMMITS]]
    if len(ordered) > MAX_COMMITS:
        lines.append(f"- ... and {len(ordered) - MAX_COMMITS} more")
    headline = f"{_n(len(s.commits), 'commit')} by {_n(len({c.author for c in s.commits}), 'author')}"
    if flagged:
        headline += f"; {len(flagged)} flagged for a revert, hotfix or breaking-change keyword"
    return Section(10, "Commits", headline, lines)


def sections(s: Summary) -> list[Section]:
    built = [fn(s) for fn in (_migrations, _config, _images, _dependencies, _infra, _commits)]
    return sorted((b for b in built if b), key=lambda b: -b.rank)


def render_markdown(s: Summary) -> str:
    added, deleted = sum(f.added for f in s.files), sum(f.deleted for f in s.files)
    out = [f"# Deploy diff: {s.old} -> {s.new}", "",
           f"`{s.old_sha[:7]}` -> `{s.new_sha[:7]}`: {_n(len(s.commits), 'commit')}, "
           f"{_n(len(s.files), 'file')} changed (+{added} / -{deleted})."]
    if s.notes:
        out += [""] + [f"> {note}" for note in s.notes]
    secs = sections(s)
    if secs:
        out += ["", "## What to look at first", ""]
        out += [f"{i}. **{sec.title}**: {sec.headline}" for i, sec in enumerate(secs, 1)]
        other = len(s.files) - len(s.migrations) - len(s.config) - len(s.dependencies) - len(s.infra)
        if other > 0:
            out += ["", f"The other {_n(other, 'changed file')} {'is' if other == 1 else 'are'} "
                        "application code, tests or docs."]
    for sec in secs:
        out += ["", f"## {sec.title}", "", *sec.lines]
    return "\n".join(out) + "\n"


def render_json(s: Summary) -> str:
    doc = asdict(s)
    doc["order"] = [sec.title for sec in sections(s)]
    doc["headlines"] = {sec.title: sec.headline for sec in sections(s)}
    return json.dumps(doc, indent=2) + "\n"


RENDERERS = {"markdown": render_markdown, "json": render_json}


# --- registry image comparison ----------------------------------------------

def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def render_image_markdown(c: ImageComparison) -> str:
    a, b = c.old, c.new
    out = [f"# Image diff: {c.image} {a.ref} -> {b.ref}", ""]
    if c.same_image:
        out += [f"Both refs point at the same image (`{a.digest}`); nothing changed.", ""]
    rows = [("Digest", _cell(a.digest), _cell(b.digest)),
            ("Platform", _cell(a.platform), _cell(b.platform)),
            ("Created", _cell(a.created), _cell(b.created)),
            ("Layers", f"{len(a.layers)} ({_size(a.size)})", f"{len(b.layers)} ({_size(b.size)})")]
    out += [f"| | `{a.ref}` | `{b.ref}` |", "|---|---|---|"]
    out += [f"| {name} | {old} | {new} |" for name, old, new in rows]
    if not c.same_image:
        out += ["", f"{c.shared_layers} of {len(b.layers)} layers in `{b.ref}` are shared with `{a.ref}`"
                    + ("; the base image or an early build step changed." if c.shared_layers == 0 and b.layers
                       else ".")]
    out += ["", "## Labels", ""]
    if not c.labels:
        out.append("No label differences.")
    for k in c.labels:
        if k.kind == "changed":
            out.append(f"- changed `{k.key}`: {_code(k.old)} -> {_code(k.new)}")
        elif k.kind == "added":
            out.append(f"- added `{k.key}` = {_code(k.new)}")
        else:
            out.append(f"- removed `{k.key}` (was {_code(k.old)})")
    return "\n".join(out) + "\n"


def render_image_json(c: ImageComparison) -> str:
    doc = asdict(c)
    doc["same_image"] = c.same_image
    return json.dumps(doc, indent=2) + "\n"


IMAGE_RENDERERS = {"markdown": render_image_markdown, "json": render_image_json}
