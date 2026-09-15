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


def strip_lines(path: str, prefixes: tuple[str, ...]):
    """A mutation that removes every line in `path` starting with one of `prefixes`."""

    def apply(root: pathlib.Path) -> None:
        target = root / path
        kept = [
            line
            for line in target.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith(prefixes)
        ]
        target.write_text("".join(kept), encoding="utf-8")

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
        "a media type outside the published vocabulary",
        patch("stack.toml", 'media_types = ["tv"]', 'media_types = ["telly"]'),
        "unknown media type",
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
        "C6 kernel capability held by a service that never declared it",
        patch(
            "compose/tv.yml",
            "    profiles: [tv]",
            "    profiles: [tv]\n    cap_add: [NET_ADMIN]",
        ),
        "does not match the manifest's",
    ),
    (
        # The field was `capabilities` until 0.16.0 and the contract still accepts
        # that spelling. A fork that has not renamed its manifest is validated the
        # same way this one is, rather than told its stack is wrong.
        "a stack written before the rename still validates",
        patch("stack.toml", 'grants = ["NET_ADMIN"]', 'capabilities = ["NET_ADMIN"]'),
        None,
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
        "F2-R10 a service that says where it goes and not what for",
        patch(
            "stack.toml",
            'reaches = "music metadata providers"\n'
            'asks_for = "Reads artist, album and track information for the music in your library."\n',
            'reaches = "music metadata providers"\n',
        ),
        "declares 'reaches' without 'asks_for'",
    ),
    (
        "F2-R10 a service that says what it asks for and not of whom",
        patch(
            "stack.toml",
            'reaches = "music metadata providers"\n'
            'asks_for = "Reads artist, album and track information for the music in your library."\n',
            'asks_for = "Reads artist, album and track information for the music in your library."\n',
        ),
        "declares 'asks_for' without 'reaches'",
    ),
    (
        # The three services that reach nothing say so with an empty value, so
        # an empty one is an answer. A rule reading it as a missing field would
        # refuse the very entries it exists to collect.
        "F2-R10 a service that reaches nothing is not a service missing a value",
        patch(
            "stack.toml",
            'reaches = "music metadata providers"',
            'reaches = ""',
        ),
        None,
    ),
    (
        "F2-R10 a service whose errand says nothing at all",
        patch(
            "stack.toml",
            'asks_for = "Reads artist, album and track information for the music in your library."',
            'asks_for = "  "',
        ),
        "asks_for must say what it asks for",
    ),
    (
        # Half a manifest is the case worth refusing: the service left out is
        # reported as one nothing has described, which sits in the same list as
        # the services that said they reach nothing and cannot be told from them.
        "F2-R10 a manifest that answers for some services and not others",
        patch(
            "stack.toml",
            'reaches = "music metadata providers"\n'
            'asks_for = "Reads artist, album and track information for the music in your library."\n',
            "",
        ),
        "says nothing about what it reaches",
    ),
    (
        # Optional at the schema level: a stack that has written none of this
        # down still parses, here and in lemonfiber, which reports every service
        # in it as one nothing has described rather than refusing to read it.
        "F2-R10 a manifest that answers for no service at all",
        strip_lines("stack.toml", ("reaches = ", "asks_for = ")),
        None,
    ),
    (
        # The table is the one part of the manifest a stack may legitimately not
        # have: most have removed nothing. A rule that insisted on it would
        # refuse every fork on its first day.
        "F2-R13 a stack that has removed nothing",
        patch(
            "stack.toml",
            '[[removed]]\nid = "readarr"\nremoved_in = "0.1.0"\n'
            'reason = "Discontinued upstream in 2025. Its repository is archived, '
            'so the pin could only ever age."\nreplaced_by = "bindery"\n',
            "",
        ),
        None,
    ),
    (
        "F2-R13 a removal that does not say why",
        patch(
            "stack.toml",
            'reason = "Discontinued upstream in 2025. Its repository is archived, '
            'so the pin could only ever age."\n',
            "",
        ),
        "missing required field 'reason'",
    ),
    (
        "F2-R13 a removal whose reason is empty",
        patch(
            "stack.toml",
            'reason = "Discontinued upstream in 2025. Its repository is archived, '
            'so the pin could only ever age."',
            'reason = "   "',
        ),
        "an empty one records nothing",
    ),
    (
        "F2-R13 a removal naming a service the stack still runs",
        patch("stack.toml", 'id = "readarr"\nremoved_in', 'id = "bazarr"\nremoved_in'),
        "still declares",
    ),
    (
        "F2-R13 a replacement the stack does not have",
        patch("stack.toml", 'replaced_by = "bindery"', 'replaced_by = "papyrus"'),
        "neither a service this stack declares nor a removal it records",
    ),
    (
        "F2-R13 a removal replaced by itself",
        patch("stack.toml", 'replaced_by = "bindery"', 'replaced_by = "readarr"'),
        "which is the service that was removed",
    ),
    (
        "F2-R13 a removal that does not say which version it went in",
        patch("stack.toml", 'removed_in = "0.1.0"', 'removed_in = "before Bindery"'),
        "removed_in must be the stack version",
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
