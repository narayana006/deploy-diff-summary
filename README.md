# deploy-diff-summary

Explain what actually changed between two releases: commits, config, container images and infrastructure, in plain English.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool.

**The problem it solves:** When a deploy goes wrong, the first question is what changed, and the answer is scattered across several systems.

## Who it is for

On-call engineers and release managers who need to know what a deploy changed.

## How it will work

1. You name two versions: git refs, image tags or releases.
2. The tool gathers the commits, config changes, image changes and migrations between them.
3. It writes a plain-English summary with the changes most likely to matter first.

## Planned features

Unticked items are not built yet.

- [ ] Compares two git refs, image tags or Helm releases
- [ ] Collects commits, config and environment changes, image digests and migrations
- [ ] Plain-English summary ordered by likely impact
- [ ] Markdown output for release notes or an incident channel
- [ ] Read-only: never deploys or rolls back

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
deploy-diff-summary --from v1.41.0 --to v1.42.0
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## License

[MIT](LICENSE)
