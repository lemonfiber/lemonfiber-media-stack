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
from forge import get_json, repo_of, safe

# The shortest window in which a project can show a pattern rather than a burst.
# Under it there is nothing to read: a fork three days old is not badly
# maintained, it is unestablished, and admitting it is a bet rather than a
# judgement.
MIN_HISTORY_DAYS = 30
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


def own_history(owner: str, repo: str, project: dict) -> dict:
    """How many commits are the project's own, over how many days, and how that was read.

    A fork inherits its parent's history, and counting it would credit a
    three-day fork with the parent's decade. So a fork is compared against the
    project it forked and only what is ahead counts — which is the same thing a
    person does by eye when they open the fork's commit list and look for where
    it diverges.
    """
    parent = project.get("parent") or {}
    if parent.get("full_name"):
        parent_owner = (parent.get("owner") or {}).get("login", "")
        head, base = (
            f"{owner}:{project.get('default_branch')}",
            f"{parent_owner}:{parent.get('default_branch')}",
        )
        document, problem = get_json(f"/repos/{owner}/{repo}/compare/{base}...{head}")
        if problem:
            # Falling through to the commit listing here would count the parent's
            # history as the fork's, which is the one mistake this function
            # exists to avoid. Unreadable is the honest answer.
            return {
                "own_commits": 0,
                "at_least": False,
                "history_days": None,
                "history_read_from": f"could not be compared with {safe(parent['full_name'])} ({problem})",
            }
        commits = (document or {}).get("commits") or []
        dates = commit_dates(commits)
        return {
            "own_commits": int((document or {}).get("ahead_by") or len(commits)),
            "at_least": False,
            "history_days": (dates[-1] - dates[0]).days if dates else 0,
            "history_read_from": f"commits ahead of {safe(parent['full_name'])}",
        }

    document, problem = get_json(f"/repos/{owner}/{repo}/commits?per_page=100")
    if problem:
        return {
            "own_commits": 0,
            "at_least": False,
            "history_days": None,
            "history_read_from": f"commit history unreadable ({problem})",
        }
    commits = document if isinstance(document, list) else []
    dates = commit_dates(commits)
    if not dates:
        return {
            "own_commits": 0,
            "at_least": False,
            "history_days": None,
            "history_read_from": "no dated commits",
        }
    if len(commits) >= 100:
        # A full page is a floor, not a count, and the span it covers is the
        # latest hundred commits rather than the project's life. The repository's
        # own age is the honest figure for how long it has been at this.
        created = date_of(project.get("created_at"))
        return {
            "own_commits": len(commits),
            "at_least": True,
            "history_days": (dates[-1] - created).days if created else (dates[-1] - dates[0]).days,
            "history_read_from": "the latest 100 commits, over the repository's age",
        }
    return {
        "own_commits": len(commits),
        "at_least": False,
        "history_days": (dates[-1] - dates[0]).days,
        "history_read_from": "every commit it has",
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

    # Ten, not a hundred. A release carries its whole changelog, and a hundred of
    # them from a long-lived project is past what `forge` will read — which is
    # how a project with hundreds of releases came back as one with none. Nothing
    # below counts past "any at all" and "the latest", so ten is the question.
    releases, release_problem = get_json(f"/repos/{owner}/{name}/releases?per_page=10")
    published = (
        None
        if release_problem
        else [date for date in (date_of(r.get("published_at")) for r in (releases or [])) if date]
    )

    history = own_history(owner, name, project)

    # A project that cuts no GitHub release may still publish versions — several
    # long-established ones tag and never use the releases feature — so the
    # absence of releases alone says nothing. Tags are the weaker signal and are
    # read as one: something to pin, with no dated history behind it.
    tags, tag_problem = get_json(f"/repos/{owner}/{name}/tags?per_page=100")

    found: set[tuple[str, str]] = set()
    how_image = ""
    image_published = None
    if image:
        try:
            found, image_problem = platforms(image)
        except FileNotFoundError:
            found, image_problem = set(), "docker is not on PATH"
        image_published = bool(found) and not image_problem
        how_image = "publishes " + ", ".join(f"{o}/{a}" for o, a in sorted(found)) if found else image_problem

    return {
        **history,
        "archived": bool(project.get("archived")),
        "fork": bool(project.get("fork")),
        "parent": (project.get("parent") or {}).get("full_name"),
        "tags": 0 if tag_problem else len(tags if isinstance(tags, list) else []),
        # None where the list could not be read, which is a different thing from
        # a project that has never released: one is a question that went
        # unanswered and the other is evidence.
        "releases": None if published is None else len(published),
        "releases_problem": release_problem,
        # Asked a page at a time, so a full page is a floor rather than a total.
        # Nothing below turns on which it is, but the figure is printed for a
        # person to read and should not overstate itself.
        "counts_capped": published is not None and len(published) >= 10,
        "latest_release": max(published) if published else None,
        "last_activity": date_of(project.get("pushed_at")),
        "image": image,
        "image_published": image_published,
        "image_detail": how_image,
        "missing_platforms": sorted(REQUIRED - found) if image else [],
        # Read, shown, and never judged on. See the module docstring.
        "self_description": safe(str(project.get("description") or ""), 200),
    }, ""


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

    if evidence.get("releases") is None:
        findings.append(
            (
                "disqualifying",
                "its release history could not be read, so nothing about how it ships is established",
            )
        )
    elif not evidence.get("releases"):
        if evidence.get("tags"):
            findings.append(
                (
                    "caution",
                    (
                        f"no release published, though it carries {evidence['tags']} tag(s): there is a "
                        "version to pin, and no dated release history to read behind it"
                    ),
                )
            )
        elif evidence.get("image_published") is True:
            findings.append(("caution", "no release and no tag; the image is the only thing to pin"))
        elif evidence.get("image_published") is False:
            findings.append(
                (
                    "disqualifying",
                    "no release, no tag and no published image: nothing it has made is installable",
                )
            )
        else:
            findings.append(
                (
                    "disqualifying",
                    "no release and no tag, and no image was offered to check; pass --image to settle it",
                )
            )

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


def report(upstream: str, evidence: dict, verdict: str, findings: list[tuple[str, str]]) -> None:
    print(f"{upstream}\n")
    rows = (
        (
            "own commits",
            (
                f"{evidence['own_commits']}{'+' if evidence['at_least'] else ''} over "
                f"{evidence['history_days']} day(s)  [{evidence['history_read_from']}]"
            ),
        ),
        (
            "releases",
            (
                f"unreadable ({evidence.get('releases_problem')})"
                if evidence["releases"] is None
                else f"{evidence['releases']}{'+' if evidence.get('counts_capped') else ''}"
                + (f", latest {evidence['latest_release']}" if evidence["latest_release"] else "")
            )
            + f"  ({evidence['tags']} tag(s))",
        ),
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
    report(args.upstream, evidence, verdict, findings)

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
