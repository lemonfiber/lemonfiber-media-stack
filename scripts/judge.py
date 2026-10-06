#!/usr/bin/env python3
"""The lemonfiber commit this stack's claims are held to.

`.github/lemonfiber-judge` names it, once, for the two workflows that need it:
`validate`'s `claims` job builds lemonfiber's judge at that commit, and `pins`
re-records a moved pin's claims with the capability vocabulary that commit
publishes. Read here rather than in each workflow, so both read it the same way
and a file that names anything but one full commit is refused rather than
checked out.

    python3 scripts/judge.py                  # the commit
    python3 scripts/judge.py --github-output  # commit=<sha>, for $GITHUB_OUTPUT

`--self-test` proves the reading, offline.
Exit 0 = one commit named, 1 = not.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
JUDGE = ROOT / ".github" / "lemonfiber-judge"
COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")


def named(text: str) -> str:
    """The one commit `text` names, beside its comments and blank lines.

    Raises ValueError for anything else: none, several, or a value that is not
    a full commit hash. A branch or a short hash would move under the stack, and
    what the stack is held to moves only when this file does.
    """
    values = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if len(values) != 1:
        raise ValueError(f"names {len(values)} values where it names one commit")
    if not COMMIT.match(values[0]):
        raise ValueError(f"names {values[0]!r}, which is not a full commit hash")
    return values[0]


def self_test() -> int:
    sha = "07fa30cbac80749e9b101d47adf14d191afe200c"
    assert named(f"# why\n\n{sha}\n") == sha
    for refused in ("", "# only a comment\n", "main\n", sha[:12], f"{sha}\n{sha}\n", sha.upper()):
        try:
            named(refused)
        except ValueError:
            continue
        raise AssertionError(f"accepted {refused!r}")
    named(JUDGE.read_text(encoding="utf-8"))
    print("self-test: one full commit read, and none, several, a branch or a short hash refused")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--github-output", action="store_true", help="print commit=<sha>")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    try:
        commit = named(JUDGE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        print(f"::error::{JUDGE.relative_to(ROOT)} {unreadable}", file=sys.stderr)
        return 1
    print(f"commit={commit}" if args.github_output else commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
