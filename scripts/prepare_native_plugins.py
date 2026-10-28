#!/usr/bin/env python3
"""Check the pinned Microduck plugin source patch; --apply edits source only."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.microduck_tools.native import PreparationError, TARGETS, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--apply", action="store_true", help="write reviewed native source changes; no build/install/restart")
    parser.add_argument("--print-diff", action="store_true", help="print unified source diff after the JSON plan")
    args = parser.parse_args()
    try:
        plan, diff = prepare(args.repo, args.target, apply=args.apply)
    except (PreparationError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.print_diff:
        print(diff, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
