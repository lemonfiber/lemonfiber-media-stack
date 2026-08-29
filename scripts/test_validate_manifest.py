#!/usr/bin/env python3
"""Negative tests for validate_manifest.py.

A lint nobody has watched fail is a lint nobody knows works. Each case copies the
stack into a temporary directory, breaks exactly one rule, and asserts the
validator reports it. The first case is the control: the unmodified stack passes.

Run directly — no test framework, because the repo has no Python dependencies:

    python3 scripts/test_validate_manifest.py
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
COPIED = ("compose.yml", "stack.toml", "compose", "scripts")


def patch(path: str, old: str, new: str):
    """A mutation that replaces `old` exactly once in `path`."""

    def apply(root: pathlib.Path) -> None:
        target = root / path
        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise AssertionError(
                f"test fixture is stale: {old!r} appears {count}x in {path}, expected 1"
            )
        target.write_text(text.replace(old, new), encoding="utf-8")

    return apply


def append(path: str, text: str):
    def apply(root: pathlib.Path) -> None:
        with (root / path).open("a", encoding="utf-8") as handle:
            handle.write(text)

    return apply


# (name, mutation or None, expected substring of the report)
CASES = [
    (
        "control: the stack as committed passes",
        None,
        None,
    ),
    (
        "a servarr service that declares no API version",
        patch(
            "stack.toml",
            'media_types = ["tv"]\n'
            'health = { kind = "http", path = "/ping", timeout_s = 90 }\n'
            'api = { kind = "servarr", key_source = "config-xml", path = "/config/config.xml", version = 3 }',
            'media_types = ["tv"]\n'
            'health = { kind = "http", path = "/ping", timeout_s = 90 }\n'
            'api = { kind = "servarr", key_source = "config-xml", path = "/config/config.xml" }',
        ),
        "servarr api.version must be",
    ),
    (
        "an api kind no client here implements",
        patch(
            "stack.toml",
            'api = { kind = "bazarr", key_source = "config-yaml", path = "/config/config/config.yaml" }',
            'api = { kind = "subtitler", key_source = "config-yaml", path = "/config/config/config.yaml" }',
        ),
        "api.kind 'subtitler' unknown",
    ),
    (
        "a key source nothing knows how to read",
        patch(
            "stack.toml",
            'api = { kind = "bazarr", key_source = "config-yaml", path = "/config/config/config.yaml" }',
            'api = { kind = "bazarr", key_source = "config-runes", path = "/config/config/config.yaml" }',
        ),
        "api.key_source 'config-runes' unknown",
    ),
    (
        "B1-R15 a profile claiming a protocol nobody configures",
        patch(
            "stack.toml",
            'description = "Usenet downloading"\nprotocol = "usenet"',
            'description = "Usenet downloading"\nprotocol = "carrier-pigeon"',
        ),
        "is not one of",
    ),
    (
        "B1-R15 two profiles claiming the same protocol",
        patch(
            "stack.toml",
            'description = "Torrent downloading, VPN-isolated"\nprotocol = "torrent"',
            'description = "Torrent downloading, VPN-isolated"\nprotocol = "usenet"',
        ),
        "already claimed",
    ),
    (
        "ADR-0006 two mounts beneath the data root",
        patch(
            "compose/media.yml",
            "      - ${DATA_ROOT:-./data}:/data\n      - ./config/jellyfin:/config",
            "      - ${DATA_ROOT:-./data}/media:/media\n"
            "      - ${DATA_ROOT:-./data}/downloads:/downloads\n"
            "      - ./config/jellyfin:/config",
        ),
        "mounts beneath the data root",
    ),
    (
        "ADR-0006 data mounted somewhere other than /data",
        patch(
            "compose/tv.yml",
            "      - ${DATA_ROOT:-./data}:/data",
            "      - ${DATA_ROOT:-./data}/tv:/data",
        ),
        "must be exactly ${DATA_ROOT}:/data",
    ),
    (
        "E1-R1 floating tag",
        patch("stack.toml", 'tag = "4.0.15"', 'tag = "latest"'),
        "floating tag",
    ),
    (
        "B1-R14 cross-profile depends_on in the manifest",
        patch(
            "stack.toml",
            'media_types = ["tv"]',
            'media_types = ["tv"]\ndepends_on = ["prowlarr"]',
        ),
        "crosses a profile boundary",
    ),
    (
        "C6-R1 admin service published beyond loopback",
        patch("compose/tv.yml", '"127.0.0.1:8989:8989"', '"0.0.0.0:8989:8989"'),
        "must be 127.0.0.1",
    ),
    (
        "C6-R2 household service trapped on loopback",
        patch(
            "compose/media.yml",
            '"${LAN_BIND:-0.0.0.0}:8096:8096"',
            '"127.0.0.1:8096:8096"',
        ),
        "unreachable from a TV",
    ),
    (
        "REPO-R18 service in compose but not in the manifest",
        append(
            "compose/tv.yml",
            "\n  ghost:\n    image: alpine:3\n    profiles: [tv]\n",
        ),
        "not in stack.toml",
    ),
    (
        "REPO-R18 service in the manifest but not in compose",
        patch(
            "compose.yml",
            "  - path: compose/movies.yml\n    project_directory: .\n",
            "",
        ),
        "not in the Compose model",
    ),
    (
        "REPO-R18 pinned tag drifts from the manifest",
        patch(
            "compose/tv.yml",
            "lscr.io/linuxserver/sonarr:4.0.15",
            "lscr.io/linuxserver/sonarr:4.0.14",
        ),
        "does not match the manifest",
    ),
    (
        "C6 capability held by a service that never declared it",
        patch(
            "compose/tv.yml",
            "    profiles: [tv]",
            "    profiles: [tv]\n    cap_add: [NET_ADMIN]",
        ),
        "does not match the manifest's",
    ),
    (
        "C2-R12 torrent client escaping the tunnel",
        patch(
            "compose/torrent.yml",
            '    network_mode: "service:gluetun" # all traffic through the tunnel\n',
            "",
        ),
        "killswitch",
    ),
    (
        "F2-R14 missing last_release",
        patch("stack.toml", 'last_release = "2026-06-26"\n', ""),
        "missing required field 'last_release'",
    ),
    (
        "F2-R14 malformed last_release",
        patch("stack.toml", 'last_release = "2026-07-22"', 'last_release = "22-07-2026"'),
        "last_release must be YYYY-MM-DD",
    ),
    (
        "F2-R14 last_release in the future",
        patch("stack.toml", 'last_release = "2025-11-16"', 'last_release = "2099-01-01"'),
        "is in the future",
    ),
    (
        "F2-R14 last_release that is not a real date",
        patch("stack.toml", 'last_release = "2026-07-04"', 'last_release = "2026-02-31"'),
        "is not a real date",
    ),
    (
        "F2-R5 non-OSI licence",
        patch(
            "stack.toml",
            'license = "MIT"\nupstream = "https://github.com/FlareSolverr/FlareSolverr"',
            'license = "Proprietary"\nupstream = "https://github.com/FlareSolverr/FlareSolverr"',
        ),
        "not a recognised OSI identifier",
    ),
    (
        # Compose itself rejects this before the lint runs: the fragment's paths
        # rebase onto compose/, so `extends: file: compose/_common.yml` becomes
        # compose/compose/_common.yml and no model is produced. The mount-source
        # check in validate_manifest.py is the backstop for a fragment that has
        # no extends to break first.
        "include missing project_directory is rejected",
        patch(
            "compose.yml",
            "  - path: compose/tv.yml\n    project_directory: .\n",
            "  - path: compose/tv.yml\n",
        ),
        "compose/compose/",
    ),
    (
        "B1-R1 a service carrying two profiles",
        patch("compose/tv.yml", "    profiles: [tv]", "    profiles: [tv, movies]"),
        "must be exactly",
    ),
    (
        "manifest references a profile that was never declared",
        patch("stack.toml", 'profile = "subs"', 'profile = "subtitles"'),
        "unknown profile",
    ),
    (
        "a profile no service claims",
        patch(
            "stack.toml",
            '[[profile]]\nid = "dash"',
            '[[profile]]\nid = "unclaimed"\nname = "Unclaimed"\ndescription = "Nothing declares this"\n\n[[profile]]\nid = "dash"',
        ),
        "no service declares this profile",
    ),
]


def run_case(name, mutation, expected) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "stack"
        root.mkdir()
        for item in COPIED:
            source = ROOT / item
            if source.is_dir():
                shutil.copytree(source, root / item)
            else:
                shutil.copy2(source, root / item)
        if mutation is not None:
            mutation(root)

        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "validate_manifest.py")],
            cwd=root, capture_output=True, text=True, check=False,
        )
        output = result.stdout + result.stderr

    if expected is None:
        if result.returncode == 0:
            return True
        print(f"  FAIL {name}\n    expected a clean run, got:\n{output}")
        return False

    if result.returncode == 0:
        print(f"  FAIL {name}\n    validator accepted a stack that breaks this rule")
        return False
    if expected not in output:
        print(f"  FAIL {name}\n    expected {expected!r} in the report, got:\n{output}")
        return False
    return True


def main() -> int:
    print(f"{len(CASES)} cases\n")
    failures = 0
    for name, mutation, expected in CASES:
        if run_case(name, mutation, expected):
            print(f"  ok   {name}")
        else:
            failures += 1
    print()
    if failures:
        print(f"{failures} of {len(CASES)} cases failed.", file=sys.stderr)
        return 1
    print(f"all {len(CASES)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
