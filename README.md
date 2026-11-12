# deploy-diff-summary

Explain what changed between two releases of a git repository: commits, config and environment settings, dependencies, migrations and container image tags, in a Markdown summary with the changes most likely to matter first.

**Status:** working first version (0.1.0). Comparing two git refs in a local repository works today, including key-by-key config diffs, dependency version changes, migration detection and image tag changes in manifests and Dockerfiles. Comparing two tags of a public container image directly in its registry (digest, layers, labels) also works. Comparing Helm releases in a cluster is not built yet.

## Why

When a deploy goes wrong, the first question is "what changed?", and `git log` only answers part of it. The pool size someone raised, the base image that moved and the migration that came along are in the diff, but not in the commit subjects. This tool pulls them out and puts them in order.

It is read-only. It runs `git rev-parse`, `merge-base`, `log`, `diff` and `show`, and nothing else. It never checks out, fetches, deploys or rolls back. In `--image` mode it only sends HTTP GET requests for manifests, image config and an anonymous pull token.

## Install

Requires Python 3.10 or newer and git. The only dependency is PyYAML.

```bash
pip install git+https://github.com/narayana006/deploy-diff-summary
```

## Quick start

In any repository with two tags, branches or commits you want to compare:

```bash
deploy-diff-summary --from v1.41.0 --to v1.42.0
```

To try it without real code, this repository has a script that builds a throwaway demo repository with two release tags:

```bash
git clone https://github.com/narayana006/deploy-diff-summary
cd deploy-diff-summary
pip install -e .
bash examples/make-demo-repo.sh /tmp/deploy-demo
deploy-diff-summary --repo /tmp/deploy-demo --from v1.41.0 --to v1.42.0
```

Output (commit hashes will differ on your machine, because the demo commits are created when you run the script):

```markdown
# Deploy diff: v1.41.0 -> v1.42.0

`8e24acd` -> `aa6199d`: 5 commits, 7 files changed (+16 / -13).

## What to look at first

1. **Database migrations**: 1 migration file (1 added)
2. **Configuration and environment**: 2 config files changed, 6 settings added, removed or changed
3. **Container images**: 3 container image references changed
4. **Dependencies**: 4 dependency changes in 1 file, 2 of them major upgrades, downgrades or removals
5. **Infrastructure and deployment files**: 2 infrastructure files changed
6. **Commits**: 5 commits by 2 authors; 1 flagged for a revert, hotfix or breaking-change keyword

The other 1 changed file is application code, tests or docs.

## Database migrations

- added `db/migrations/0008_add_currency.sql` (+1 / -0)

## Configuration and environment

- `config/production.env` (modified, +3 / -3)
  - added `CURRENCY_CONVERSION_ENABLED` = `true`
  - changed `DB_POOL_SIZE`: `10` -> `50`
  - removed `LEGACY_EXPORT_ENABLED` (was `true`)
  - changed `PAYMENT_API_KEY`: `(hidden)` -> `(hidden)`
- `helm/values-prod.yaml` (modified, +2 / -2)
  - changed `autoscaling.enabled`: `false` -> `true`
  - changed `replicaCount`: `3` -> `6`

## Container images

| Image | From | To | Where |
|---|---|---|---|
| `envoyproxy/envoy` | `v1.30.1` | `v1.31.0` | `deploy/k8s/orders.yaml` |
| `ghcr.io/acme/orders` | `1.41.0` | `1.42.0` | `deploy/k8s/orders.yaml` |
| `python` | `3.11-slim` | `3.12-slim` | `Dockerfile` |

## Dependencies

| Package | From | To | Change | File |
|---|---|---|---|---|
| `flask` | `2.3.3` | `3.0.3` | major upgrade | `requirements.txt` |
| `simplejson` | `3.19.2` | - | removed | `requirements.txt` |
| `orjson` | - | `3.10.7` | added | `requirements.txt` |
| `requests` | `2.31.0` | `2.32.3` | upgrade | `requirements.txt` |

## Infrastructure and deployment files

- modified `Dockerfile` (+1 / -1)
- modified `deploy/k8s/orders.yaml` (+2 / -2)

## Commits

- `2f22e76` hotfix: revert order state validation (Priya Raman) **<- check this one**
- `aa6199d` Release 1.42.0 (Priya Raman)
- `108ef47` Raise DB pool size and enable autoscaling for launch (Marcus Chen)
- `d430312` Upgrade flask to 3.x and switch to orjson (Marcus Chen)
- `a30a8c5` feat: store order currency (Priya Raman)
```

## What it collects

Sections appear in this order when they have content. The order is a fixed judgement about which kinds of change most often explain a bad deploy.

| Order | Section | What is reported |
|---|---|---|
| 1 | Database migrations | Files under `migrations/`, `migrate/`, `alembic/versions/`, `*.sql`, `db/schema.rb`. Edited or removed migrations are called out. |
| 2 | Configuration and environment | `.env*`, `*.properties`, `*.ini`, `values*.yaml`, `application*.yml`, anything under `config/` or `conf/`. Env-style, JSON and single-document YAML files are compared key by key. |
| 3 | Container images | `image:` references in changed YAML files and `FROM` lines in Dockerfiles, with old and new tag or digest. |
| 4 | Dependencies (major upgrade, downgrade or removal present) | Package-level changes from `requirements*.txt`, `package.json` and `go.mod`. |
| 5 | Infrastructure and deployment files | Terraform, Dockerfiles, Compose files, `k8s/`, `helm/`, `charts/`, `deploy/`, CI workflows. |
| 6 | Dependencies (routine upgrades and additions only) | As above. |
| 7 | Commits | Non-merge commits in `--to` that are not in `--from`. Subjects mentioning a revert, hotfix, rollback or breaking change are listed first and marked. |

Values of settings whose name looks secret (containing `pass`, `secret`, `token`, `credential`, `private`, `auth` or `api_key`, or ending in `_key`) are never printed; they show as `(hidden)`. Other config values are printed, so check the summary before pasting it somewhere public.

If `--to` is older than `--from`, the summary says it is a rollback. If the two refs have diverged, it says so and compares the two trees directly.

## Usage

```text
deploy-diff-summary --from REF --to REF [--repo PATH] [-f markdown|json] [-o FILE]
deploy-diff-summary --image NAME --from TAG --to TAG [--platform OS/ARCH] [-f markdown|json] [-o FILE]
```

| Option | Meaning |
|---|---|
| `--from REF` | The release currently running: a tag, branch or commit. Required. |
| `--to REF` | The release being deployed. Required. |
| `--repo PATH` | Path to the git repository. Default: current directory. |
| `--image NAME` | Compare two tags (or `sha256:` digests) of this image in its registry instead of two git refs. |
| `--platform OS/ARCH` | With `--image`: which image to pick from a multi-platform tag. Default: `linux/amd64`. |
| `-f`, `--format` | `markdown` (default) or `json`. |
| `-o`, `--output FILE` | Write the summary to a file instead of stdout. |

Exit codes: `0` on success, `2` when a ref or tag is unknown, the path is not a git repository, or the registry refuses access.

Save release notes to a file:

```bash
deploy-diff-summary --from v1.41.0 --to v1.42.0 -o release-notes.md
```

## Compare two image tags in a registry

When the question is "what is actually different between the image running now and the one we are rolling out", compare the tags in the registry directly. No git checkout or Docker daemon is needed:

```bash
deploy-diff-summary --image ghcr.io/acme/orders --from 1.41.0 --to 1.42.0
```

It reads both manifests (picking `--platform` from multi-platform tags), then each image's config, and reports the digests, creation times, layer counts and compressed sizes, how many layers the new image shares with the old one, and every label that was added, removed or changed. Example output (generated by the test suite's fake registry, so the digests are not real):

```markdown
# Image diff: reg.example/acme/orders 1.41.0 -> 1.42.0

| | `1.41.0` | `1.42.0` |
|---|---|---|
| Digest | `sha256:d24c9d804169893e3586ba49d48b7629c06d17f74f80e921f956695b9a352c2b` | `sha256:cc5b7a0b4de6917002ac510342e397895d70922c388e917693e3f0cfc5c81339` |
| Platform | `linux/amd64` | `linux/amd64` |
| Created | `2026-10-01T10:00:00Z` | `2026-11-01T10:00:00Z` |
| Layers | 3 (3.0 MB) | 4 (4.0 MB) |

2 of 4 layers in `1.42.0` are shared with `1.41.0`.

## Labels

- changed `org.opencontainers.image.revision`: `8e24acd` -> `aa6199d`
- added `release` = `1.42.0`
- removed `team` (was `orders`)
```

If both tags resolve to the same digest, the output says so: a retag with no rebuild is not a code change. If no layers are shared, the base image or an early build step changed. Label values whose name looks secret are hidden as for config files.

Image names follow Docker's rules: `nginx` means `docker.io/library/nginx`, and `localhost:5000/app` is fetched over plain HTTP; every other registry uses HTTPS. Works with Docker Hub, GHCR, Quay, and any registry that speaks the OCI distribution API and hands out anonymous pull tokens for public images.

## Features

- [x] Compares two git refs (tags, branches or commits) in a local repository
- [x] Collects commits, config and environment changes, dependency changes, image tags and migrations
- [x] Plain-English summary ordered by likely impact
- [x] Markdown output for release notes or an incident channel, plus JSON
- [x] Read-only: never deploys or rolls back
- [x] Compare container image tags in a registry (image digests and labels)
- [ ] Compare Helm releases in a cluster

## Limitations

- Works on one local git repository. Both refs must already be fetched; the tool does not fetch for you.
- The impact order is fixed by category, not learned from your incident history.
- Files are classified by path patterns that are built in and not yet configurable.
- Key-by-key config diffs cover env-style files, JSON and single-document YAML. Multi-document YAML, Helm templates and settings written as code are listed with line counts only.
- Environment variables set inside Kubernetes manifests (`env:` blocks) are not compared key by key; the manifest shows up under infrastructure files.
- Image detection reads `image:` lines and `FROM` lines as text. Helm charts that split `repository:` and `tag:` show the tag change under configuration, not under container images. Images built from variables are skipped.
- Dependency parsing covers `requirements*.txt`, `package.json` and `go.mod`. Other manifests and all lock files are listed as changed without a package breakdown.
- Renames appear as a removal plus an addition.
- `--image` works with public images only. It does not read `docker login` credentials, so private repositories fail with an "only public images are supported" error. It compares one image per run and does not diff file contents inside layers.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests build small git repositories in a temporary directory, so git must be installed.

## Contributing

Issues and pull requests are welcome. Parsers for more dependency manifests and reports of files that landed in the wrong section are especially useful.

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
