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
import re
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


def compose_alias(service_id: str) -> str:
    """The name the other services address it by: its Compose service name."""
    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    return next(s["id"] for s in manifest["service"] if s["id"] == service_id)


def validate_sabnzbd_whitelist(path: pathlib.Path) -> tuple[bool, str]:
    """The name the *arrs use must be one SABnzbd will answer to.

    SABnzbd refuses any request whose Host header is a name it does not
    recognise, and a fresh install recognises only the container's own generated
    hostname. Names that are an IP, `localhost`, or that end in `.local` are
    accepted without being listed; a Compose service alias is none of those, so
    it has to be in the list or every *arr is answered with 403 — which they
    report as "unable to connect", pointing at the network rather than at this.
    """
    alias = compose_alias("sabnzbd")
    listed = [
        line.split("=", 1)[1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("host_whitelist")
    ]
    if not listed:
        return False, "no host_whitelist is set, so only SABnzbd's own container hostname is accepted"
    names = {name.strip() for name in listed[0].split(",") if name.strip()}
    if alias not in names:
        return False, f"host_whitelist does not name {alias!r}, which is how the *arrs address it"
    return True, ""


def validate_recyclarr(config: pathlib.Path, includes: pathlib.Path) -> tuple[bool, str]:
    """Every include the config names exists, and no two instances share a name.

    Two failures that both present as silence. A missing include file makes the
    whole config unreadable, and two instances called the same thing are rejected
    as duplicates — in either case the sync reports nothing at all and exits `0`,
    so a stack that syncs nothing looks exactly like one with nothing to sync.
    """
    text = config.read_text(encoding="utf-8")

    named = re.findall(r"^\s+- config:\s*(\S+)", text, re.MULTILINE)
    if not named:
        return False, "no include is named, so no instance asks the guides for anything"
    for path in named:
        if not (includes / pathlib.PurePosixPath(path).name).is_file():
            return False, f"include {path} is named but not shipped"

    instances = re.findall(r"^ {2}(\w[\w-]*):$", text, re.MULTILINE)
    duplicated = {name for name in instances if instances.count(name) > 1}
    if duplicated:
        return False, f"instances share a name across services: {sorted(duplicated)}"
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
        with tempfile.TemporaryDirectory() as tmp:
            bare = pathlib.Path(tmp) / "sabnzbd.ini"
            # The shape a fresh install writes: no whitelist of its own.
            bare.write_text("[misc]\nport = 8080\n", encoding="utf-8")
            ok, error = validate_sabnzbd_whitelist(bare)
        if ok:
            print("::error::self-test: a sabnzbd.ini naming nobody was accepted")
            return 1
        print(f"  ok   sabnzbd.ini naming nobody rejected: {error[:80]}")
        with tempfile.TemporaryDirectory() as tmp:
            twice = pathlib.Path(tmp) / "recyclarr.yml"
            # Two instances called the same thing: what 8.x rejects outright.
            twice.write_text(
                "sonarr:\n  main:\n    include:\n      - config: /config/includes/a.yml\n"
                "radarr:\n  main:\n    include:\n      - config: /config/includes/a.yml\n",
                encoding="utf-8",
            )
            shipped = pathlib.Path(tmp) / "includes"
            shipped.mkdir()
            (shipped / "a.yml").write_text("quality_definition:\n  type: movie\n", encoding="utf-8")
            ok, error = validate_recyclarr(twice, shipped)
        if ok:
            print("::error::self-test: a config with two instances named alike was accepted")
            return 1
        print(f"  ok   duplicate instance names rejected: {error[:80]}")
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

    sabnzbd = ROOT / "config" / "sabnzbd" / "sabnzbd.ini"
    ok, error = validate_sabnzbd_whitelist(sabnzbd)
    if not ok:
        print(f"::error::config/sabnzbd/sabnzbd.ini: {error}")
        print(
            "\nEvery *arr would be answered with 403 and report it as being unable "
            "to connect. The download client would never register.",
            file=sys.stderr,
        )
        return 1
    print("  ok   config/sabnzbd/sabnzbd.ini answers to the name the *arrs use")

    ok, error = validate_recyclarr(
        ROOT / "config" / "recyclarr" / "recyclarr.yml",
        ROOT / "config" / "recyclarr" / "includes",
    )
    if not ok:
        print(f"::error::config/recyclarr/recyclarr.yml: {error}")
        print(
            "\nThe sync would report nothing and exit 0, which looks exactly like a "
            "stack with nothing to sync.",
            file=sys.stderr,
        )
        return 1
    print("  ok   config/recyclarr/recyclarr.yml names includes that exist, once each")
    print("\nall shipped templates valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
