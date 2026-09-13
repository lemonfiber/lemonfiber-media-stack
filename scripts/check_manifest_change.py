#!/usr/bin/env python3
"""The rules a *change* to stack.toml must satisfy, which only a diff can see.

Two of them, and both are about a recorded fact going stale at the one moment it
is cheapest to refresh:

  pin moved, date not reviewed
      `last_release` is the latest release upstream had published when the pin
      was last reviewed. Moving a `tag` and leaving the date behind is a record
      that now describes the *previous* review, and nothing else here can tell
      the two apart: the value is still well-formed, still in the past, and
      still consistent with upstream. Only the diff knows the pin moved.

  service gone, removal not recorded
      A service that leaves the stack has to say why and what took its place.
      `validate_manifest.py` holds the `[[removed]]` table to its shape; only
      the diff can see a service that vanished and left no entry at all.

A pin that genuinely needs no new date — a registry re-tagging the same build,
a tag corrected to the one already recorded — is re-affirmed by a trailer on the
commit that moves it, naming the service:

    Pin-reviewed: lidarr

A commit trailer rather than a manifest field or a line in the pull request
body, because it is the form hardest to write by accident. It lives in the
commit that made the change, it names the service rather than the change, it is
reviewed with the message it sits in, and it cannot be altered afterwards
without rewriting history. A field in the manifest is one copy-paste away from
being bumped in the same edit as the tag, which is the exact accident this
exists to catch; text in a pull request body can be edited once the check has
gone green, and nothing would notice.

Needs git — it reads the manifest as of the base commit, and the commit messages
between there and here. Exit 2 means it could not establish what changed, which
is a different answer from "nothing is wrong" and is never reported as one.

    python3 scripts/check_manifest_change.py                 # against origin/main
    python3 scripts/check_manifest_change.py --base <sha>
    python3 scripts/check_manifest_change.py --self-test     # offline, no git
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACK_TOML = "stack.toml"

# One trailer per line, and a line may name several services. Matched
# case-insensitively at the start of a line, the way git reads a trailer.
REAFFIRMED = re.compile(r"^[ \t]*Pin-reviewed:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)


def pins(text: str) -> dict[str, dict[str, str]]:
    """What each service records about its pin, keyed by service id.

    Takes the manifest's text rather than a path: the two sides of this
    comparison come from different places — one from `git show`, one from the
    working tree — and neither is a file this can open for itself.
    """
    manifest = tomllib.loads(text)
    return {
        str(service.get("id")): {
            "image": str(service.get("image", "")),
            "tag": str(service.get("tag", "")),
            "last_release": str(service.get("last_release", "")),
        }
        for service in manifest.get("service", [])
    }


def recorded_removals(text: str) -> set[str]:
    """The service ids the manifest's removal table names."""
    manifest = tomllib.loads(text)
    return {str(entry.get("id")) for entry in manifest.get("removed", [])}


def bumped(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> set[str]:
    """Services whose pin this change moves.

    Image as well as tag: a service moved to another registry is running
    somebody else's build, which is at least as much of a review as a new
    version of the same one. A service that did not exist before is not a bump —
    it has no previous pin to have moved from.
    """
    return {
        sid
        for sid, now in after.items()
        if sid in before and (now["image"], now["tag"]) != (before[sid]["image"], before[sid]["tag"])
    }


def judge(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    removed: set[str],
    reaffirmed: set[str],
) -> list[str]:
    """Everything wrong with one manifest change. Pure, so the self-test needs no git.

    Every violation in one pass, each naming its service — the same reason
    validate_manifest.py reports them all rather than stopping at the first.
    """
    faults = []

    for sid in sorted(bumped(before, after)):
        if after[sid]["last_release"] != before[sid]["last_release"]:
            continue
        if sid in reaffirmed:
            continue
        was, now = before[sid], after[sid]
        moved = (
            f"{was['tag']} -> {now['tag']}"
            if was["image"] == now["image"]
            else f"{was['image']}:{was['tag']} -> {now['image']}:{now['tag']}"
        )
        faults.append(
            f"service {sid}: the pin moved ({moved}) and last_release did not; it still says "
            f"{now['last_release']}, which is what the previous review found. Look up what "
            f"upstream has published now, or say the review needed no new date with a "
            f"`Pin-reviewed: {sid}` trailer on the commit that moves it (F2-R14)"
        )

    for sid in sorted(set(before) - set(after)):
        if sid in removed:
            continue
        faults.append(
            f"service {sid}: left the stack and the manifest records no reason; add a "
            f"[[removed]] entry saying why it went and what replaced it (F2-R13)"
        )

    return faults


def reaffirmations(messages: str) -> set[str]:
    """Service ids named by `Pin-reviewed:` trailers, across every commit message."""
    named: set[str] = set()
    for line in REAFFIRMED.findall(messages):
        named.update(part.strip() for part in line.replace(",", " ").split() if part.strip())
    return named


def git(*arguments: str) -> tuple[bool, str]:
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False)
    return result.returncode == 0, (result.stdout if result.returncode == 0 else result.stderr.strip())


def self_test() -> int:
    """Each verdict, driven against a change no repository contains.

    Reaching any of these for real needs a branch, a base commit and a pin that
    somebody moved, which is why none of them had ever been driven. The two that
    matter most are the last pair: a rule that refuses every change is as
    useless as one that refuses none, and only those two say which this is.
    """
    was = {
        "sonarr": {"image": "lscr.io/linuxserver/sonarr", "tag": "4.0.15", "last_release": "2026-06-26"},
        "lidarr": {"image": "lscr.io/linuxserver/lidarr", "tag": "2.9.6", "last_release": "2025-11-16"},
    }

    def with_sonarr(**changes: str) -> dict[str, dict[str, str]]:
        return {**was, "sonarr": {**was["sonarr"], **changes}}

    cases = (
        (
            "a pin moved and the date was refreshed",
            was,
            with_sonarr(tag="4.0.16", last_release="2026-08-01"),
            set(),
            set(),
            None,
        ),
        (
            "a pin moved and the date did not",
            was,
            with_sonarr(tag="4.0.16"),
            set(),
            set(),
            "the pin moved (4.0.15 -> 4.0.16)",
        ),
        (
            "a pin moved, the date did not, and the commit re-affirmed it",
            was,
            with_sonarr(tag="4.0.16"),
            set(),
            {"sonarr"},
            None,
        ),
        (
            "a re-affirmation naming another service entirely",
            was,
            with_sonarr(tag="4.0.16"),
            set(),
            {"lidarr"},
            "service sonarr",
        ),
        (
            "a service moved to another registry, same tag",
            was,
            with_sonarr(image="ghcr.io/elsewhere/sonarr"),
            set(),
            set(),
            "lscr.io/linuxserver/sonarr:4.0.15 -> ghcr.io/elsewhere/sonarr:4.0.15",
        ),
        (
            "a service removed with its reason recorded",
            was,
            {"lidarr": was["lidarr"]},
            {"sonarr"},
            set(),
            None,
        ),
        (
            "a service removed and nothing said why",
            was,
            {"lidarr": was["lidarr"]},
            set(),
            set(),
            "records no reason",
        ),
        (
            "a service added",
            was,
            {
                **was,
                "bazarr": {
                    "image": "lscr.io/linuxserver/bazarr",
                    "tag": "1.4.5",
                    "last_release": "2026-07-04",
                },
            },
            set(),
            set(),
            None,
        ),
        (
            "a date refreshed on a pin that did not move",
            was,
            with_sonarr(last_release="2026-08-01"),
            set(),
            set(),
            None,
        ),
        (
            "a change that touched no service at all",
            was,
            was,
            set(),
            set(),
            None,
        ),
    )

    problems = []
    for said, before, after, removed, reaffirmed, because in cases:
        faults = judge(before, after, removed, reaffirmed)
        if because is None:
            if faults:
                problems.append(f"{said}: was refused — {faults}")
        elif not faults:
            problems.append(f"{said}: was accepted")
        elif not any(because in fault for fault in faults):
            problems.append(f"{said}: said {faults}, which does not mention {because!r}")

    # The trailer is the escape hatch, so a trailer nobody wrote must not open
    # it: this reads the form out of a whole commit message, beside the two
    # trailers every commit here already carries.
    message = (
        "fix(lidarr): take the tag the registry re-cut\n\n"
        "Pin-reviewed: lidarr, sonarr\n"
        "Signed-off-by: A Maintainer <m@example.com>\n"
        "Spec: F2-R14\n"
    )
    if reaffirmations(message) != {"lidarr", "sonarr"}:
        problems.append(f"a trailer naming two services read as {reaffirmations(message)}")
    if reaffirmations("fix: something\n\nSpec: F2-R14\n"):
        problems.append("a message carrying no trailer was read as re-affirming something")

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA rule that cannot tell a reviewed bump from an unreviewed one is not a rule.")
        return 1
    print(f"self-test: all {len(cases)} changes were judged as they should be")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="the commit this change is measured against")
    parser.add_argument("--self-test", action="store_true", help="prove the verdicts, without git")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    found, base_manifest = git("show", f"{args.base}:{STACK_TOML}")
    if not found:
        print(
            f"::error::cannot read {STACK_TOML} as of {args.base}: {base_manifest}\n"
            "Without the base commit there is no way to tell which pins moved, and a check "
            "that cannot tell does not get to say nothing is wrong. In a workflow this means "
            "the checkout needs `fetch-depth: 0`; in a clone, `git fetch origin main`.",
            file=sys.stderr,
        )
        return 2

    logged, messages = git("log", "--format=%B", f"{args.base}..HEAD")
    if not logged:
        print(f"::error::cannot read the commits since {args.base}: {messages}", file=sys.stderr)
        return 2

    head_manifest = (ROOT / STACK_TOML).read_text(encoding="utf-8")
    before, after = pins(base_manifest), pins(head_manifest)
    faults = judge(before, after, recorded_removals(head_manifest), reaffirmations(messages))

    if faults:
        print("\n".join(f"::error::{fault}" for fault in faults))
        print(f"\n{len(faults)} violation(s).", file=sys.stderr)
        return 1

    moved = sorted(bumped(before, after))
    if moved:
        print(f"pins moved and reviewed: {', '.join(moved)}")
    gone = sorted(set(before) - set(after))
    if gone:
        print(f"services removed, each with its reason recorded: {', '.join(gone)}")
    if not moved and not gone:
        print(f"no pin moved and no service left the stack since {args.base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
