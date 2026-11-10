import json
import subprocess
from pathlib import Path

import pytest

from deploy_diff_summary import cli
from deploy_diff_summary.analyze import matches, summarize, MIGRATION_PATTERNS, CONFIG_PATTERNS, INFRA_PATTERNS
from deploy_diff_summary.gitrepo import GitError, Repo
from deploy_diff_summary.parsers import (HIDDEN, classify_version_change, diff_deps, diff_images, diff_keys,
                                         find_images, flatten, parse_env, parse_go_mod, parse_package_json,
                                         parse_requirements, parse_structured, split_image)
from deploy_diff_summary.render import render_json, render_markdown, sections

DEMO_SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "make-demo-repo.sh"


# --- config parsing ---------------------------------------------------------

def test_parse_env_handles_comments_quotes_and_export():
    text = '# comment\n\nA=1\nexport B="two words"\nC = \'x\'\n[section]\nnot a pair\nURL=http://h/?a=b\n'
    assert parse_env(text) == {"A": "1", "B": "two words", "C": "x", "URL": "http://h/?a=b"}


def test_flatten_nested_structures():
    assert flatten({"a": {"b": 1, "c": [True, None]}, "d": "x"}) == {
        "a.b": "1", "a.c[0]": "true", "a.c[1]": "null", "d": "x"}


def test_parse_structured_by_file_type():
    assert parse_structured("config/app.json", '{"db": {"pool": 5}}') == {"db.pool": "5"}
    assert parse_structured("helm/values.yaml", "image:\n  tag: 1.2\n") == {"image.tag": "1.2"}
    assert parse_structured(".env.production", "A=1\n") == {"A": "1"}
    assert parse_structured("config/removed.yaml", "") == {}
    assert parse_structured("k8s/multi.yaml", "a: 1\n---\nb: 2\n") is None  # multi-document
    assert parse_structured("helm/templates/x.yaml", "a: {{ .Values.x }}\n") is None  # templated
    assert parse_structured("config/broken.json", "{oops") is None
    assert parse_structured("app/settings.py", "DEBUG = True\n") is None


def test_diff_keys_reports_added_removed_changed_and_hides_secrets():
    old = {"POOL": "10", "OLD": "1", "DB_PASSWORD": "hunter2", "SAME": "x", "db.api_key": "abc"}
    new = {"POOL": "50", "NEW": "yes", "DB_PASSWORD": "hunter3", "SAME": "x", "AUTH_TOKEN": "t"}
    got = {(c.key, c.kind, c.old, c.new) for c in diff_keys(old, new)}
    assert got == {
        ("POOL", "changed", "10", "50"),
        ("OLD", "removed", "1", None),
        ("NEW", "added", None, "yes"),
        ("DB_PASSWORD", "changed", HIDDEN, HIDDEN),
        ("db.api_key", "removed", HIDDEN, None),
        ("AUTH_TOKEN", "added", None, HIDDEN),
    }
    assert "hunter" not in repr(diff_keys(old, new))


# --- dependency parsing -----------------------------------------------------

def test_parse_requirements():
    text = ("# pinned\nFlask==2.3.3\nrequests[socks]>=2.31,<3  # http\n-r base.txt\n"
            "typing_extensions ; python_version < '3.11'\n\n")
    assert parse_requirements(text) == {"flask": "2.3.3", "requests": ">=2.31,<3", "typing-extensions": "any"}


def test_parse_package_json_and_go_mod():
    pkg = json.dumps({"dependencies": {"react": "^18.2.0"}, "devDependencies": {"jest": "~29.7.0"}})
    assert parse_package_json(pkg) == {"react": "^18.2.0", "jest": "~29.7.0"}
    assert parse_package_json("{broken") == {}
    gomod = ("module example.com/svc\n\ngo 1.22\n\nrequire (\n\tgithub.com/lib/pq v1.10.9\n"
             "\tgolang.org/x/sync v0.7.0 // indirect\n)\n\nrequire github.com/google/uuid v1.6.0\n")
    assert parse_go_mod(gomod) == {"github.com/lib/pq": "v1.10.9", "golang.org/x/sync": "v0.7.0",
                                   "github.com/google/uuid": "v1.6.0"}


@pytest.mark.parametrize("old, new, kind", [
    ("2.3.3", "3.0.3", "major upgrade"),
    ("^18.2.0", "^18.3.1", "upgrade"),
    ("v1.10.9", "v1.9.0", "downgrade"),
    ("1.9", "1.10", "upgrade"),
    ("main", "develop", "changed"),
    ("==1.0", ">=1.0", "changed"),
])
def test_classify_version_change(old, new, kind):
    assert classify_version_change(old, new) == kind


def test_diff_deps():
    got = {(c.name, c.kind) for c in diff_deps({"a": "1.0", "b": "1.0", "c": "1.0"},
                                               {"a": "2.0", "c": "1.0", "d": "0.1"})}
    assert got == {("a", "major upgrade"), ("b", "removed"), ("d", "added")}


# --- image parsing ----------------------------------------------------------

@pytest.mark.parametrize("ref, expected", [
    ("nginx", ("nginx", "latest (implicit)")),
    ("ghcr.io/acme/orders:1.42.0", ("ghcr.io/acme/orders", "1.42.0")),
    ("localhost:5000/app", ("localhost:5000/app", "latest (implicit)")),
    ("localhost:5000/app:2", ("localhost:5000/app", "2")),
    ("app@sha256:0123456789abcdef0123", ("app", "sha256:0123456789ab")),
    ("app:1.0@sha256:0123456789abcdef0123", ("app", "sha256:0123456789ab")),
])
def test_split_image(ref, expected):
    assert split_image(ref) == expected


def test_find_images_in_manifests_and_dockerfiles():
    manifest = ('containers:\n  - name: a\n    image: "ghcr.io/acme/a:1.0"\n  - image: redis:7\n'
                "    imagePullPolicy: Always\n  - image: {{ .Values.image }}\n")
    assert find_images("deploy/a.yaml", manifest) == {"ghcr.io/acme/a": {"1.0"}, "redis": {"7"}}
    docker = "FROM --platform=linux/amd64 golang:1.22 AS build\nFROM scratch\nfrom alpine:3.20\n"
    assert find_images("Dockerfile.prod", docker) == {"golang": {"1.22"}, "alpine": {"3.20"}}
    assert find_images("notes.txt", "image: nginx:1") == {}


def test_diff_images():
    old = {"app": {"1.0"}, "redis": {"7"}, "gone": {"1"}}
    new = {"app": {"1.1"}, "redis": {"7"}, "fresh": {"2"}}
    got = {(c.image, c.old, c.new) for c in diff_images(old, new)}
    assert got == {("app", "1.0", "1.1"), ("gone", "1", None), ("fresh", None, "2")}


# --- path classification ----------------------------------------------------

def test_path_classification_patterns():
    assert matches("svc/orders/migrations/0002_x.py", MIGRATION_PATTERNS)
    assert matches("db/x.sql", MIGRATION_PATTERNS)
    assert matches(".env.production", CONFIG_PATTERNS) and matches("a/config/x.yml", CONFIG_PATTERNS)
    assert matches("infra/main.tf", INFRA_PATTERNS) and matches("a/Dockerfile.dev", INFRA_PATTERNS)
    assert not matches("src/app.py", MIGRATION_PATTERNS + CONFIG_PATTERNS + INFRA_PATTERNS)


# --- end to end against real git repositories -------------------------------

def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test Author", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True, text=True).stdout


def write(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    write(r, "src/app.py", "print('v1')\n")
    write(r, ".env.production", "POOL=10\nAPI_SECRET=aaa\n")
    write(r, "deploy/app.yaml", "containers:\n  - image: acme/app:1.0\n")
    write(r, "package.json", json.dumps({"dependencies": {"express": "^4.18.0"}}))
    write(r, "db/migrations/001_init.sql", "CREATE TABLE a (id INT);\n")
    write(r, "yarn.lock", "# lock v1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "release one")
    git(r, "tag", "v1")
    write(r, "src/app.py", "print('v2')\n")
    write(r, ".env.production", "POOL=20\nAPI_SECRET=bbb\n")
    write(r, "deploy/app.yaml", "containers:\n  - image: acme/app:2.0\n")
    write(r, "package.json", json.dumps({"dependencies": {"express": "^5.0.0"}}))
    write(r, "db/migrations/001_init.sql", "CREATE TABLE a (id BIGINT);\n")
    write(r, "db/migrations/002_more.sql", "ALTER TABLE a ADD b INT;\n")
    write(r, "yarn.lock", "# lock v2\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "fix!: breaking schema change")
    write(r, "README.md", "docs\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "docs only")
    git(r, "tag", "v2")
    return r


def test_summarize_collects_every_category(repo):
    s = summarize(Repo(str(repo)), "v1", "v2")
    assert [c.subject for c in s.commits] == ["docs only", "fix!: breaking schema change"]
    assert [c.subject for c in s.attention_commits] == ["fix!: breaking schema change"]
    assert {(f.path, f.status) for f in s.migrations} == {
        ("db/migrations/001_init.sql", "M"), ("db/migrations/002_more.sql", "A")}
    (cfg,) = s.config
    assert [(k.key, k.old, k.new) for k in cfg.keys] == [("API_SECRET", HIDDEN, HIDDEN), ("POOL", "10", "20")]
    assert [(u.change.image, u.change.old, u.change.new, u.files) for u in s.images] == [
        ("acme/app", "1.0", "2.0", ["deploy/app.yaml"])]
    deps = {d.path: d.changes for d in s.dependencies}
    assert deps["yarn.lock"] is None
    assert [(c.name, c.kind) for c in deps["package.json"]] == [("express", "major upgrade")]
    assert [f.path for f in s.infra] == ["deploy/app.yaml"]
    assert len(s.files) == 8 and s.notes == []


def test_markdown_orders_sections_by_impact_and_never_leaks_secrets(repo):
    s = summarize(Repo(str(repo)), "v1", "v2")
    md = render_markdown(s)
    titles = [sec.title for sec in sections(s)]
    assert titles == ["Database migrations", "Configuration and environment", "Container images",
                      "Dependencies", "Infrastructure and deployment files", "Commits"]
    positions = [md.index(f"## {t}") for t in titles]
    assert positions == sorted(positions)
    assert md.startswith("# Deploy diff: v1 -> v2\n")
    assert "existing migrations were edited or removed" in md
    assert "changed `POOL`: `10` -> `20`" in md
    assert "| `acme/app` | `1.0` | `2.0` | `deploy/app.yaml` |" in md
    assert "| `express` | `^4.18.0` | `^5.0.0` | major upgrade | `package.json` |" in md
    assert "**<- check this one**" in md
    assert "aaa" not in md and "bbb" not in md
    assert "aaa" not in render_json(s) and "bbb" not in render_json(s)


def test_minor_only_dependency_changes_rank_below_infra(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    write(r, "requirements.txt", "requests==2.31.0\n")
    write(r, "infra/main.tf", "# a\n")
    git(r, "add", "-A"); git(r, "commit", "-q", "-m", "one")
    write(r, "requirements.txt", "requests==2.32.3\n")
    write(r, "infra/main.tf", "# b\n")
    git(r, "add", "-A"); git(r, "commit", "-q", "-m", "two")
    s = summarize(Repo(str(r)), "HEAD~1", "HEAD")
    assert [sec.title for sec in sections(s)] == [
        "Infrastructure and deployment files", "Dependencies", "Commits"]


def test_same_ref_rollback_and_diverged_notes(repo):
    same = summarize(Repo(str(repo)), "v2", "v2")
    assert same.files == [] and "same commit" in same.notes[0]
    assert "nothing changed" in render_markdown(same)
    rollback = summarize(Repo(str(repo)), "v2", "v1")
    assert rollback.commits == [] and "rollback" in rollback.notes[0]
    assert {f.status for f in rollback.migrations} == {"M", "D"}
    git(repo, "checkout", "-q", "-b", "side", "v1")
    write(repo, "side.txt", "x\n")
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "side work")
    diverged = summarize(Repo(str(repo)), "v2", "side")
    assert "diverged" in diverged.notes[0]
    assert [c.subject for c in diverged.commits] == ["side work"]


def test_tool_does_not_modify_the_repository(repo):
    def snapshot():
        return (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"),
                git(repo, "for-each-ref"), git(repo, "stash", "list"))
    before = snapshot()
    assert cli.main(["--repo", str(repo), "--from", "v1", "--to", "v2"]) == 0
    assert snapshot() == before
    with pytest.raises(GitError, match="refusing"):
        Repo(str(repo))._git("checkout", "v1")


def test_cli_output_file_json_and_errors(repo, tmp_path, capsys):
    out = tmp_path / "notes.md"
    assert cli.main(["--repo", str(repo), "--from", "v1", "--to", "v2", "-o", str(out)]) == 0
    assert capsys.readouterr().out == ""
    assert out.read_text().startswith("# Deploy diff: v1 -> v2")
    assert cli.main(["--repo", str(repo), "--from", "v1", "--to", "v2", "-f", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["order"][0] == "Database migrations" and len(doc["commits"]) == 2
    assert cli.main(["--repo", str(repo), "--from", "v1", "--to", "v9"]) == 2
    assert "unknown ref 'v9'" in capsys.readouterr().err
    assert cli.main(["--repo", str(tmp_path), "--from", "a", "--to", "b"]) == 2
    assert "not a git repository" in capsys.readouterr().err


def test_demo_script_produces_the_documented_summary(tmp_path, capsys):
    demo = tmp_path / "demo"
    subprocess.run(["bash", str(DEMO_SCRIPT), str(demo)], check=True, capture_output=True)
    assert cli.main(["--repo", str(demo), "--from", "v1.41.0", "--to", "v1.42.0"]) == 0
    md = capsys.readouterr().out
    assert "5 commits, 7 files changed" in md
    assert "changed `replicaCount`: `3` -> `6`" in md
    assert "changed `autoscaling.enabled`: `false` -> `true`" in md
    assert "| `python` | `3.11-slim` | `3.12-slim` | `Dockerfile` |" in md
    assert "example-key" not in md
