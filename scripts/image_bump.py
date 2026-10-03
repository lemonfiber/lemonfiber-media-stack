#!/usr/bin/env python3
"""Pin one of lemonfiber's own images at the tag the release train published.

`ghcr.io/lemonfiber/<service>` is built by `lemonfiber-<service>` and published
only from a tag the train cuts, `vX.Y.Z` or `vX.Y.Z-<identifier>` (ADR-0033 §3).
Each publish dispatches `image-bump` with the service, the tag and the index
digest it recorded, and this writes that pin: `tag` and `digest` in `stack.toml`,
`image:tag@digest` in the service's compose fragment, and `last_release` as the
day it was published (`F2-R14`). `pins.py` leaves these images alone; their pin
moves with the train, not with a registry's newest tag.

The pin is checked before it is written. The service has to be in `stack.toml`
already with lemonfiber's image for its id, and the registry has to answer the
tag with the same digest, as an index publishing `linux/amd64` and `linux/arm64`
(`E1-R1`, `F2-R6`). Where the date written is the one already recorded, the
service is named for a `Pin-reviewed:` trailer, as `pins.py` names one.

    python3 scripts/image_bump.py --apply decline v0.24.0 sha256:...   # JSON: what moved
    python3 scripts/image_bump.py --self-test                          # offline
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import tomllib
from collections.abc import Callable

import registry
from pins import COMPOSE_DIR, OWN_IMAGES, REQUIRED, STACK_TOML, rewrite_compose, rewrite_manifest

SERVICE_ID = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
# A tag the train cuts, and the identifier it refuses: ARCH-R43 gives a release
# candidate a meaning no tag cut on the train may claim by accident.
TRAIN_TAG = re.compile(r"\Av\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?\Z")
RELEASE_CANDIDATE = re.compile(r"\Av[\d.]+-rc\d*(?:\.|\Z)")
NOT_A_TRAIN_TAG = "is not a tag the train cuts: vX.Y.Z, or vX.Y.Z-<identifier> other than rc"

Resolve = Callable[[str, str], tuple[str, set[tuple[str, str]], str]]


class Refused(Exception):
    """A pin that is not written, and why."""


def refusal(manifest: dict, service: str, tag: str, digest: str) -> str:
    """Why this pin is not one to write, before any registry is asked; empty if it is."""
    if not SERVICE_ID.match(service):
        return f"{service!r} is not a service id"
    if not TRAIN_TAG.match(tag) or RELEASE_CANDIDATE.match(tag):
        return f"{tag!r} {NOT_A_TRAIN_TAG}"
    if not registry.DIGEST.match(digest):
        return f"{digest!r} is not a sha256 digest"
    entries = [entry for entry in manifest.get("service", []) if entry.get("id") == service]
    if not entries:
        return (f"stack.toml has no service {service!r}. Its [[service]] and compose entry are "
                "added in a change of their own; this only moves the pin of a service already there.")
    image = f"{OWN_IMAGES}/{service}"
    if entries[0].get("image") != image:
        return (f"{service}'s image is {entries[0].get('image')!r}, not {image!r}; "
                "only lemonfiber's own images are pinned this way")
    return ""


def bump(service: str, tag: str, digest: str, today: str, manifest_text: str,
         fragments: dict[str, str], resolve: Resolve = registry.resolve) -> tuple[dict, str, dict[str, str]]:
    """The pin written into the manifest and compose fragments given.

    What moved, in the shape `pins.py --apply` reports it, the manifest's new
    text, and the new text of each fragment that changed, by name. Nothing is
    read from or written to disk here; `main` does both.
    """
    manifest = tomllib.loads(manifest_text)
    if why := refusal(manifest, service, tag, digest):
        raise Refused(why)
    entry = next(entry for entry in manifest["service"] if entry["id"] == service)
    image = entry["image"]

    served, platforms, problem = resolve(image, tag)
    if problem:
        raise Refused(f"{image}:{tag} could not be resolved: {problem}")
    if served != digest:
        raise Refused(f"{image}:{tag} is {served} in the registry, not {digest}")
    if missing := REQUIRED - platforms:
        listed = ", ".join(f"{os_}/{arch}" for os_, arch in sorted(missing))
        raise Refused(f"{image}:{tag} does not publish {listed} (F2-R6)")

    was = {"tag": entry.get("tag", ""), "digest": entry.get("digest", "")}
    if (was["tag"], was["digest"]) == (tag, digest):
        return {"moves": [], "reviewed": []}, manifest_text, {}

    rewritten, changed = {}, 0
    for name, body in sorted(fragments.items()):
        after, count = rewrite_compose(body, image, f"{image}:{tag}@{digest}")
        if count:
            rewritten[name] = after
            changed += count
    if changed != 1:
        raise Refused(f"{service}: {changed} compose lines name {image}, not one")
    reviewed = [service] if was["tag"] != tag and entry.get("last_release") == today else []
    moved = {"moves": [{"id": service, "image": image, "from": was, "to": {"tag": tag, "digest": digest}}],
             "reviewed": reviewed}
    return moved, rewrite_manifest(manifest_text, service, tag, digest, today), rewritten


#: What the self-tests pin, and where.
SERVICE, TAG, EARLIER, FRAGMENT = "decline", "v0.24.0", "v0.23.0", "dash.yml"
TODAY, RECORDED = "2026-10-03", "2026-10-01"


def refusal_problems(digest: str) -> list[str]:
    """Which pins are refused before a registry is asked, and which are not."""
    manifest = {"service": [
        {"id": SERVICE, "image": f"{OWN_IMAGES}/{SERVICE}"},
        {"id": "sonarr", "image": "lscr.io/linuxserver/sonarr"},
    ]}
    problems = []
    for service, tag, wanted in (
        (SERVICE, TAG, ""),
        (SERVICE, f"{TAG}-pre.1", ""),
        (SERVICE, f"{TAG}-rc.1", NOT_A_TRAIN_TAG),
        (SERVICE, TAG.removeprefix("v"), NOT_A_TRAIN_TAG),
        (SERVICE, f"{TAG} --push", NOT_A_TRAIN_TAG),
        ("Decline", TAG, "not a service id"),
        ("request-gate", TAG, "stack.toml has no service 'request-gate'"),
        ("sonarr", TAG, "only lemonfiber's own images"),
    ):
        said = refusal(manifest, service, tag, digest)
        if (wanted and wanted not in said) or (not wanted and said):
            problems.append(f"{service} at {tag}: said {said!r}, wanted {wanted or 'nothing'!r}")
    if "not a sha256 digest" not in refusal(manifest, SERVICE, TAG, "sha256:abc"):
        problems.append("a digest that is not one was accepted")
    return problems


def bump_problems(digest: str, other: str) -> list[str]:
    """How a pin is checked against the registry and written into both files."""
    manifest = (f'[[service]]\nid = "{SERVICE}"\nimage = "{OWN_IMAGES}/{SERVICE}"\ntag = "{EARLIER}"\n'
                f'digest = "{other}"\nlast_release = "{RECORDED}"\n')
    fragments = {FRAGMENT: f"services:\n  {SERVICE}:\n    image: {OWN_IMAGES}/{SERVICE}:{EARLIER}@{other}\n"}

    def answers(served: str, platforms: set, problem: str = "") -> Resolve:
        return lambda image, tag: (served, platforms, problem)

    problems = []
    for resolve, given, wanted in (
        (answers("", set(), "not found"), fragments, "could not be resolved: not found"),
        (answers(other, REQUIRED), fragments, f"is {other} in the registry"),
        (answers(digest, {("linux", "amd64")}), fragments, "does not publish linux/arm64"),
        (answers(digest, REQUIRED), {FRAGMENT: "services:\n  other:\n    image: caddy:2.8.4\n"},
         "0 compose lines"),
    ):
        try:
            bump(SERVICE, TAG, digest, TODAY, manifest, given, resolve)
            problems.append(f"a pin was written where the answer was {wanted!r}")
        except Refused as refused:
            if wanted not in str(refused):
                problems.append(f"refused with {refused}, wanted {wanted!r}")

    published = answers(digest, REQUIRED)
    moved, text, rewritten = bump(SERVICE, TAG, digest, TODAY, manifest, fragments, published)
    if (f'tag = "{TAG}"\ndigest = "{digest}"' not in text or f'"{TODAY}"' not in text
            or f"{SERVICE}:{TAG}@{digest}" not in rewritten.get(FRAGMENT, "")):
        problems.append("the pin was not written in both places, with the day it was published")
    if moved["moves"][0]["from"]["tag"] != EARLIER or moved["reviewed"]:
        problems.append(f"what moved was reported as {moved}")
    again, _, untouched = bump(SERVICE, TAG, digest, TODAY, text, rewritten, published)
    if again["moves"] or untouched:
        problems.append("a pin already written was reported as moving")
    same_day, _, _ = bump(SERVICE, TAG, digest, RECORDED, manifest, fragments, published)
    if same_day["reviewed"] != [SERVICE]:
        problems.append("a tag moved on the day already recorded was not named for Pin-reviewed")
    return problems


def self_test() -> int:
    problems = refusal_problems(f"sha256:{'a' * 64}") + bump_problems(f"sha256:{'a' * 64}", f"sha256:{'b' * 64}")
    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        return 1
    print("self-test: a pin the train did not publish is refused, and one it did is written in both places")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="offline")
    parser.add_argument("--apply", nargs=3, metavar=("SERVICE", "TAG", "DIGEST"), help="write this pin")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.apply:
        parser.print_help()
        return 2
    service, tag, digest = args.apply
    today = datetime.datetime.now(datetime.UTC).date().isoformat()
    fragments = {path.name: path.read_text(encoding="utf-8") for path in sorted(COMPOSE_DIR.glob("*.yml"))}
    try:
        moved, text, rewritten = bump(service, tag, digest, today,
                                      STACK_TOML.read_text(encoding="utf-8"), fragments)
    except Refused as refused:
        print(f"::error::{refused}", file=sys.stderr)
        return 1
    for name, body in rewritten.items():
        (COMPOSE_DIR / name).write_text(body, encoding="utf-8")
    STACK_TOML.write_text(text, encoding="utf-8")
    print(json.dumps(moved, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
