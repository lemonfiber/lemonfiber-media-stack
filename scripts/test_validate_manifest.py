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
import re
import shutil
import subprocess
import sys
import tempfile

import stack_manifest

ROOT = pathlib.Path(__file__).resolve().parent.parent
COPIED = ("stack.toml", "services", "scripts", "recordings", "config/door/Caddyfile")

# What a mutation names to be made in whichever of the manifest's files holds the text:
# the root or a service's file (ARCH-R171).
MANIFEST = "the manifest"


def files(root: pathlib.Path, path: str) -> list[pathlib.Path]:
    """The files `path` names: every one of the manifest's for `MANIFEST`."""
    if path != MANIFEST:
        return [root / path]
    return [root / stack_manifest.ROOT_FILE, *sorted((root / stack_manifest.SERVICES).glob("*.toml"))]


def patch(path: str, old: str, new: str):
    """A mutation that replaces `old` exactly once in `path`, among all its files."""

    def apply(root: pathlib.Path) -> None:
        holding = [target for target in files(root, path) for _ in range(target.read_text(encoding="utf-8").count(old))]
        if len(holding) != 1:
            raise AssertionError(
                f"test fixture is stale: {old!r} appears {len(holding)}x in {path}, expected 1"
            )
        target = holding[0]
        target.write_text(target.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    return apply


def field(service: str, name: str, line: str):
    """A mutation that replaces one field's line in one service's file.

    Found by the service and the field rather than by the value, because the
    values here are pins, dates and digests that `pins.py` moves on its own
    schedule — a fixture spelled with today's value goes stale at the next bump.
    An empty `line` removes the field.
    """

    def apply(root: pathlib.Path) -> None:
        target = root / stack_manifest.service_file(service)
        blocks = re.split(r"(?m)^(?=\[\[)", target.read_text(encoding="utf-8"))
        for number, block in enumerate(blocks):
            if block.startswith("[[service]]") and f'\nid = "{service}"\n' in block:
                replaced, count = re.subn(rf"(?m)^{name} = .*\n", f"{line}\n" if line else "", block)
                if count != 1:
                    raise AssertionError(f"test fixture is stale: {service} has {count} {name!r} lines")
                blocks[number] = replaced
                target.write_text("".join(blocks), encoding="utf-8")
                return
        raise AssertionError(f"test fixture is stale: no [[service]] {service} in {target.name}")

    return apply


def strip_lines(path: str, prefixes: tuple[str, ...]):
    """A mutation that removes every line in `path`'s files starting with one of `prefixes`."""

    def apply(root: pathlib.Path) -> None:
        for target in files(root, path):
            kept = [
                line
                for line in target.read_text(encoding="utf-8").splitlines(keepends=True)
                if not line.startswith(prefixes)
            ]
            target.write_text("".join(kept), encoding="utf-8")

    return apply


# The last line of FlareSolverr's one claim, after which a case writes a second.
FLARESOLVERR_FIXTURE = 'fixture = "recordings/flaresolverr/indexer-proxy-identifies.json"\n'

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
            MANIFEST,
            'media_types = ["tv"]\n'
            'provides = ["library.curate"]\n'
            'health = { kind = "http", path = "/ping", timeout_s = 90 }\n'
            'api = { kind = "servarr", key_source = "config-xml", path = "/config/config.xml", version = 3 }',
            'media_types = ["tv"]\n'
            'provides = ["library.curate"]\n'
            'health = { kind = "http", path = "/ping", timeout_s = 90 }\n'
            'api = { kind = "servarr", key_source = "config-xml", path = "/config/config.xml" }',
        ),
        "servarr api.version must be",
    ),
    (
        "a service with an api that says nowhere it listens",
        field("sabnzbd", "listens", ""),
        "declares an api but no listens",
    ),
    (
        "a memory estimate of nothing",
        patch(MANIFEST, "memory_mib = 600\n", "memory_mib = 0\n"),
        "memory_mib must be a whole number",
    ),
    (
        "a memory estimate that is not a number",
        patch(MANIFEST, "memory_mib = 600\n", 'memory_mib = "600"\n'),
        "memory_mib must be a whole number",
    ),
    (
        "a capability in a plugin's namespace on a bundled service",
        patch(MANIFEST, 'provides = ["download.torrent"]', 'provides = ["qbittorrent:torrent"]'),
        "is not a core name",
    ),
    (
        "a capability name with no area",
        patch(MANIFEST, 'provides = ["subtitles.fetch"]', 'provides = ["fetch"]'),
        "is not a core name",
    ),
    (
        "the same capability declared twice by one service",
        patch(
            MANIFEST,
            'provides = ["media.serve", "identity.source"]',
            'provides = ["media.serve", "media.serve"]',
        ),
        "is declared twice",
    ),
    (
        "an api kind no client here implements",
        patch(
            MANIFEST,
            'api = { kind = "bazarr", key_source = "config-yaml", path = "/config/config/config.yaml" }',
            'api = { kind = "subtitler", key_source = "config-yaml", path = "/config/config/config.yaml" }',
        ),
        "api.kind 'subtitler' unknown",
    ),
    (
        "a key source nothing knows how to read",
        patch(
            MANIFEST,
            'api = { kind = "bazarr", key_source = "config-yaml", path = "/config/config/config.yaml" }',
            'api = { kind = "bazarr", key_source = "config-runes", path = "/config/config/config.yaml" }',
        ),
        "api.key_source 'config-runes' unknown",
    ),
    (
        "B1-R15 a profile claiming a protocol nobody configures",
        patch(
            MANIFEST,
            'description = "Usenet downloading"\nprotocol = "usenet"',
            'description = "Usenet downloading"\nprotocol = "carrier-pigeon"',
        ),
        "is not one of",
    ),
    (
        "B1-R15 two profiles claiming the same protocol",
        patch(
            MANIFEST,
            'description = "Torrent downloading, VPN-isolated"\nprotocol = "torrent"',
            'description = "Torrent downloading, VPN-isolated"\nprotocol = "usenet"',
        ),
        "already claimed",
    ),
    (
        "a media type outside the published vocabulary",
        patch(MANIFEST, 'media_types = ["tv"]', 'media_types = ["telly"]'),
        "unknown media type",
    ),
    (
        "E1-R1 floating tag",
        field("sonarr", "tag", 'tag = "latest"'),
        "floating tag",
    ),
    (
        "E1-R1 a service with no digest",
        field("sonarr", "digest", ""),
        "missing required field 'digest'",
    ),
    (
        "E1-R1 a digest that is not one",
        field("sonarr", "digest", 'digest = "sha256:a5c1a5fe"'),
        "digest must be sha256:",
    ),
    (
        "E1-R1 a digest written into the image name",
        field("sonarr", "image", 'image = "lscr.io/linuxserver/sonarr@sha256:0000000000000000000000000000000000000000000000000000000000000000"'),
        "must not carry a tag or a digest",
    ),
    (
        "ARCH-R246 depends_on, refused naming network.via",
        patch(
            MANIFEST,
            'media_types = ["tv"]',
            'media_types = ["tv"]\ndepends_on = ["prowlarr"]',
        ),
        (
            "service sonarr: declares depends_on, which is not a field: the one start-order edge the model "
            "writes is network.via's"
        ),
    ),
    (
        # Both spellings are still read, so that the refusal can say what to
        # write instead.
        "ARCH-R246 grants under the spelling it had until 0.16.0, refused naming the shape",
        field("gluetun", "shape", 'shape = "egress-guard"\ncapabilities = ["NET_ADMIN"]'),
        (
            "service gluetun: declares capabilities, which is not a field: a kernel capability is a shape's "
            "to give; NET_ADMIN is egress-guard's"
        ),
    ),
    (
        "ARCH-R246 grants, refused naming the shape",
        field("gluetun", "shape", 'shape = "egress-guard"\ngrants = ["SYS_ADMIN"]'),
        (
            "declares grants, which is not a field: a kernel capability is a shape's to give; no shape gives "
            "SYS_ADMIN"
        ),
    ),
    (
        "F2-R14 missing last_release",
        field("sonarr", "last_release", ""),
        "missing required field 'last_release'",
    ),
    (
        "F2-R14 malformed last_release",
        field("prowlarr", "last_release", 'last_release = "22-07-2026"'),
        "last_release must be YYYY-MM-DD",
    ),
    (
        "F2-R14 last_release in the future",
        field("lidarr", "last_release", 'last_release = "2099-01-01"'),
        "is in the future",
    ),
    (
        "F2-R14 last_release that is not a real date",
        field("bazarr", "last_release", 'last_release = "2026-02-31"'),
        "is not a real date",
    ),
    (
        "F2-R5 non-OSI licence",
        patch(
            MANIFEST,
            'license = "MIT"\nupstream = "https://github.com/FlareSolverr/FlareSolverr"',
            'license = "Proprietary"\nupstream = "https://github.com/FlareSolverr/FlareSolverr"',
        ),
        "not a recognised OSI identifier",
    ),
    (
        "manifest references a profile that was never declared",
        patch(MANIFEST, 'profile = "subs"', 'profile = "subtitles"'),
        "unknown profile",
    ),
    (
        "F2-R10 a service that says where it goes and not what for",
        patch(
            MANIFEST,
            'reaches = "music metadata providers"\n'
            'asks_for = "Reads artist, album and track information for the music in your library."\n',
            'reaches = "music metadata providers"\n',
        ),
        "declares 'reaches' without 'asks_for'",
    ),
    (
        "F2-R10 a service that says what it asks for and not of whom",
        patch(
            MANIFEST,
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
            MANIFEST,
            'reaches = "music metadata providers"',
            'reaches = ""',
        ),
        None,
    ),
    (
        "F2-R10 a service whose errand says nothing at all",
        patch(
            MANIFEST,
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
            MANIFEST,
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
        strip_lines(MANIFEST, ("reaches = ", "asks_for = ")),
        None,
    ),
    (
        # The table is the one part of the manifest a stack may legitimately not
        # have: most have removed nothing. A rule that insisted on it would
        # refuse every fork on its first day.
        "F2-R13 a stack that has removed nothing",
        patch(
            MANIFEST,
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
            MANIFEST,
            'reason = "Discontinued upstream in 2025. Its repository is archived, '
            'so the pin could only ever age."\n',
            "",
        ),
        "missing required field 'reason'",
    ),
    (
        "F2-R13 a removal whose reason is empty",
        patch(
            MANIFEST,
            'reason = "Discontinued upstream in 2025. Its repository is archived, '
            'so the pin could only ever age."',
            'reason = "   "',
        ),
        "an empty one records nothing",
    ),
    (
        "F2-R13 a removal naming a service the stack still runs",
        patch(MANIFEST, 'id = "readarr"\nremoved_in', 'id = "bazarr"\nremoved_in'),
        "still declares",
    ),
    (
        "F2-R13 a replacement the stack does not have",
        patch(MANIFEST, 'replaced_by = "bindery"', 'replaced_by = "papyrus"'),
        "neither a service this stack declares nor a removal it records",
    ),
    (
        "F2-R13 a removal replaced by itself",
        patch(MANIFEST, 'replaced_by = "bindery"', 'replaced_by = "readarr"'),
        "which is the service that was removed",
    ),
    (
        "F2-R13 a removal that does not say which version it went in",
        patch(MANIFEST, 'removed_in = "0.1.0"', 'removed_in = "before Bindery"'),
        "removed_in must be the stack version",
    ),
    (
        "a profile no service claims",
        patch(
            MANIFEST,
            '[[profile]]\nid = "dash"',
            '[[profile]]\nid = "unclaimed"\nname = "Unclaimed"\ndescription = "Nothing declares this"\n\n[[profile]]\nid = "dash"',
        ),
        "no service declares this profile",
    ),
    (
        "F4-R12 a by-name wiring that does not say why",
        patch(
            MANIFEST,
            'by = "recyclarr"\nto = "radarr"\nwhy = "The same, in Radarr\'s terms."',
            'by = "recyclarr"\nto = "radarr"',
        ),
        "does not say why",
    ),
    (
        "F4-R12 a by-name wiring whose reason is blank",
        patch(MANIFEST, 'why = "The same, in Lidarr\'s terms."', 'why = "   "'),
        "does not say why",
    ),
    (
        "a wiring that both asks and names",
        patch(
            MANIFEST,
            'by = "seerr"\nasks = "identity.source"',
            'by = "seerr"\nasks = "identity.source"\nto = "jellyfin"\nwhy = "both"',
        ),
        "exactly one of `asks` and `to`",
    ),
    (
        "a wiring that neither asks nor names",
        patch(MANIFEST, 'by = "bazarr"\nasks = "library.curate"\neach = true',
              'by = "bazarr"'),
        "exactly one of `asks` and `to`",
    ),
    (
        "F4-R1 a wiring running from a service this stack does not declare",
        patch(MANIFEST, 'by = "bazarr"\nasks = "library.curate"',
              'by = "subtitler"\nasks = "library.curate"'),
        "by names 'subtitler', which this stack does not declare",
    ),
    (
        "F4-R1 a by-name wiring pointing at a service this stack does not declare",
        patch(MANIFEST, 'by = "unpackerr"\nto = "lidarr"', 'by = "unpackerr"\nto = "lidaarr"'),
        "to names 'lidaarr', which this stack does not declare",
    ),
    (
        "F4-R1 a wiring asking for something shaped like a plugin's own name",
        patch(MANIFEST, 'by = "seerr"\nasks = "identity.source"',
              'by = "seerr"\nasks = "plex:identity"'),
        "which is not a core capability name",
    ),
    (
        "F4-R8 a chosen filler that does not declare the capability",
        patch(MANIFEST, 'asks = "indexer.search"\nfilled_by = "prowlarr"',
              'asks = "indexer.search"\nfilled_by = "sabnzbd"'),
        "which that service does not declare",
    ),
    (
        "F4-R8 a chosen filler with no reason beside it",
        patch(
            MANIFEST,
            'filled_by = "prowlarr"\nwhy = "Two services here answer as an indexer',
            'filled_by = "prowlarr"\nignored = "Two services here answer as an indexer',
        ),
        "a rule somebody encoded",
    ),
    (
        "a wiring that reaches every filler and also chooses one",
        patch(MANIFEST, 'by = "prowlarr"\nasks = "library.curate"\neach = true',
              'by = "prowlarr"\nasks = "library.curate"\neach = true\nfilled_by = "sonarr"'),
        "does both only by meaning neither",
    ),
    (
        "a by-name wiring carrying something only an ask can say",
        patch(MANIFEST, 'by = "unpackerr"\nto = "sonarr"',
              'by = "unpackerr"\nto = "sonarr"\neach = true'),
        "each says something about an ask",
    ),
    (
        "a wiring from a service to itself",
        patch(MANIFEST, 'by = "unpackerr"\nto = "radarr"', 'by = "unpackerr"\nto = "unpackerr"'),
        "a service does not wire to itself",
    ),
    (
        "the same ask written twice",
        patch(MANIFEST, 'by = "lidarr"\nasks = "download.usenet"',
              'by = "lidarr"\nasks = "download.usenet"\n\n[[wiring]]\nby = "lidarr"\nasks = "download.usenet"'),
        "asks for 'download.usenet' twice",
    ),
    (
        "F2-R5 an OSI licence on lemonfiber's own image",
        field("decline", "license", 'license = "MIT"'),
        "licence 'MIT' on lemonfiber's own image",
    ),
    (
        "F2-R5 lemonfiber's own licence on an image lemonfiber does not build",
        field("caddy", "license", 'license = "Hippocratic-3.0"'),
        "licence 'Hippocratic-3.0' is not a recognised OSI identifier",
    ),
    (
        "ARCH-R136 a claim for a capability the service does not provide",
        patch(MANIFEST, 'capability = "indexer.proxy"', 'capability = "indexer.search"'),
        "service flaresolverr.claim indexer.search: indexer.search is not in this service's `provides`",
    ),
    (
        "ARCH-R136 one capability claimed twice",
        patch(MANIFEST, FLARESOLVERR_FIXTURE, FLARESOLVERR_FIXTURE + '\n[[service.claim]]\ncapability = "indexer.proxy"\n'),
        "service flaresolverr.claim indexer.proxy: indexer.proxy is claimed twice",
    ),
    (
        "ARCH-R136 a fixture kept with another service's recordings",
        patch(MANIFEST, 'fixture = "recordings/prowlarr/indexer-search-guarded.json"',
              'fixture = "recordings/flaresolverr/indexer-proxy-identifies.json"'),
        "fixture 'recordings/flaresolverr/indexer-proxy-identifies.json' is not under recordings/prowlarr/",
    ),
    (
        "ARCH-R136 a fixture that climbs out of its service's directory",
        patch(MANIFEST, 'fixture = "recordings/prowlarr/indexer-search-guarded.json"',
              'fixture = "recordings/prowlarr/../flaresolverr/indexer-proxy-identifies.json"'),
        "is not under recordings/prowlarr/, where this service's recordings are kept",
    ),
    (
        "ARCH-R136 a fixture naming no recording",
        patch(MANIFEST, 'fixture = "recordings/prowlarr/indexer-search-guarded.json"',
              'fixture = "recordings/prowlarr/absent.json"'),
        "fixture recordings/prowlarr/absent.json names no recording here",
    ),
    (
        "ARCH-R136 a recording taken from another build",
        patch("recordings/flaresolverr/indexer-proxy-identifies.json", "@sha256:", "@sha256:0"),
        "recording recordings/flaresolverr/indexer-proxy-identifies.json was recorded from",
    ),
    (
        "ARCH-R136 a claim that names no capability",
        patch(MANIFEST, FLARESOLVERR_FIXTURE, FLARESOLVERR_FIXTURE + "\n[[service.claim]]\n"),
        "service flaresolverr: a claim names no capability",
    ),
    (
        "ARCH-R136 a probe that names no fixture",
        patch(MANIFEST, FLARESOLVERR_FIXTURE, ""),
        "service flaresolverr.claim indexer.proxy.probe identifies: names no fixture",
    ),
    (
        "ARCH-R136 probes written as something other than tables",
        patch(MANIFEST, 'capability = "indexer.proxy"\n\n[[service.claim.probe]]\nid = "identifies"\n',
              'capability = "indexer.proxy"\nprobe = ["identifies"]\n\n[[unclaimed]]\nid = "identifies"\n'),
        "service flaresolverr.claim indexer.proxy: probe must be an array of [[service.claim.probe]] tables",
    ),
    (
        "ARCH-R136 claims written as something other than tables",
        field("caddy", "health", 'health = { kind = "tcp", timeout_s = 30 }\nclaim = ["indexer.proxy"]'),
        "service caddy: claim must be an array of [[service.claim]] tables",
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
                (root / item).parent.mkdir(parents=True, exist_ok=True)
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
    from test_service_model import CASES as MODEL_CASES

    cases = [*CASES, *MODEL_CASES]
    print(f"{len(cases)} cases\n")
    failures = 0
    for name, mutation, expected in cases:
        if run_case(name, mutation, expected):
            print(f"  ok   {name}")
        else:
            failures += 1
    print()
    if failures:
        print(f"{failures} of {len(cases)} cases failed.", file=sys.stderr)
        return 1
    print(f"all {len(cases)} cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
