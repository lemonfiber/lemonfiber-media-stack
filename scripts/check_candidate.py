#!/usr/bin/env python3
"""Judge a candidate service on its published history, not on what it says it is.

A project proposing itself for the stack has a README, and the README says it is
actively maintained. Every project's does. What it cannot write is a commit
history, a list of releases and a published image, so those are what this reads:
dated facts somebody else's infrastructure recorded.

This is deliberately not a merge gate. A candidate has no `[[service]]` entry to
check — it is not in the manifest yet, and the decision about whether it ever
should be is a human one taken before any file changes. So it is run by hand,
its answer goes in the pull request that proposes the service, and the rule it
applies is written down beside the rest of the admission criteria in README.md.

The worked example, which the self-test drives: a fork presenting itself as the
actively maintained successor to a slow-moving project in the stack, with two
days of commits of its own, no release, no published image, and nothing since
April. Rejected — a dead fork is worse than a slow-moving working project.

    python3 scripts/check_candidate.py https://github.com/owner/project
    python3 scripts/check_candidate.py https://github.com/owner/project --image ghcr.io/owner/project:v1
    python3 scripts/check_candidate.py --self-test    # offline, proves the verdicts

Exit 0 where the history supports admission or supports it with a judgement
recorded, 1 where it does not, 2 where the history could not be read — which is
not the same answer and is never reported as one.
"""

from __future__ import annotations

import argparse
import datetime
import sys

from check_images import REQUIRED, platforms
from check_releases import STALE_DAYS
from forge import NOT_FOUND, get_json, repo_of, safe

# The shortest window in which a project can show a pattern rather than a burst.
# Under it there is nothing to read: a fork three days old is not badly
# maintained, it is unestablished, and admitting it is a bet rather than a
# judgement.
MIN_HISTORY_DAYS = 30
# How many commits are asked for at a time. A commit carries its whole message,
# and a hundred of them from a busy project is half a megabyte — past what
# `forge` will read, which turns a fifteen-year-old project into one with no
# history at all. Thirty is more than enough to tell a burst from a record, and
# where the page fills up the repository's own age answers the question instead.
PAGE = 30
# Quiet, and then abandoned. The first is check_releases.py's figure, calibrated
# against the slowest-moving service already in the stack, and it means the same
# thing here: worth a look, not a fault. A year is the point at which "mature
# and finished" stops being the likelier reading.
QUIET_DAYS = STALE_DAYS
ABANDONED_DAYS = 365


def date_of(stamp: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(stamp)[:10])
    except (TypeError, ValueError):
        return None


def commit_dates(commits: list) -> list[datetime.date]:
    """Every date a list of commits carries, in order, undated entries dropped."""
    dated = (date_of((one.get("commit") or {}).get("committer", {}).get("date")) for one in commits)
    return sorted(date for date in dated if date)


def unreadable(why: str) -> dict:
    """A history nobody could read, said as a history rather than as a zero."""
    return {"own_commits": 0, "at_least": False, "history_days": None, "history_read_from": why}


def ahead_of_parent(owner: str, repo: str, project: dict, parent: dict) -> dict:
    """What a fork has done since it forked, and nothing of what it inherited.

    The same thing a person does by eye when they open a fork's commit list and
    look for where it diverges. A comparison that cannot be made is unreadable
    rather than answered from the commit listing, because that listing carries
    the parent's decade and would credit a three-day fork with it.
    """
    of = safe(parent["full_name"])
    head = f"{owner}:{project.get('default_branch')}"
    base = f"{(parent.get('owner') or {}).get('login', '')}:{parent.get('default_branch')}"
    document, problem = get_json(f"/repos/{owner}/{repo}/compare/{base}...{head}?per_page={PAGE}")
    if problem:
        return unreadable(f"could not be compared with {of} ({problem})")

    commits = (document or {}).get("commits") or []
    dates = commit_dates(commits)
    ahead = int((document or {}).get("ahead_by") or len(commits))
    if ahead <= len(commits):
        return {
            "own_commits": ahead,
            "at_least": False,
            "history_days": (dates[-1] - dates[0]).days if dates else 0,
            "history_read_from": f"commits ahead of {of}",
        }

    # Further ahead than one page shows. The span of the page would be the span
    # of its newest commits, and a fork years ahead would read as a burst — so
    # the date it was forked answers instead, which is where its own history
    # starts by definition.
    forked = date_of(project.get("created_at"))
    return {
        "own_commits": len(commits),
        "at_least": True,
        "history_days": (dates[-1] - forked).days if forked and dates else 0,
        "history_read_from": f"the latest {PAGE} commits ahead of {of}, over its age as a fork",
    }


def whole_history(owner: str, repo: str, project: dict) -> dict:
    """How long a project that forked nothing has been at it, from its own commits."""
    document, problem = get_json(f"/repos/{owner}/{repo}/commits?per_page={PAGE}")
    if problem:
        return unreadable(f"commit history unreadable ({problem})")

    commits = document if isinstance(document, list) else []
    dates = commit_dates(commits)
    if not dates:
        return unreadable("no dated commits")

    span = (dates[-1] - dates[0]).days
    if len(commits) < PAGE:
        return {
            "own_commits": len(commits),
            "at_least": False,
            "history_days": span,
            "history_read_from": "every commit it has",
        }

    # A full page is a floor, not a count, and the span it covers is the latest
    # few dozen commits rather than the project's life. The repository's own age
    # is the honest figure for how long it has been at this.
    created = date_of(project.get("created_at"))
    return {
        "own_commits": len(commits),
        "at_least": True,
        "history_days": (dates[-1] - created).days if created else span,
        "history_read_from": f"the latest {PAGE} commits, over the repository's age",
    }


def own_history(owner: str, repo: str, project: dict) -> dict:
    """How much of this project's history is its own, read whichever way applies."""
    parent = project.get("parent") or {}
    if parent.get("full_name"):
        return ahead_of_parent(owner, repo, project, parent)
    return whole_history(owner, repo, project)


def released(owner: str, name: str) -> tuple[datetime.date | None, int | None, str]:
    """When this project last published a release, and whether it publishes them at all.

    One release, not a page of them. A release carries its whole changelog, and
    a project that writes proper release notes answers a request for ten of them
    with megabytes — past what `forge` will read, which is how a project with
    hundreds of releases came back as one that had never released. The endpoint
    for the latest alone answers both questions asked here, in a reply the size
    of one changelog.

    Prereleases and drafts are not "latest" to that endpoint, so a project
    publishing only those reads as having none. That is what the tag count
    beside it is for, and why none of this disqualifies anything on its own.
    """
    document, problem = get_json(f"/repos/{owner}/{name}/releases/latest")
    if problem == NOT_FOUND:
        return None, 0, ""
    if problem:
        return None, None, problem
    return date_of((document or {}).get("published_at")), 1, ""


def tagged(owner: str, name: str) -> int:
    """How many versions this project has tagged.

    A project that cuts no release may still publish versions — several
    long-established ones tag and never use the releases feature — so the absence
    of releases alone says nothing. The weaker signal, and read as one: something
    to pin, with no dated history behind it.
    """
    tags, problem = get_json(f"/repos/{owner}/{name}/tags?per_page=100")
    if problem or not isinstance(tags, list):
        return 0
    return len(tags)


def image_evidence(image: str | None) -> dict:
    """Whether the image a candidate offers exists, and on which architectures.

    `image_published` is None where no image was offered: not checked is not the
    same answer as not published, and the judgement turns on which it is.
    """
    if not image:
        return {"image": None, "image_published": None, "image_detail": "", "missing_platforms": []}
    try:
        found, problem = platforms(image)
    except FileNotFoundError:
        found, problem = set(), "docker is not on PATH"
    where = ", ".join(f"{os_}/{arch}" for os_, arch in sorted(found))
    return {
        "image": image,
        "image_published": bool(found) and not problem,
        "image_detail": f"publishes {where}" if found else problem,
        "missing_platforms": sorted(REQUIRED - found),
    }


def gather(upstream: str, image: str | None) -> tuple[dict | None, str]:
    """Everything dated that the forge knows about a candidate. None where it cannot be read."""
    repo = repo_of(upstream)
    if repo is None:
        return None, f"{safe(upstream)} is not a github.com project URL"
    owner, name = repo

    project, problem = get_json(f"/repos/{owner}/{name}")
    if problem:
        return None, f"{owner}/{name}: {problem}"
    project = project if isinstance(project, dict) else {}

    latest, releases, release_problem = released(owner, name)

    return {
        **own_history(owner, name, project),
        **image_evidence(image),
        "archived": bool(project.get("archived")),
        "fork": bool(project.get("fork")),
        "parent": (project.get("parent") or {}).get("full_name"),
        "tags": tagged(owner, name),
        # None where it could not be read, which is a different thing from a
        # project that has never released: one is a question that went
        # unanswered and the other is evidence.
        "releases": releases,
        "releases_problem": release_problem,
        "latest_release": latest,
        "last_activity": date_of(project.get("pushed_at")),
        # Read, shown, and never judged on. See the module docstring.
        "self_description": safe(str(project.get("description") or ""), 200),
    }, ""


def publication(evidence: dict) -> list[tuple[str, str]]:
    """What a candidate has actually shipped, and what that is worth.

    Releases are the strongest answer, tags the weaker one, an image the last:
    a project may cut no release and still publish versions somebody can pin.
    Only the absence of all three says nothing it has made is installable — and
    a release list that could not be read says nothing at all, which is a
    different answer again.
    """
    if evidence.get("releases") is None:
        return [
            (
                "disqualifying",
                "its release history could not be read, so nothing about how it ships is established",
            )
        ]
    if evidence.get("releases"):
        return []
    if evidence.get("tags"):
        return [
            (
                "caution",
                (
                    f"no release published, though it carries {evidence['tags']} tag(s): there is a "
                    "version to pin, and no dated release history to read behind it"
                ),
            )
        ]
    if evidence.get("image_published") is True:
        return [("caution", "no release and no tag; the image is the only thing to pin")]
    if evidence.get("image_published") is False:
        return [
            (
                "disqualifying",
                "no release, no tag and no published image: nothing it has made is installable",
            )
        ]
    return [
        (
            "disqualifying",
            "no release and no tag, and no image was offered to check; pass --image to settle it",
        )
    ]


def activity(evidence: dict, today: datetime.date) -> list[tuple[str, str]]:
    """How long ago this project last did anything, by the two clocks that show it.

    Going quiet is a judgement to record rather than a fault — the stack already
    carries one slow-moving service deliberately — until the silence is long
    enough that "mature and finished" stops being the likelier reading.
    """
    findings = []

    last = evidence.get("last_activity")
    if last is None:
        findings.append(("disqualifying", "the forge reports no activity date at all"))
    else:
        quiet = (today - last).days
        if quiet > ABANDONED_DAYS:
            findings.append(("disqualifying", f"nothing pushed since {last}, {quiet} days ago"))
        elif quiet > QUIET_DAYS:
            findings.append(("caution", f"nothing pushed since {last}, {quiet} days ago"))

    latest = evidence.get("latest_release")
    if latest is not None and (today - latest).days > QUIET_DAYS:
        findings.append(
            (
                "caution",
                (
                    f"latest release {latest}, {(today - latest).days} days ago; slow-moving is a "
                    "judgement to record, not a fault"
                ),
            )
        )
    return findings


def judge(evidence: dict, today: datetime.date) -> tuple[str, list[tuple[str, str]]]:
    """The verdict and everything behind it. Pure, so the self-test needs no forge.

    `self_description` is in the evidence and is not read here. That is the
    requirement, stated as code: the project's own account of itself is shown to
    whoever runs this and counts for nothing in the answer.
    """
    findings: list[tuple[str, str]] = []

    if evidence.get("archived"):
        findings.append(("disqualifying", "upstream is archived; there is no maintenance to establish"))

    days = evidence.get("history_days")
    if days is None:
        findings.append(("disqualifying", "its commit history could not be read, so nothing is established"))
    elif days < MIN_HISTORY_DAYS:
        findings.append(
            (
                "disqualifying",
                (
                    f"{days} day(s) of commit history of its own, over "
                    f"{evidence.get('own_commits', 0)} commit(s); under {MIN_HISTORY_DAYS} days there is "
                    "no record to judge, only a burst"
                ),
            )
        )

    findings += publication(evidence)
    findings += activity(evidence, today)

    if evidence.get("fork"):
        findings.append(
            (
                "caution",
                (
                    f"a fork of {evidence.get('parent')}: none of what it inherits is its own record, so "
                    f"only its {evidence.get('own_commits', 0)} own commit(s) were counted"
                ),
            )
        )

    if any(kind == "disqualifying" for kind, _ in findings):
        return "reject", findings
    return ("watch" if findings else "admit"), findings


def self_test() -> int:
    """Every verdict, including the one the catalogue already recorded by hand.

    The first two cases are the worked example from the feature text, driven
    twice: once as the forge describes it, and once with the project's own claim
    to be actively maintained attached. The answer has to be identical, because
    the rule is that a project's account of itself is not evidence — and a
    self-test that never tries to mislead the judgement has not proved that.
    """
    today = datetime.date(2026, 9, 13)
    melodarr = {
        "archived": False,
        "fork": True,
        "parent": "Lidarr/Lidarr",
        "own_commits": 9,
        "history_days": 2,
        "releases": 0,
        "tags": 0,
        "latest_release": None,
        "last_activity": datetime.date(2026, 4, 8),
        "image_published": False,
        "self_description": "",
    }
    lidarr = {
        "archived": False,
        "fork": False,
        "parent": None,
        "own_commits": 100,
        "history_days": 3200,
        "releases": 57,
        "tags": 57,
        "latest_release": datetime.date(2025, 11, 16),
        "last_activity": datetime.date(2026, 7, 2),
        "image_published": True,
        "self_description": "Looking after your music collection",
    }

    cases = (
        (
            "the fork the catalogue rejected",
            melodarr,
            "reject",
            ["2 day(s) of commit history", "no release, no tag and no published image"],
        ),
        (
            "the same fork, presenting itself as a maintained successor",
            {
                **melodarr,
                "self_description": "The actively maintained successor to Lidarr. Production ready.",
            },
            "reject",
            ["2 day(s) of commit history"],
        ),
        ("the slow-moving project already in the stack", lidarr, "watch", ["latest release 2025-11-16"]),
        (
            "a project releasing regularly",
            {
                **lidarr,
                "latest_release": datetime.date(2026, 8, 1),
                "last_activity": datetime.date(2026, 9, 1),
            },
            "admit",
            [],
        ),
        ("an archived project", {**lidarr, "archived": True}, "reject", ["archived"]),
        (
            "a project silent for two years",
            {**lidarr, "last_activity": datetime.date(2024, 9, 1)},
            "reject",
            ["nothing pushed since 2024-09-01"],
        ),
        (
            "a project that tags versions and cuts no releases",
            {**lidarr, "releases": 0, "latest_release": None},
            "watch",
            ["no release published, though it carries 57 tag(s)"],
        ),
        (
            "a project that ships only images",
            {**lidarr, "releases": 0, "tags": 0, "latest_release": None},
            "watch",
            ["the image is the only thing to pin"],
        ),
        (
            "a candidate whose release history could not be read",
            {**lidarr, "releases": None, "latest_release": None},
            "reject",
            ["release history could not be read"],
        ),
        (
            "a candidate whose history could not be read",
            {**lidarr, "history_days": None},
            "reject",
            ["could not be read"],
        ),
    )

    problems = []
    for said, evidence, want, because in cases:
        verdict, findings = judge(evidence, today)
        said_what = " | ".join(text for _, text in findings)
        if verdict != want:
            problems.append(f"{said}: judged {verdict!r}, wanted {want!r} — {said_what}")
        for phrase in because:
            if phrase not in said_what:
                problems.append(f"{said}: said {said_what!r}, which does not mention {phrase!r}")

    # The requirement itself, as an assertion: the same history judged with and
    # without the project's claim about itself has to come out the same way.
    claimed = {**melodarr, "self_description": "Actively maintained. Drop-in replacement."}
    if judge(melodarr, today) != judge(claimed, today):
        problems.append("a project's own description changed the verdict on its history")

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA rule that reads a project's own account of itself is the rule this one replaces.")
        return 1
    print(f"self-test: all {len(cases)} candidates were judged on their history alone")
    return 0


def released_row(evidence: dict) -> str:
    """What this project has published, as a person should read it.

    Unread is said as unread rather than as none: the two answers lead opposite
    ways and only one of them is about the project.
    """
    if evidence["releases"] is None:
        return f"unreadable ({evidence.get('releases_problem')})"
    if not evidence["releases"]:
        return "none published"
    latest = f", latest {evidence['latest_release']}" if evidence["latest_release"] else ""
    return f"published{latest}"


def report(upstream: str, evidence: dict, findings: list[tuple[str, str]]) -> None:
    print(f"{upstream}\n")
    rows = (
        (
            "own commits",
            (
                f"{evidence['own_commits']}{'+' if evidence['at_least'] else ''} over "
                f"{evidence['history_days']} day(s)  [{evidence['history_read_from']}]"
            ),
        ),
        ("releases", f"{released_row(evidence)}  ({evidence['tags']} tag(s))"),
        ("last activity", f"{evidence['last_activity']}"),
        ("fork of", f"{evidence['parent']}" if evidence["fork"] else "not a fork"),
        ("archived", "yes" if evidence["archived"] else "no"),
        (
            "image",
            f"{evidence['image']} — {evidence['image_detail']}"
            if evidence["image"]
            else "none offered; pass --image to settle it",
        ),
    )
    for label, value in rows:
        print(f"  {label:<14} {value}")

    if evidence["missing_platforms"]:
        listed = ", ".join(f"{os_}/{arch}" for os_, arch in evidence["missing_platforms"])
        print(f"  {'':<14} missing {listed} (F2-R6)")

    print(f"\n  what it says about itself, which is not evidence:\n    {evidence['self_description'] or '—'}")

    print()
    for kind, text in findings:
        print(f"  {'FAIL' if kind == 'disqualifying' else 'note'} {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", nargs="?", help="the candidate's project URL on github.com")
    parser.add_argument("--image", help="the image it publishes, if it publishes one")
    parser.add_argument("--self-test", action="store_true", help="prove the verdicts, without a forge")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.upstream:
        parser.error("a project URL is required unless --self-test is given")

    evidence, problem = gather(args.upstream, args.image)
    if evidence is None:
        print(f"::error::{problem}", file=sys.stderr)
        print(
            "Nothing was established either way. A candidate whose history cannot be read is not a "
            "candidate that passed.",
            file=sys.stderr,
        )
        return 2

    verdict, findings = judge(evidence, datetime.date.today())
    report(args.upstream, evidence, findings)

    closing = {
        "reject": "Rejected on its history. Say so in the pull request that proposed it, and record it "
        "among the notable exclusions.",
        "watch": "Admissible, with the judgement above recorded against it — the catalogue carries a "
        "signal column for exactly this.",
        "admit": "Nothing in its history argues against admission. The rest of the criteria — one OSI "
        "licence, both architectures, a job no service here already does, no paid tier — are "
        "still yours to check.",
    }[verdict]
    print(f"\n{verdict}: {closing}")
    return 1 if verdict == "reject" else 0


if __name__ == "__main__":
    sys.exit(main())
