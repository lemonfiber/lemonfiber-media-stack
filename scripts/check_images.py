#!/usr/bin/env python3
"""Verify every pinned image publishes linux/amd64 and linux/arm64.

Inclusion in the stack requires native images for both architectures — an Apple
Silicon or Raspberry Pi operator running an amd64 image under emulation gets a
service that is slow in ways nothing explains.

This is the one check that needs the network, so it lives apart from
validate_manifest.py and runs as its own CI job. It reads the registry's
manifest list rather than pulling anything.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIRED = {("linux", "amd64"), ("linux", "arm64")}


def platforms(reference: str) -> tuple[set[tuple[str, str]], str]:
    """Platforms a reference publishes, read from its manifest list."""
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return set(), result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "inspect failed"

    try:
        document = json.loads(result.stdout)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="check one service id")
    args = parser.parse_args()

    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    services = [s for s in manifest["service"] if not args.only or s["id"] == args.only]
    if not services:
        print(f"no service matching {args.only!r}", file=sys.stderr)
        return 2

    errors = []
    for service in services:
        reference = f"{service['image']}:{service['tag']}"
        found, problem = platforms(reference)
        if problem:
            errors.append(f"{service['id']} ({reference}): {problem}")
            print(f"  FAIL {service['id']:<22} {problem}")
            continue
        missing = REQUIRED - found
        if missing:
            listed = ", ".join(f"{os_}/{arch}" for os_, arch in sorted(missing))
            errors.append(f"{service['id']} ({reference}): missing {listed} (F2-R6)")
            print(f"  FAIL {service['id']:<22} missing {listed}")
        else:
            print(f"  ok   {service['id']:<22} {reference}")

    if errors:
        print()
        print("\n".join(f"::error::{error}" for error in errors))
        return 1
    print(f"\nall {len(services)} images publish linux/amd64 and linux/arm64.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
