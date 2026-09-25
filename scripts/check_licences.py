#!/usr/bin/env python3
"""Ask each service's upstream project what licence it publishes now.

`validate_manifest.py` checks the identifier the manifest *records* against the
vendored OSI list, and lemonfiber checks that same string again when it reads the
manifest. Neither asks the project. A service that relicenses away from OSI
approval therefore stays green in both for as long as nobody edits the `license`
field — which is precisely the case this check exists for.

What it fails, and what it only reports:

  A service whose pin this change moves is **gated**. The disqualification
  belongs at pin-bump time because that is when somebody is already looking at
  the service, and when the stack takes a new build of it. For a gated service,
  a licence outside the OSI list fails; so does a licence the forge will not
  name, and so does a forge that does not answer at all. "I could not establish
  it" is not "it is fine", and passing quietly because the network was down is
  the one failure this check must not have.

  Every other service is **reported**. Failing a pull request that did not touch
  a service would turn every unrelated change red on a schedule its author does
  not control and cannot fix — the same reason `check_releases.py` reports drift
  rather than failing it. The note is loud, and the service's next pin bump
  gates it.

  A service this change *adds* is reported too, and deliberately. It has no
  previous pin to have moved from, and whether it belongs here at all is a
  judgement taken before the manifest is edited — README's admission criteria,
  and `check_candidate.py` beside them. Two of the projects already in this
  stack publish a licence file the forge declines to identify; gating admission
  on the forge's classifier would have kept both out for a reason that is not
  about their licences.

  A licence file the forge cannot identify, on a gated service, is compared
  with itself: the file at the upstream release the pin moves to against the
  file at the release it moves from. Byte-identical, the licence did not change
  at this bump, which is the change `F2-R12` disqualifies on, and the service
  passes as `unchanged`. Different, or not readable at either release, it
  fails as before: a file that changed is a licence somebody has to read.

  A recorded identifier that differs from upstream's but is still OSI-approved
  is reported and never failed, gated or not. The forge answers with deprecated
  identifiers — `GPL-3.0` for what SPDX now spells `GPL-3.0-only` — and names
  one licence for a project that offers two, so a mismatch is a prompt to look
  rather than proof of anything. Those two spellings are the same licence here
  for that reason.

Needs the network, and git, which is how it knows which pins moved. Set
GITHUB_TOKEN to avoid the unauthenticated rate limit; where it may be sent is
`forge.py`'s decision, and it is never printed.

    python3 scripts/check_licences.py                    # gates what moved since origin/main
    python3 scripts/check_licences.py --base <sha>
    python3 scripts/check_licences.py --all              # gate every service, deliberately
    python3 scripts/check_licences.py --self-test        # offline, proves the verdicts
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tomllib
import urllib.parse

from check_manifest_change import bumped, pins, read_at
from forge import NOT_FOUND, get_json, repo_of, safe

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The forge's own spelling of "there is a licence file here and I cannot tell
# what it is". Not evidence either way, which is exactly why it is not a pass.
UNIDENTIFIED = {"", "noassertion", "other"}

FAILS_WHEN_GATED = {"moved", "unstated", "unknown"}

# How an upstream names the git tag of a release its image tag names: the
# version bare, with a `v`, or after `release-` — the three spellings the
# upstreams in this stack use.
REF_SPELLINGS = ("{version}", "v{version}", "release-{version}")


def spdx_key(identifier: str) -> str:
    """Two spellings of one licence, reduced to the licence.

    SPDX splits `GPL-3.0` into `GPL-3.0-only` and `GPL-3.0-or-later`; the forge
    still answers with the deprecated undivided identifier. Which of the two a
    project means is a real question and not one this check can settle, so it
    asks the smaller one: is this the same licence?
    """
    key = identifier.strip().lower()
    for suffix in ("-or-later", "-only"):
        if key.endswith(suffix):
            return key[: -len(suffix)]
    return key


def osi_keys() -> set[str]:
    path = ROOT / "scripts" / "spdx_osi.txt"
    return {
        spdx_key(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def stated_licence(upstream: str) -> tuple[str | None, str]:
    """The SPDX identifier the forge says a project publishes, or why it could not say.

    Apart from the judgement below, so the verdicts can be driven against replies
    nobody fetched.
    """
    repo = repo_of(upstream)
    if repo is None:
        return None, "upstream is not a github.com project, so its licence was not read"

    document, problem = get_json(f"/repos/{repo[0]}/{repo[1]}/license")
    if problem == NOT_FOUND:
        # One answer for two things — a project with no licence file, and a
        # project that is not there at all — and neither is a licence.
        return None, ""
    if problem:
        return None, problem
    licence = (document or {}).get("license") or {}
    return str(licence.get("spdx_id") or ""), ""


def release_refs(tag: str) -> list[str]:
    """The git refs an image tag's release may be tagged as upstream."""
    version = re.sub(r"\A(?:release-|[vV])", "", tag)
    return [spelling.format(version=version) for spelling in REF_SPELLINGS]


def licence_file_at(upstream: str, tag: str) -> tuple[str, str]:
    """The blob of upstream's licence file at the release `tag` names, and that release's ref.

    Empty where no spelling of the release is a ref the forge knows, or the
    forge did not answer: either way there is nothing to compare.
    """
    repo = repo_of(upstream)
    if repo is None:
        return "", ""
    for ref in release_refs(tag):
        query = urllib.parse.urlencode({"ref": ref})
        document, problem = get_json(f"/repos/{repo[0]}/{repo[1]}/license?{query}")
        if problem == NOT_FOUND:
            continue
        if problem:
            return "", ""
        return str((document or {}).get("sha") or ""), ref
    return "", ""


def compare_files(was: tuple[str, str], now: tuple[str, str]) -> tuple[str, str]:
    """Whether an unidentified licence file is the one the previous pin shipped. Pure.

    Each side is (blob, ref). Only the same blob at two readable releases clears
    it; anything less leaves the service as the forge left it, unstated.
    """
    (was_blob, was_ref), (now_blob, now_ref) = was, now
    if not (was_blob and now_blob):
        return "unstated", (
            "upstream's licence file is one the forge cannot identify, and it could not be read at "
            "both the release pinned before and the one pinned now"
        )
    if was_blob != now_blob:
        return "unstated", (
            f"upstream's licence file is one the forge cannot identify, and it changed between "
            f"{safe(was_ref)} and {safe(now_ref)}; read it"
        )
    return "unchanged", (
        f"upstream's licence file is one the forge cannot identify, and it is the same file at "
        f"{safe(now_ref)} as at {safe(was_ref)}"
    )


def assess(recorded: str, stated: str | None, problem: str, osi: set[str]) -> tuple[str, str]:
    """Verdict for one service. Pure, so the self-test needs no forge."""
    if problem:
        return "unknown", problem
    if stated is None:
        return "unstated", "the forge reports no licence for it: no licence file, or no such project"
    if spdx_key(stated) in UNIDENTIFIED:
        return "unstated", f"upstream's licence file is one the forge cannot identify ({safe(stated)})"
    if spdx_key(stated) not in osi:
        return "moved", (
            f"upstream now publishes {safe(stated)}, which OSI has not approved; the manifest "
            f"records {recorded}"
        )
    if spdx_key(stated) != spdx_key(recorded):
        return "differs", f"manifest records {recorded}, upstream states {safe(stated)}"
    return "agrees", f"{recorded}"


def comparison_problems() -> list[str]:
    """An unidentified licence file held against the one the previous pin shipped."""
    problems = []
    for said, was, now, want in (
        ("the same file at both releases", ("a1", "4.5.1"), ("a1", "4.5.5"), "unchanged"),
        ("a file that changed between them", ("a1", "4.5.1"), ("b2", "4.5.5"), "unstated"),
        ("a release whose file could not be read", ("", ""), ("b2", "4.5.5"), "unstated"),
        ("neither release readable", ("", ""), ("", ""), "unstated"),
    ):
        verdict, _ = compare_files(was, now)
        if verdict != want:
            problems.append(f"{said}: judged {verdict!r}, wanted {want!r}")
    if "unchanged" in FAILS_WHEN_GATED or "unstated" not in FAILS_WHEN_GATED:
        problems.append("an unchanged file would fail a bump, or a changed one would pass it")
    if release_refs("release-5.2.3") != ["5.2.3", "v5.2.3", "release-5.2.3"] or release_refs("V3.1.4")[0] != "3.1.4":
        problems.append(f"release refs read as {release_refs('release-5.2.3')}")
    return problems


def self_test() -> int:
    """Every verdict, and both halves of the gate, driven against replies nobody fetched.

    The gate is the half worth proving. Each answer below is put through both
    sides of it — a service this change bumped, and one it did not — because the
    whole design of this check is that those two differ, and a mistake in it
    would look exactly like a check that works.
    """
    osi = {"mit", "apache-2.0", "gpl-3.0", "gpl-2.0"}

    cases = (
        ("upstream states what the manifest records", "MIT", "MIT", "", "agrees", "MIT"),
        (
            "the forge's deprecated spelling of the same licence",
            "GPL-3.0-only",
            "GPL-3.0",
            "",
            "agrees",
            "GPL-3.0-only",
        ),
        ("a different licence, still OSI-approved", "MIT", "Apache-2.0", "", "differs", "upstream states"),
        ("a licence OSI has not approved", "MIT", "BUSL-1.1", "", "moved", "OSI has not approved"),
        ("a licence file the forge cannot identify", "MIT", "NOASSERTION", "", "unstated", "cannot identify"),
        ("no licence file at all", "MIT", None, "", "unstated", "no licence for it"),
        ("a forge that did not answer", "MIT", None, "forge unreachable timed out", "unknown", "unreachable"),
        (
            "a forge that answered with a rate limit",
            "MIT",
            None,
            "forge answered 403 rate limited",
            "unknown",
            "403",
        ),
        (
            "an upstream that is not on this forge",
            "MIT",
            None,
            "upstream is not a github.com project",
            "unknown",
            "not a github.com project",
        ),
    )

    problems = []
    for said, recorded, stated, problem, want, because in cases:
        verdict, detail = assess(recorded, stated, problem, osi)
        if verdict != want:
            problems.append(f"{said}: judged {verdict!r}, wanted {want!r}")
        if because not in detail:
            problems.append(f"{said}: said {detail!r}, which does not mention {because!r}")

        # The gate itself: the same answer has to fail a bumped pin and merely be
        # reported against an untouched service. A check that failed both would
        # turn every unrelated change red; one that failed neither would be this
        # repository recording a licence it never asked about.
        gated_outcome = verdict in FAILS_WHEN_GATED
        if gated_outcome != (want in FAILS_WHEN_GATED):
            problems.append(f"{said}: a bumped pin would be judged wrongly")

    if {want for *_, want, _ in cases} != {"agrees", "differs", "moved", "unstated", "unknown"}:
        problems.append("a verdict this check can give is never driven here")

    problems += comparison_problems()

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA check that cannot tell an unanswered question from a licence is not a check.")
        return 1
    print(f"self-test: all {len(cases)} answers were judged, and the gate opened only where it should")
    return 0


def gated_services(
    base: str, all_of_them: bool, manifest_text: str
) -> tuple[set[str], str, dict[str, dict[str, str]]]:
    """The services this run gates, how it decided, and the pins at the base.

    Empty is a legitimate answer. The base's pins are empty under `--all`, which
    compares nothing with a previous release.
    """
    if all_of_them:
        return set(pins(manifest_text)), "every service, asked for", {}
    found, base_manifest = read_at(base, "manifest")
    if not found:
        return set(), "", {}
    before = pins(base_manifest)
    return bumped(before, pins(manifest_text)), f"pins moved since {base}", before


def judged(service: dict, was: dict[str, str] | None, osi: set[str]) -> tuple[str, str]:
    """One service's verdict. `was` is its pin at the base, given only where it is gated.

    An unidentified licence file on a gated service is compared with the file at
    the release pinned before, which is the one question the forge's classifier
    cannot answer and a blob comparison can.
    """
    upstream = str(service.get("upstream", ""))
    stated, problem = stated_licence(upstream)
    verdict, detail = assess(str(service.get("license", "")), stated, problem, osi)
    if was is not None and stated is not None and spdx_key(stated) in UNIDENTIFIED:
        return compare_files(
            licence_file_at(upstream, was["tag"]),
            licence_file_at(upstream, str(service.get("tag", ""))),
        )
    return verdict, detail


def survey(
    services: list, gated: set[str], osi: set[str], before: dict[str, dict[str, str]]
) -> tuple[list[str], list[str], dict[str, int]]:
    """Ask about each service, say what came back, and separate the failures from the notes.

    The asking and the judging are apart — `stated_licence` fetches and `assess`
    decides — and this is the third thing, which is saying. What it fails is
    only ever a gated service, which is the whole design of the check.
    """
    failures, noted, tally = [], [], {}
    for service in services:
        sid = service["id"]
        verdict, detail = judged(service, before.get(sid) if sid in gated else None, osi)
        tally[verdict] = tally.get(verdict, 0) + 1
        gate = sid in gated
        failed = gate and verdict in FAILS_WHEN_GATED
        marker = "ok  " if verdict in ("agrees", "unchanged") else "note"
        print(f"  {'FAIL' if failed else marker} {sid:<22} {'[bumped] ' if gate else ''}{detail}")

        if failed:
            failures.append(f"{sid}: {detail} (F2-R12)")
        elif verdict not in ("agrees", "unchanged"):
            noted.append(sid)
    return failures, noted, tally


def counted(tally: dict[str, int]) -> str:
    """What was asked and what came back, rather than a count of services.

    Every one of these is a question that can go unanswered, and a closing line
    saying nineteen licences were read would be wrong on the run where a rate
    limit answered all nineteen.
    """
    return ", ".join(f"{count} {verdict}" for verdict, count in sorted(tally.items()))


def settled(gated: set[str], tally: dict[str, int]) -> str:
    """What the gate decided, including the common case where it had nothing to decide."""
    if not gated:
        return "nothing was gated, because this change moves no pin"
    if tally.get("unchanged"):
        return (
            f"every one of the {len(gated)} gated is OSI-approved or ships the licence file "
            f"the release pinned before it shipped"
        )
    return f"every one of the {len(gated)} gated is OSI-approved"


def refuse(failures: list[str]) -> None:
    """Say what was refused and why, where a gated service could not be cleared."""
    print()
    print("\n".join(f"::error::{failure}" for failure in failures))
    print(
        "\nA pin bump is where a licence is established. A service whose licence has left the "
        "OSI list, or could not be read at all, does not get to be bumped.",
        file=sys.stderr,
    )
    if any("403" in failure for failure in failures):
        print(
            "A 403 here is the unauthenticated rate limit — sixty requests an hour, and this asks "
            "one per service. Set GITHUB_TOKEN and run it again.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="the commit this change is measured against")
    parser.add_argument("--all", action="store_true", help="gate every service, not only bumped pins")
    parser.add_argument("--self-test", action="store_true", help="prove the verdicts, without a forge")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    manifest_text = (ROOT / "stack.toml").read_text(encoding="utf-8")
    manifest = tomllib.loads(manifest_text)
    gated, how, before = gated_services(args.base, args.all, manifest_text)
    if not how:
        print(
            f"::error::cannot read stack.toml as of {args.base}, so there is no way to tell which "
            "pins this change moves. In a workflow the checkout needs `fetch-depth: 0`; in a clone, "
            "`git fetch origin main`. Pass --all to gate every service instead.",
            file=sys.stderr,
        )
        return 2

    failures, noted, tally = survey(manifest["service"], gated, osi_keys(), before)
    print(f"\ngated: {len(gated)} service(s) — {how}")
    if noted:
        print(f"reported, not failed: {', '.join(noted)}")
        print("A service nothing here touched is reported now and gated when its pin next moves.")

    if failures:
        refuse(failures)
        return 1
    print(f"\n{len(manifest['service'])} upstream(s) asked — {counted(tally)}; {settled(gated, tally)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
