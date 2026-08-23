#!/usr/bin/env python3
"""The service count the docs state matches the one stack.toml defines.

`19 services` appears in this repo's prose and in several documents outside it.
The manifest is the only place that knows, so the prose is checked against it
rather than trusted (REPO-R18).

Run from the repo root. `--self-test` proves the check fails when it should.
Exit 0 = every stated count is right, 1 = one is not.
"""
from __future__ import annotations

import pathlib
import re
import sys
import tempfile
import tomllib

MANIFEST = "stack.toml"
README = "README.md"
AGENTS = "AGENTS.md"
DOCS = (README, AGENTS)

COUNTED = re.compile(r"\b(\d+)\s+services\b")
SPELLED = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s+services\b",
    re.I,
)
WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
]


def defined(root: pathlib.Path) -> int:
    """How many services the manifest declares."""
    manifest = tomllib.loads((root / MANIFEST).read_text(encoding="utf-8"))
    return len(manifest["service"])


def disagreeing(line: str, actual: int, word: str):
    """Every count on one line that is not the one the manifest defines."""
    for stated in COUNTED.findall(line):
        if int(stated) != actual:
            yield stated
    for stated in SPELLED.findall(line):
        if stated.lower() != word:
            yield stated


def stated_counts(root: pathlib.Path, actual: int):
    """Every disagreeing count in the docs, as (location, stated) pairs."""
    word = WORDS[actual] if actual < len(WORDS) else ""
    for name in DOCS:
        doc = root / name
        if not doc.is_file():
            continue
        for number, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for stated in disagreeing(line, actual, word):
                yield f"{name}:{number}", stated


def check(root: pathlib.Path) -> list[str]:
    actual = defined(root)
    return [
        f"{where}: says {stated} services, {MANIFEST} defines {actual}"
        for where, stated in stated_counts(root, actual)
    ]


def self_test() -> int:
    """A doc that states the wrong count is rejected; the right one passes."""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / MANIFEST).write_text(
            '[[service]]\nname = "A"\n\n[[service]]\nname = "B"\n', encoding="utf-8"
        )
        for prose, want in (("3 services", True), ("three services", True),
                            ("2 services", False), ("two services", False)):
            (root / README).write_text(f"The stack is {prose}.\n", encoding="utf-8")
            if bool(check(root)) is not want:
                print(f"::error::self-test: {prose!r} was not judged as expected")
                return 1
    print("docs check self-test: a wrong count fails, a right one passes")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    root = pathlib.Path(".")
    problems = check(root)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print(f"docs: every stated service count is {defined(root)}, as {MANIFEST} defines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
