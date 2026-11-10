"""Pure functions that compare the before and after content of one file."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import yaml

SECRET_KEY = re.compile(r"pass|secret|token|credential|private|auth|api[_-]?key|_key$|^key$", re.I)
HIDDEN = "(hidden)"


# --- configuration keys -----------------------------------------------------

@dataclass(frozen=True)
class KeyChange:
    key: str
    kind: str  # added | removed | changed
    old: str | None = None
    new: str | None = None


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE files: .env, .properties, simple .ini (sections are ignored)."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;[" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Flatten nested mappings and lists to dotted keys with scalar values."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list):
        out = {}
        for i, v in enumerate(value):
            out.update(flatten(v, f"{prefix}[{i}]"))
        return out
    if value is None or isinstance(value, bool):
        return {prefix: json.dumps(value)}  # null / true / false, as written in the file
    return {prefix: str(value)}


def parse_structured(path: str, text: str) -> dict[str, str] | None:
    """Flattened keys for a config file, or None when it cannot be parsed as key/values."""
    name = path.rsplit("/", 1)[-1].lower()
    if not text.strip():
        return {}
    try:
        if name.endswith(".json"):
            return flatten(json.loads(text))
        if name.endswith((".yml", ".yaml")):
            docs = list(yaml.safe_load_all(text))
            if len(docs) != 1 or not isinstance(docs[0], (dict, list)):
                return None
            return flatten(docs[0])
    except (json.JSONDecodeError, yaml.YAMLError):
        return None
    if name.startswith(".env") or name.endswith((".env", ".properties", ".ini", ".conf", ".cfg")):
        return parse_env(text)
    return None


def _show(key: str, value: str) -> str:
    return HIDDEN if SECRET_KEY.search(key.rsplit(".", 1)[-1]) else value


def diff_keys(old: dict[str, str], new: dict[str, str]) -> list[KeyChange]:
    """Key-level differences. Values of secret-looking keys are never included."""
    changes = []
    for key in sorted(set(old) | set(new)):
        if key not in old:
            changes.append(KeyChange(key, "added", new=_show(key, new[key])))
        elif key not in new:
            changes.append(KeyChange(key, "removed", old=_show(key, old[key])))
        elif old[key] != new[key]:
            changes.append(KeyChange(key, "changed", _show(key, old[key]), _show(key, new[key])))
    return changes


# --- dependencies -----------------------------------------------------------

@dataclass(frozen=True)
class DepChange:
    name: str
    old: str | None
    new: str | None
    kind: str  # added | removed | major upgrade | upgrade | downgrade | changed


_REQ_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(.*)$")
_GO_REQ = re.compile(r"^\s*(?:require\s+)?([\w.~-]+\.[\w./~-]+)\s+(v[\w.+-]+)")


def parse_requirements(text: str) -> dict[str, str]:
    deps = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_LINE.match(line)
        if m:
            deps[m.group(1).lower().replace("_", "-")] = m.group(2).replace(" ", "").removeprefix("==") or "any"
    return deps


def parse_package_json(text: str) -> dict[str, str]:
    try:
        doc = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    deps = {}
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = doc.get(section) if isinstance(doc, dict) else None
        if isinstance(block, dict):
            deps.update({str(k): str(v) for k, v in block.items()})
    return deps


def parse_go_mod(text: str) -> dict[str, str]:
    deps = {}
    for line in text.splitlines():
        m = _GO_REQ.match(line.split("//", 1)[0])
        if m and m.group(1) != "go":
            deps[m.group(1)] = m.group(2)
    return deps


def dependency_parser(path: str):
    name = path.rsplit("/", 1)[-1]
    if name == "package.json":
        return parse_package_json
    if name == "go.mod":
        return parse_go_mod
    if re.fullmatch(r"requirements.*\.(txt|in)", name):
        return parse_requirements
    return None


def version_tuple(version: str) -> tuple[int, ...] | None:
    m = re.search(r"\d+(?:\.\d+)*", version)
    return tuple(int(p) for p in m.group(0).split(".")) if m else None


def classify_version_change(old: str, new: str) -> str:
    a, b = version_tuple(old), version_tuple(new)
    if a is None or b is None or a == b:
        return "changed"
    if b < a:
        return "downgrade"
    return "major upgrade" if b[0] != a[0] else "upgrade"


def diff_deps(old: dict[str, str], new: dict[str, str]) -> list[DepChange]:
    changes = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            changes.append(DepChange(name, None, new[name], "added"))
        elif name not in new:
            changes.append(DepChange(name, old[name], None, "removed"))
        elif old[name] != new[name]:
            changes.append(DepChange(name, old[name], new[name], classify_version_change(old[name], new[name])))
    return changes


# --- container images -------------------------------------------------------

@dataclass(frozen=True)
class ImageChange:
    image: str
    old: str | None
    new: str | None


_YAML_IMAGE = re.compile(r"^\s*-?\s*image:\s*[\"']?([^\s\"'#{}]+)", re.M)
_DOCKER_FROM = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?([^\s{}$]+)", re.M | re.I)


def split_image(ref: str) -> tuple[str, str]:
    """Split 'registry:5000/app:1.2' into ('registry:5000/app', '1.2')."""
    if "@" in ref:
        name, digest = ref.split("@", 1)
        algo, _, hexpart = digest.partition(":")
        return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name, f"{algo}:{hexpart[:12]}"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest (implicit)"


def find_images(path: str, text: str) -> dict[str, set[str]]:
    name = path.rsplit("/", 1)[-1].lower()
    if name.startswith("dockerfile") or name.endswith(".dockerfile"):
        refs = [r for r in _DOCKER_FROM.findall(text) if r.lower() != "scratch"]
    elif name.endswith((".yml", ".yaml")):
        refs = _YAML_IMAGE.findall(text)
    else:
        return {}
    images: dict[str, set[str]] = {}
    for ref in refs:
        image, tag = split_image(ref)
        images.setdefault(image, set()).add(tag)
    return images


def diff_images(old: dict[str, set[str]], new: dict[str, set[str]]) -> list[ImageChange]:
    changes = []
    for image in sorted(set(old) | set(new)):
        before, after = old.get(image, set()), new.get(image, set())
        if before == after:
            continue
        gone, came = sorted(before - after), sorted(after - before)
        changes.append(ImageChange(image, ", ".join(gone) or None, ", ".join(came) or None))
    return changes
