#!/usr/bin/env python3
"""Validate the configuration templates this repo ships.

A template that resolves as Compose data can still be rejected by the service
that has to read it, and the failure lands at `up` time rather than in CI. The
proxy profile is the sharp case: Caddy refuses to start on a config it cannot
parse, and nothing about the Compose model reveals that.

Templates are checked with the same pinned image the stack runs, read from
stack.toml so this cannot drift from what is deployed.

    python3 scripts/check_configs.py
    python3 scripts/check_configs.py --self-test   # prove the check can fail
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def pinned_image(service_id: str) -> str:
    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    service = next(s for s in manifest["service"] if s["id"] == service_id)
    return f"{service['image']}:{service['tag']}"


def validate_caddyfile(path: pathlib.Path) -> tuple[bool, str]:
    """Adapt the Caddyfile exactly as Caddy would, with nothing in the environment.

    Deliberately no variables: a template that only parses once the operator has
    filled in .env is a template that breaks on first run, which is the failure
    this catches.
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{path}:/etc/caddy/Caddyfile:ro",
            pinned_image("caddy"),
            "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
        ],
        capture_output=True, text=True, check=False,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        error = next(
            (line for line in output.splitlines() if line.lower().startswith("error")),
            output.splitlines()[-1] if output else "validation failed",
        )
        return False, error
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="assert a deliberately broken template is rejected",
    )
    args = parser.parse_args()

    if args.self_test:
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp) / "Caddyfile"
            # An `email` global with no argument: the exact shape that shipped.
            broken.write_text("{\n\temail\n}\n", encoding="utf-8")
            ok, error = validate_caddyfile(broken)
        if ok:
            print("::error::self-test: a broken Caddyfile was accepted")
            return 1
        print(f"  ok   broken Caddyfile rejected: {error[:80]}")
        print("\nself-test passed.")
        return 0

    caddyfile = ROOT / "config" / "caddy" / "Caddyfile"
    ok, error = validate_caddyfile(caddyfile)
    if not ok:
        print(f"::error::config/caddy/Caddyfile: {error}")
        print(
            "\nThe proxy profile would fail at startup. Templates must parse with "
            "nothing set in the environment.",
            file=sys.stderr,
        )
        return 1
    print("  ok   config/caddy/Caddyfile adapts with an empty environment")
    print("\nall shipped templates valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
