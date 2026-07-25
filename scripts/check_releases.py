#!/usr/bin/env python3
"""Compare each service's recorded last_release against upstream.

The manifest records the latest release upstream had published when the pin was
last reviewed. Two different things can be wrong with that record, and they
deserve different treatment:

  drifted   Upstream has released since. Normal — it means someone shipped.
            Reported so a pin review has the figures, never failed, because
            failing would turn every unrelated change red on upstream's schedule.

  ahead     The record is newer than anything upstream has published. This cannot
            arise from time passing, so the value was guessed rather than looked
            up. Failed.

Needs the network. Set GITHUB_TOKEN to avoid the unauthenticated rate limit.

    python3 scripts/check_releases.py
    python3 scripts/check_releases.py --self-test   # offline, proves the verdicts
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import sys
import tomllib
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Six months of upstream silence is worth a look. Calibrated against the one
# service the catalogue already singles out as slow-moving: a threshold that
# does not surface that one is measuring nothing.
STALE_DAYS = 180


def assess(recorded: datetime.date, actual: datetime.date | None) -> tuple[str, str]:
    """Verdict for one service. Pure, so the self-test needs no network."""
    if actual is None:
        return "unknown", "no upstream release found"
    if recorded > actual:
        return "ahead", f"recorded {recorded} is newer than upstream's latest {actual}"
    behind = (actual - recorded).days
    if behind == 0:
        return "current", f"{recorded}"
    return "drifted", f"recorded {recorded}, upstream now {actual} ({behind}d newer)"


def github_latest(upstream: str) -> datetime.date | None:
    repo = re.sub(r"^https://github\.com/", "", upstream).rstrip("/")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "lemonfiber-media-stack"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for url, pick in (
        (f"https://api.github.com/repos/{repo}/releases/latest", lambda d: d.get("published_at")),
        (f"https://api.github.com/repos/{repo}/tags", lambda d: None),
    ):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
                published = pick(json.load(r))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            continue
        if published:
            return datetime.date.fromisoformat(published[:10])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="prove the verdicts, offline")
    args = parser.parse_args()

    if args.self_test:
        d = datetime.date
        cases = [
            ("current", d(2026, 6, 1), d(2026, 6, 1)),
            ("drifted", d(2026, 1, 1), d(2026, 6, 1)),
            ("ahead", d(2026, 7, 1), d(2026, 6, 1)),
            ("unknown", d(2026, 6, 1), None),
        ]
        bad = [(want, assess(rec, act)[0]) for want, rec, act in cases if assess(rec, act)[0] != want]
        if bad:
            print(f"::error::self-test: wrong verdicts {bad}")
            return 1
        for want, rec, act in cases:
            print(f"  ok   {want:<8} {assess(rec, act)[1]}")
        print("\nself-test passed.")
        return 0

    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    failures, drifted, stale = [], [], []
    today = datetime.date.today()

    for service in manifest["service"]:
        recorded = datetime.date.fromisoformat(service["last_release"])
        verdict, detail = assess(recorded, github_latest(service["upstream"]))
        marker = {"current": "ok  ", "drifted": "note", "ahead": "FAIL", "unknown": "note"}[verdict]
        print(f"  {marker} {service['id']:<22} {detail}")
        if verdict == "ahead":
            failures.append(f"{service['id']}: {detail}")
        elif verdict == "drifted":
            drifted.append(service["id"])
        if (today - recorded).days > STALE_DAYS:
            stale.append(f"{service['id']} ({(today - recorded).days}d)")

    if drifted:
        print(f"\n{len(drifted)} pin(s) behind upstream: {', '.join(drifted)}")
        print("Not a fault. Refresh last_release when you next review the pin.")
    if stale:
        print(f"\nQuiet upstreams, worth a look: {', '.join(stale)}")

    if failures:
        print()
        print("\n".join(f"::error::{f}" for f in failures))
        print("\nA recorded date newer than upstream's latest was not looked up.", file=sys.stderr)
        return 1
    print(f"\nall {len(manifest['service'])} recorded dates are consistent with upstream.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
