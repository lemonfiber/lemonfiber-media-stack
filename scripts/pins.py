#!/usr/bin/env python3
"""Move each pin to the newest release of its own major, by tag and digest together.

A pin is `image:tag@digest`, written twice: in `stack.toml` as `tag` and `digest`,
and in the service's compose fragment as the reference Compose pulls. This finds,
for each service, the newest tag the registry publishes that

  - is spelled the way the current tag is spelled — the same prefix, the same
    number of dotted numbers and the same suffix, so `v3.41.3` follows `v3.40.0`
    and `4.5.5` follows `4.5.1`, while `12.0-rc7`, `4.0.16-develop` and
    `4.5.5.1234-ls200` are other spellings and are never taken;
  - has the same first number as the current tag, which is the major. A major
    carries breaking changes and is the operator's decision (`E1-R10`), so a pin
    never crosses one here;

and resolves it to the digest of its multi-architecture index (`E1-R1`). A tag
that stays where it is can still move: publishers rebuild a release on a patched
base image under the same tag, and the digest follows that rebuild too.

`last_release` is read from upstream's latest release when the pin moves
(`F2-R14`). Where upstream's date is the one already recorded, the pin is
re-affirmed with a `Pin-reviewed:` trailer on the commit; where it cannot be
read, the move is still proposed and `check_manifest_change.py` names what is
missing, because a date this did not read is not one it will write.

    python3 scripts/pins.py --plan               # JSON: every pin that would move
    python3 scripts/pins.py --apply sonarr ...   # rewrite those pins; JSON: what moved
    python3 scripts/pins.py --self-test          # offline
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tomllib

import registry
from check_releases import github_latest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACK_TOML = ROOT / "stack.toml"
COMPOSE_DIR = ROOT / "compose"
REQUIRED = {("linux", "amd64"), ("linux", "arm64")}

def version_of(tag: str) -> tuple[str, tuple[int, ...], str] | None:
    """A tag's prefix, numbers and suffix, or None for a tag with no number in it.

    The prefix is everything before the first digit; the numbers are the run of
    digits and single dots after it; the suffix is whatever follows the last
    number. Read by hand rather than by a pattern, so a long tag costs one pass.
    """
    start = next((at for at, char in enumerate(tag) if char.isdigit()), None)
    if start is None:
        return None
    numbers, current, at = [], "", start
    while at < len(tag):
        char = tag[at]
        if char.isdigit():
            current += char
        elif char == "." and current and at + 1 < len(tag) and tag[at + 1].isdigit():
            numbers.append(int(current))
            current = ""
        else:
            break
        at += 1
    numbers.append(int(current))
    return tag[:start], tuple(numbers), tag[at:]


def newest_in_major(current: str, tags: list[str]) -> str:
    """The newest tag spelled like `current` and in its major; `current` if none is newer."""
    mine = version_of(current)
    if mine is None:
        return current
    prefix, numbers, suffix = mine
    best, best_numbers = current, numbers
    for tag in tags:
        theirs = version_of(tag)
        if theirs is None:
            continue
        their_prefix, their_numbers, their_suffix = theirs
        if (their_prefix, their_suffix, len(their_numbers)) != (prefix, suffix, len(numbers)):
            continue
        if their_numbers[0] != numbers[0]:
            continue
        if their_numbers > best_numbers:
            best, best_numbers = tag, their_numbers
    return best


def rewrite_manifest(text: str, sid: str, tag: str, digest: str, last_release: str) -> str:
    """`stack.toml` with one service's `tag`, `digest` and `last_release` replaced.

    Edited as text rather than re-serialised, so every comment and every other
    line stays byte for byte what it was. A `digest` line the block does not
    have yet goes directly under `tag`.
    """
    blocks = re.split(r"(?m)^(?=\[\[)", text)
    for number, block in enumerate(blocks):
        if not block.startswith("[[service]]") or not re.search(rf'(?m)^id = "{re.escape(sid)}"$', block):
            continue
        block = re.sub(r'(?m)^tag = ".*"$', f'tag = "{tag}"', block, count=1)
        if re.search(r'(?m)^digest = ".*"$', block):
            block = re.sub(r'(?m)^digest = ".*"$', f'digest = "{digest}"', block, count=1)
        else:
            block = re.sub(r'(?m)^(tag = ".*")$', rf'\1\ndigest = "{digest}"', block, count=1)
        block = re.sub(r'(?m)^last_release = ".*"$', f'last_release = "{last_release}"', block, count=1)
        blocks[number] = block
        return "".join(blocks)
    raise KeyError(f"stack.toml has no [[service]] with id {sid!r}")


def rewrite_compose(text: str, image: str, reference: str) -> tuple[str, int]:
    """A compose fragment with `image`'s reference replaced, and how many lines changed."""
    return re.subn(rf"(?m)^(\s*image:\s*){re.escape(image)}(?::|@)\S*$", rf"\g<1>{reference}", text)


def plan(only: set[str]) -> tuple[list[dict], list[str]]:
    """Every pin that would move, and every service whose next pin could not be read."""
    manifest = tomllib.loads(STACK_TOML.read_text(encoding="utf-8"))
    moves, problems = [], []
    for service in manifest["service"]:
        sid = service["id"]
        if only and sid not in only:
            continue
        published, problem = registry.tags(service["image"])
        if problem:
            problems.append(f"{sid}: its tags could not be listed: {problem}")
            continue
        tag = newest_in_major(service["tag"], published)
        digest, platforms, problem = registry.resolve(service["image"], tag)
        if problem:
            problems.append(f"{sid}: {tag} could not be resolved: {problem}")
            continue
        if missing := REQUIRED - platforms:
            listed = ", ".join(f"{os_}/{arch}" for os_, arch in sorted(missing))
            problems.append(f"{sid}: {tag} does not publish {listed}, so it is not taken (F2-R6)")
            continue
        if (tag, digest) == (service["tag"], service.get("digest")):
            continue
        latest = github_latest(service["upstream"])
        moves.append({
            "id": sid,
            "image": service["image"],
            "from": {"tag": service["tag"], "digest": service.get("digest", "")},
            "to": {"tag": tag, "digest": digest},
            "last_release": {"was": service["last_release"], "now": latest.isoformat() if latest else ""},
        })
    return moves, problems


def apply(moves: list[dict]) -> list[str]:
    """Write each move into `stack.toml` and its compose fragment; the `Pin-reviewed` ids.

    A service whose tag moved and whose upstream date is the one already recorded
    was reviewed and needed no new date, so it is named for the trailer. One whose
    date could not be read keeps what it had and is not named.
    """
    text = STACK_TOML.read_text(encoding="utf-8")
    reaffirmed = []
    for move in moves:
        was, now = move["last_release"]["was"], move["last_release"]["now"]
        text = rewrite_manifest(text, move["id"], move["to"]["tag"], move["to"]["digest"], now or was)
        if move["to"]["tag"] != move["from"]["tag"] and now == was:
            reaffirmed.append(move["id"])
        reference = f"{move['image']}:{move['to']['tag']}@{move['to']['digest']}"
        changed = 0
        for fragment in sorted(COMPOSE_DIR.glob("*.yml")):
            body, count = rewrite_compose(fragment.read_text(encoding="utf-8"), move["image"], reference)
            if count:
                with fragment.open("w", encoding="utf-8") as handle:
                    handle.write(body)
                changed += count
        if changed != 1:
            raise SystemExit(f"{move['id']}: {changed} compose lines name {move['image']}, not one")
    with STACK_TOML.open("w", encoding="utf-8") as handle:
        handle.write(text)
    return reaffirmed


# A newer tag in the same spelling and major, used by more than one self-test case.
UNPACKERR_NEXT = "release-0.15.2"


def choice_problems() -> list[str]:
    """Which tag is taken from a registry's list, and how a tag is read."""
    problems = []
    published = [
        "latest", "4.0.15", "4.0.20", "4.0.21-develop", "4.0.20.3014-ls300", "5.0.0",
        "v3.41.3", "3.99.0", "10.11.11", "12.1", "12.0-rc7", UNPACKERR_NEXT, "release-1.0.0",
        "V3.1.4", "V4.0.0", "0.64.2", "0.65.0",
    ]
    for current, wanted in (
        ("4.0.15", "4.0.20"),         # a newer patch, not the next major nor another spelling
        ("v3.40.0", "v3.41.3"),       # the prefix is part of the spelling
        ("10.10.3", "10.11.11"),      # a minor within the major; 12.1 is another major
        ("release-0.14.5", UNPACKERR_NEXT),
        ("V3.0.4", "V3.1.4"),
        ("0.64.0", "0.65.0"),         # 0 is a major like any other
        ("4.0.20", "4.0.20"),         # already newest
        ("12.1", "12.1"),             # two numbers, and nothing newer in 12 spelled that way
        ("nightly", "nightly"),       # no number to compare
    ):
        if (got := newest_in_major(current, published)) != wanted:
            problems.append(f"from {current}: took {got}, wanted {wanted}")

    for tag, wanted in (
        ("v3.40.0", ("v", (3, 40, 0), "")),
        ("4.5.5.1234-ls200", ("", (4, 5, 5, 1234), "-ls200")),
        ("12.0-rc7", ("", (12, 0), "-rc7")),
        ("1.2.", ("", (1, 2), ".")),
        (UNPACKERR_NEXT, ("release-", (0, 15, 2), "")),
        ("latest", None),
    ):
        if version_of(tag) != wanted:
            problems.append(f"{tag} read as {version_of(tag)}, wanted {wanted}")
    return problems


def manifest_problems(digest: str, rebuilt: str) -> list[str]:
    """How a move is written into `stack.toml`: in place, once, and nowhere else."""
    problems = []
    manifest = (
        '[[profile]]\nid = "tv"\n\n'
        '[[service]]\nid = "sonarr"\nimage = "lscr.io/linuxserver/sonarr"\ntag = "4.0.15"\n'
        'last_release = "2026-06-26"\n# kept\n\n'
        '[[service]]\nid = "radarr"\ntag = "5.14.0"\nlast_release = "2026-01-01"\n'
    )
    once = rewrite_manifest(manifest, "sonarr", "4.0.20", digest, "2026-09-16")
    if f'tag = "4.0.20"\ndigest = "{digest}"\n' not in once or '"2026-09-16"' not in once:
        problems.append("a service's tag, digest and date were not written in place")
    if 'tag = "5.14.0"' not in once or "# kept" not in once:
        problems.append("rewriting one service disturbed another line")
    twice = rewrite_manifest(once, "sonarr", "4.0.21", rebuilt, "2026-09-20")
    if twice.count("digest = ") != 1 or rebuilt not in twice:
        problems.append("a digest already present was added again rather than replaced")
    try:
        rewrite_manifest(manifest, "nobody", "1", digest, "2026-01-01")
        problems.append("a service that is not in the manifest was rewritten")
    except KeyError:
        pass
    return problems


def compose_problems(digest: str, later: str) -> list[str]:
    """How a move is written into a compose fragment: the one image, and only it."""
    problems = []
    fragment = "services:\n  caddy:\n    image: caddy:2.8.4\n  other:\n    image: caddy-helper:1.0\n"
    body, count = rewrite_compose(fragment, "caddy", f"caddy:2.11.4@{digest}")
    if count != 1 or f"image: caddy:2.11.4@{digest}" not in body or "caddy-helper:1.0" not in body:
        problems.append(f"the compose rewrite changed {count} lines, or the wrong one")
    _, again = rewrite_compose(body, "caddy", f"caddy:2.11.5@{later}")
    if again != 1:
        problems.append("a reference already carrying a digest was not found again")
    return problems


def self_test() -> int:
    digest, rebuilt, later = (f"sha256:{char * 64}" for char in "abc")
    problems = choice_problems() + manifest_problems(digest, rebuilt) + compose_problems(digest, later)
    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        return 1
    print("self-test: the newest tag of each major chosen, and written in both places")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="offline")
    parser.add_argument("--plan", action="store_true", help="print every pin that would move, as JSON")
    parser.add_argument("--apply", nargs="+", metavar="SERVICE", help="move these services' pins")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not (args.plan or args.apply):
        parser.print_help()
        return 2

    moves, problems = plan(set(args.apply or []))
    for problem in problems:
        print(f"::warning::{problem}", file=sys.stderr)
    if args.plan:
        print(json.dumps(moves, indent=2))
        return 0
    # What moved, and which moves the commit has to re-affirm with a
    # `Pin-reviewed:` trailer, for whoever writes the commit.
    print(json.dumps({"moves": moves, "reviewed": apply(moves)}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
