"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .analyze import summarize
from .gitrepo import GitError, Repo
from .render import RENDERERS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deploy-diff-summary",
        description="Summarise what changed between two git refs, most impactful changes first. Read-only.",
    )
    p.add_argument("--from", dest="old", required=True, metavar="REF",
                   help="the release currently running (tag, branch or commit)")
    p.add_argument("--to", dest="new", required=True, metavar="REF", help="the release being deployed")
    p.add_argument("--repo", default=".", help="path to the git repository (default: current directory)")
    p.add_argument("-f", "--format", choices=sorted(RENDERERS), default="markdown")
    p.add_argument("-o", "--output", help="write the summary to this file instead of stdout")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = summarize(Repo(args.repo), args.old, args.new)
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    text = RENDERERS[args.format](summary)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
