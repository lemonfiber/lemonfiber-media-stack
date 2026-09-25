#!/usr/bin/env python3
"""Verify every pinned digest is an index publishing linux/amd64 and linux/arm64.

Inclusion in the stack requires native images for both architectures — an Apple
Silicon or Raspberry Pi operator running an amd64 image under emulation gets a
service that is slow in ways nothing explains. The pin is the digest of the
multi-architecture index (`E1-R1`), so that is what is asked about: a digest of
one platform's image is refused here as a single-architecture publish.

The tag beside it is a label, and this asks the registry what the tag names
now. For a pin this change moves (`--base`), the two must agree — that is the
mechanical half of reviewing a bump. For a pin it does not move, a tag that has
since been re-published is reported and never failed: publishers rebuild a
release under its tag, and failing every unrelated change on their schedule is
not a check anybody keeps.

This is the one check that needs the network, so it lives apart from
validate_manifest.py and runs as its own CI job. It reads the registry's
manifests rather than pulling anything.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tomllib

from check_manifest_change import pins, read_at, repinned
from registry import resolve

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIRED = {("linux", "amd64"), ("linux", "arm64")}

# What may be handed to docker as a reference. Letters or digits first, so
# nothing beginning with a dash can arrive as a flag, and nothing outside what a
# registry reference is spelled with can arrive at all. Written out here rather
# than imported, beside the call it guards: a check an analysis cannot see next
# to the call is one it reports as absent, and it would be right — the reference
# reaches this from a manifest through one caller and from a command line
# through another, and only one of those was ever anybody's own file.
REFERENCE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}\Z")


def _inspect(reference: str) -> tuple[int, str, str]:
    """What the registry says about a reference, as docker reports it.

    A reference that is not one is refused here rather than passed on, in the
    shape docker would have answered a failure in, so the reading below is the
    same reading either way.
    """
    if not REFERENCE.match(reference):
        return 1, "", f"{reference!r} is not a reference this will hand to docker"
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        capture_output=True, text=True, check=False,
    )
    return result.returncode, result.stdout, result.stderr


def read(code: int, stdout: str, stderr: str) -> tuple[set[tuple[str, str]], str]:
    """Platforms a manifest list publishes, or why none could be read.

    Apart from the call that fetches it so the four judgements below can be
    driven without a registry: a reference nobody can inspect, a reply that is
    not JSON, a list carrying attestations beside its images, and a plain image
    manifest — which is not an empty list but a single-architecture publish, and
    the distinction is the whole point of the check.
    """
    if code != 0:
        return set(), stderr.strip().splitlines()[-1] if stderr.strip() else "inspect failed"

    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return set(), "registry returned something that is not JSON"

    found = set()
    for entry in document.get("manifests", []):
        platform = entry.get("platform", {})
        if platform.get("os") == "unknown" or platform.get("architecture") == "unknown":
            continue  # attestation manifests, not runnable images
        found.add((platform.get("os"), platform.get("architecture")))

    if not found and "config" in document:
        # A plain image manifest rather than a list: single-architecture.
        return set(), "publishes a single-architecture image, not a manifest list"
    return found, ""


def platforms(reference: str) -> tuple[set[tuple[str, str]], str]:
    """Platforms a reference publishes, read from its manifest list."""
    return read(*_inspect(reference))


def reference_problems() -> list[str]:
    """What may be handed to docker as a reference, driven both ways.

    The reference reaches a command line, and one of the two callers takes it
    from `--image` on one of its own. Refused before docker sees it, and refused
    in the shape a docker failure arrives in, so the reading below is the same
    reading either way — which is why this drives `_inspect` rather than the
    pattern, and why it needs no registry to do it.
    """
    problems = []
    for refused in ("--output=/tmp/owned", "-x", "", "caddy:2.8.4 --push", "$(whoami)"):
        if _inspect(refused)[0] == 0:
            problems.append(f"{refused!r} would have been handed to docker")
    for allowed in ("caddy:2.8.4", "lscr.io/linuxserver/sonarr:4.0.15", "ghcr.io/hotio/unpackerr:release-0.14.5"):
        if not REFERENCE.match(allowed):
            problems.append(f"{allowed!r} is a pin this manifest carries, and was refused")
    return problems


def self_test() -> int:
    """Each of the four answers `read` gives, driven against a reply it did not fetch.

    Nothing here needs the network, which is the point: this check is the one
    that does, so its judgements are the ones least likely to be exercised by
    anybody before CI runs them against a registry.
    """
    both = json.dumps({"manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "linux", "architecture": "arm64"}},
    ]})
    attested = json.dumps({"manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "linux", "architecture": "arm64"}},
        {"platform": {"os": "unknown", "architecture": "unknown"}},
    ]})
    one_arch = json.dumps({"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}}]})

    cases = (
        ("both architectures", (0, both, ""), REQUIRED, ""),
        ("an attestation beside them", (0, attested, ""), REQUIRED, ""),
        ("one architecture", (0, one_arch, ""), {("linux", "amd64")}, ""),
        ("a plain image manifest", (0, json.dumps({"config": {}}), ""), set(), "single-architecture"),
        ("a reply that is not JSON", (0, "<html>", ""), set(), "not JSON"),
        ("a reference nobody can inspect", (1, "", "denied: requested access"), set(), "denied"),
        ("a failure that said nothing", (1, "", ""), set(), "inspect failed"),
    )

    problems = reference_problems() + agreement_problems()
    for said, reply, wanted, because in cases:
        found, problem = read(*reply)
        if found != wanted:
            problems.append(f"{said}: read {sorted(found)}, wanted {sorted(wanted)}")
        if because not in problem:
            problems.append(f"{said}: said {problem!r}, which does not mention {because!r}")

    # The other half, and the one that matters: a miss has to be a miss. An
    # attestation counted as a platform, or a single-architecture publish read as
    # an empty list, would each let an image through that runs under emulation.
    for said, reply in (("a plain image manifest", (0, json.dumps({"config": {}}), "")),
                        ("one architecture", (0, one_arch, ""))):
        found, _ = read(*reply)
        if not REQUIRED - found:
            problems.append(f"{said}: was read as publishing both architectures")

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA check that cannot tell a manifest list from an image is not a check.")
        return 1
    print(f"self-test: all {len(cases)} replies were read as they should be")
    return 0


def agreement(sid: str, tag: str, pinned_digest: str, named: str, problem: str, moved: bool) -> tuple[str, str]:
    """Whether a tag still names the pinned digest: an error, a note, or neither.

    Pure, so the self-test can drive it. A registry that could not say is an
    error only where the pin moved, because only there is the answer the review.
    """
    if problem:
        said = f"{sid}: what {tag} names could not be read: {problem}"
        return (said, "") if moved else ("", said)
    if named == pinned_digest:
        return "", ""
    if moved:
        error = (
            f"{sid}: {tag} names {named}, and this change pins {pinned_digest}. A pin that moves "
            f"is the index its tag names when it moves; `scripts/pins.py --apply {sid}` writes both (E1-R1)"
        )
        return error, ""
    return "", f"{sid}: {tag} has been re-published as {named} since it was pinned; `pins.py` takes that"


def agreement_problems() -> list[str]:
    digest, other = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    problems = []
    for said, args, wanted in (
        ("a moved pin its tag names", ("s", "1.0", digest, digest, "", True), ("", "")),
        ("an unmoved pin its tag names", ("s", "1.0", digest, digest, "", False), ("", "")),
        ("a moved pin its tag does not name", ("s", "1.0", digest, other, "", True), ("error", "")),
        ("an unmoved pin whose tag was re-published", ("s", "1.0", digest, other, "", False), ("", "note")),
        ("a moved pin the registry would not answer for", ("s", "1.0", digest, "", "denied", True), ("error", "")),
        ("an unmoved pin the registry would not answer for", ("s", "1.0", digest, "", "denied", False), ("", "note")),
    ):
        error, note = agreement(*args)
        if (bool(error), bool(note)) != (bool(wanted[0]), bool(wanted[1])):
            problems.append(f"{said}: error {error!r}, note {note!r}")
    return problems


def publishes_both(service: dict) -> list[str]:
    """What is wrong with the platforms a service's pinned index publishes, printed as read."""
    reference = f"{service['image']}@{service['digest']}"
    found, problem = platforms(reference)
    if problem:
        print(f"  FAIL {service['id']:<22} {problem}")
        return [f"{service['id']} ({reference}): {problem}"]
    missing = REQUIRED - found
    if missing:
        listed = ", ".join(f"{os_}/{arch}" for os_, arch in sorted(missing))
        print(f"  FAIL {service['id']:<22} missing {listed}")
        return [f"{service['id']} ({reference}): missing {listed} (F2-R6)"]
    print(f"  ok   {service['id']:<22} {reference}")
    return []


def moved_since(base: str) -> tuple[set[str], str]:
    """The services whose pin this change moves, or why that could not be read."""
    found, manifest = read_at(base, "manifest")
    if not found:
        return set(), manifest
    now = (ROOT / "stack.toml").read_text(encoding="utf-8")
    return repinned(pins(manifest), pins(now)), ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="check one service id")
    parser.add_argument("--base", help="the commit this change is measured against; its moved pins must agree")
    parser.add_argument("--self-test", action="store_true", help="prove the reading, without a registry")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    services = [s for s in manifest["service"] if not args.only or s["id"] == args.only]
    if not services:
        print(f"no service matching {args.only!r}", file=sys.stderr)
        return 2

    moved: set[str] = set()
    if args.base:
        moved, problem = moved_since(args.base)
        if problem:
            print(f"::error::which pins moved since {args.base} could not be read: {problem}")
            return 2

    errors, notes = [], []
    for service in services:
        if not service.get("digest"):
            print(f"  FAIL {service['id']:<22} no digest")
            errors.append(f"{service['id']}: pins no digest, so nothing says which build runs (E1-R1)")
            continue
        named, _, problem = resolve(service["image"], service["tag"])
        error, note = agreement(service["id"], service["tag"], service["digest"], named, problem,
                                service["id"] in moved)
        errors.extend([error] if error else [])
        notes.extend([note] if note else [])
        errors.extend(publishes_both(service))

    if notes:
        print()
        print("\n".join(f"  note {note}" for note in notes))
    if errors:
        print()
        print("\n".join(f"::error::{error}" for error in errors))
        return 1
    print(f"\nall {len(services)} pinned indexes publish linux/amd64 and linux/arm64.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
