#!/usr/bin/env python3
"""Validate the stack manifest.

stack.toml and the service files it includes against the contract at spec
20-architecture/contracts/stack-manifest.md: how the files are laid out in
stack_manifest.py, what they say in manifest_rules.py, and the `[[wiring]]`
table in wiring_rules.py. There is no Compose file here to hold to it: lemonfiber
writes the project from the manifest, and `check_runs.py` starts what it writes.

Every violation is reported in one pass, each naming its location — reporting
one error per run turns fixing a fork into a guessing game.
"""

from __future__ import annotations

import pathlib
import sys

import stack_manifest
from manifest_rules import (
    osi_licences,
    validate_errands,
    validate_forms,
    validate_orphans,
    validate_profiles,
    validate_removals,
    validate_service,
    validate_versions,
)
from report import Report
from wiring_rules import validate_wirings

ROOT = pathlib.Path(__file__).resolve().parent.parent


def validate_manifest(manifest: dict, report: Report) -> None:
    licences = osi_licences()
    validate_versions(manifest, report)
    profiles = manifest.get("profile", [])
    forms = manifest.get("form", [])
    services = manifest.get("service", [])
    profile_ids = validate_profiles(profiles, report)
    validate_forms(forms, profile_ids, report)
    service_ids: set[str] = set()
    profile_of: dict[str, str] = {}
    for service in services:
        validate_service(service, profile_ids, licences, service_ids, profile_of, report)
    validate_errands(services, report)
    validate_orphans(profile_ids, forms, services, report)
    validate_removals(manifest.get("removed", []), service_ids, report)
    validate_wirings(manifest.get("wiring", []), services, service_ids, report)


def main() -> int:
    report = Report()
    # The files first: a manifest whose pieces break the rules about them is read
    # as whatever they do hold, and every fault below is then about that.
    for where, what in stack_manifest.layout_problems(ROOT):
        report.fail(where, what, "ARCH-R171")
    manifest = stack_manifest.load(ROOT)
    validate_manifest(manifest, report)

    for note in report.notes:
        print(f"::notice::{note}")

    if report.errors:
        print("\n".join(f"::error::{error}" for error in report.errors))
        print(f"\n{len(report.errors)} violation(s).", file=sys.stderr)
        return 1

    counts = (
        len(manifest.get("profile", [])),
        len(manifest.get("form", [])),
        len(manifest.get("service", [])),
    )
    removals = len(manifest.get("removed", []))
    recorded = f", {removals} removal(s) recorded" if removals else ""
    print(f"manifest valid: {counts[0]} profiles, {counts[1]} forms, {counts[2]} services{recorded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
