#!/usr/bin/env python3
"""Validate the stack manifest, and hold compose.yml in parity with it.

Two halves:

  Manifest    stack.toml against the contract at
              spec 20-architecture/contracts/stack-manifest.md, in
              manifest_rules.py, with its `[[wiring]]` table in wiring_rules.py.

  Parity      the *resolved* Compose model against the manifest — services,
              images, profiles, mounts, bindings and the kernel capabilities a
              service is granted — and lemonfiber's own services held to the
              containment their ADRs state, in compose_parity.py.

The parity half reads `docker compose config --format json` rather than parsing
compose.yml. That is deliberate: the resolved model is what Docker will actually
run, with includes merged, `extends:` applied, anchors expanded and variables
interpolated. Linting the source YAML would check what the file appears to say;
this checks what it does.

Every violation is reported in one pass, each naming its location — reporting
one error per run turns fixing a fork into a guessing game.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tomllib

from compose_parity import load_compose_model, validate_parity
from manifest_rules import (
    STACK_TOML,
    osi_licences,
    validate_dependencies,
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
    validate_dependencies(services, service_ids, profile_of, report)
    validate_errands(services, report)
    validate_orphans(profile_ids, forms, services, report)
    validate_removals(manifest.get("removed", []), service_ids, report)
    validate_wirings(manifest.get("wiring", []), services, service_ids, report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="skip compose parity (for environments without Docker)",
    )
    args = parser.parse_args()

    report = Report()
    manifest = tomllib.loads((ROOT / STACK_TOML).read_text(encoding="utf-8"))
    validate_manifest(manifest, report)

    checked_parity = False
    if not args.manifest_only:
        model = load_compose_model(report)
        if model is not None:
            validate_parity(manifest, model, report)
            checked_parity = True

    if report.errors:
        print("\n".join(f"::error::{error}" for error in report.errors))
        print(f"\n{len(report.errors)} violation(s).", file=sys.stderr)
        return 1

    counts = (
        len(manifest.get("profile", [])),
        len(manifest.get("form", [])),
        len(manifest.get("service", [])),
    )
    scope = "manifest + compose parity" if checked_parity else "manifest only"
    removals = len(manifest.get("removed", []))
    recorded = f", {removals} removal(s) recorded" if removals else ""
    print(f"{scope} valid: {counts[0]} profiles, {counts[1]} forms, {counts[2]} services{recorded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
