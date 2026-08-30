#!/usr/bin/env python3
"""Validate the stack manifest, and hold compose.yml in parity with it.

Two halves:

  Manifest    stack.toml against the contract at
              spec 20-architecture/contracts/stack-manifest.md.

  Parity      the *resolved* Compose model against the manifest — services,
              images, profiles, mounts, bindings and capabilities.

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
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Interpolated into DATA_ROOT before resolving the model, so that "is this mount
# beneath the data root?" is an unambiguous test on an absolute path rather than
# a guess about how the source YAML spelled it.
DATA_ROOT_SENTINEL = "/__lemonfiber_data_root__"

SUPPORTED_SCHEMA_VERSIONS = {1}
CRITICALITIES = {"critical", "core", "important", "enhancing", "optional"}
BINDS = {"loopback", "lan"}
HEALTH_KINDS = {"http", "tcp", "container"}
API_KINDS = {"servarr", "sabnzbd", "qbittorrent", "seerr", "bindery", "jellyfin", "bazarr", "audiobookshelf"}
PROTOCOLS = {"usenet", "torrent"}
KEY_SOURCES = {
    "config-xml",
    "config-ini",
    "config-json",
    "config-yaml",
    "api-settings",
    "generated",
    "none",
}
MEDIA_TYPES = {"tv", "movies", "music", "books"}
# Anything beyond this list is a privilege the stack has not justified (C6).
ALLOWED_CAPABILITIES = {"NET_ADMIN"}
# Tags that move under you. A pin that means "whatever is newest" is not a pin.
FLOATING_TAGS = {"latest", "stable", "edge", "nightly", "develop", "dev", "main", "master", "rolling"}

SERVICE_REQUIRED = (
    "id", "name", "profile", "image", "tag",
    "criticality", "license", "upstream", "last_release", "describes", "without_it",
)
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+].*)?$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STACK_TOML = "stack.toml"


class Report:
    """Collects every violation so one run tells the whole story."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, where: str, message: str, requirement: str = "") -> None:
        suffix = f" ({requirement})" if requirement else ""
        self.errors.append(f"{where}: {message}{suffix}")

    def check(self, ok: bool, where: str, message: str, requirement: str = "") -> bool:
        if not ok:
            self.fail(where, message, requirement)
        return ok


def validate_last_release(service: dict, where: str, report: Report) -> None:
    """The latest upstream release, as of the last review of this service's pin.

    Drifting behind upstream is normal and not checked here — it only means
    someone released something. A date in the future cannot arise that way, so it
    is a value that was guessed rather than looked up.
    """
    raw = str(service.get("last_release", ""))
    if not report.check(
        bool(ISO_DATE.match(raw)),
        where,
        f"last_release must be YYYY-MM-DD, got {raw!r}",
        "F2-R14",
    ):
        return
    try:
        recorded = datetime.date.fromisoformat(raw)
    except ValueError:
        report.fail(where, f"last_release {raw!r} is not a real date", "F2-R14")
        return
    report.check(
        recorded <= datetime.date.today(),
        where,
        f"last_release {raw} is in the future",
        "F2-R14",
    )


def osi_licences() -> set[str]:
    path = ROOT / "scripts" / "spdx_osi.txt"
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


# ── manifest ────────────────────────────────────────────────────────────────


def validate_versions(manifest: dict, report: Report) -> None:
    version = manifest.get("schema_version")
    report.check(
        version in SUPPORTED_SCHEMA_VERSIONS,
        STACK_TOML,
        f"schema_version {version!r} unsupported; this validator implements "
        f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}",
    )
    for field in ("stack_version", "min_cli_version"):
        value = manifest.get(field)
        report.check(
            isinstance(value, str) and bool(SEMVER.match(value)),
            STACK_TOML,
            f"{field} must be semver, got {value!r}",
        )


def validate_profiles(profiles: list, report: Report) -> set[str]:
    profile_ids: set[str] = set()
    claimed_protocols: dict[str, str] = {}
    for profile in profiles:
        where = f"profile {profile.get('id', '<unnamed>')}"
        for field in ("id", "name", "description"):
            report.check(field in profile, where, f"missing required field {field!r}")
        pid = profile.get("id")
        if pid is not None:
            report.check(pid not in profile_ids, where, "duplicate profile id")
            profile_ids.add(pid)

        # A profile marked with a protocol is one lemonfiber narrows away when
        # that provider is not configured. An unrecognised value would be read
        # as "never narrow", which is how a VPN-less torrent profile starts.
        protocol = profile.get("protocol")
        if protocol is not None:
            report.check(
                protocol in PROTOCOLS,
                where,
                f"protocol {protocol!r} is not one of {sorted(PROTOCOLS)}",
            )
            owner = claimed_protocols.get(protocol)
            report.check(
                owner is None,
                where,
                f"protocol {protocol!r} is already claimed by profile {owner!r}",
            )
            claimed_protocols.setdefault(protocol, pid)
    return profile_ids


def validate_forms(forms: list, profile_ids: set[str], report: Report) -> None:
    form_ids: set[str] = set()
    for form in forms:
        where = f"form {form.get('id', '<unnamed>')}"
        for field in ("id", "name", "description", "profiles"):
            report.check(field in form, where, f"missing required field {field!r}")
        fid = form.get("id")
        if fid is not None:
            report.check(fid not in form_ids, where, "duplicate form id")
            form_ids.add(fid)
        entries = form.get("profiles", [])
        report.check(bool(entries), where, "form activates no profiles")
        for entry in entries:
            report.check(entry in profile_ids, where, f"unknown profile {entry!r}")
        if "composable" in form:
            report.check(
                isinstance(form["composable"], bool), where, "composable must be a boolean"
            )


def validate_service_runtime(service: dict, where: str, report: Report) -> None:
    if "port" in service:
        port = service["port"]
        report.check(
            isinstance(port, int) and 1 <= port <= 65535, where, f"invalid port {port!r}"
        )
        report.check("bind" in service, where, "port declared without bind", "C6")
    if "bind" in service:
        report.check(
            service["bind"] in BINDS, where, f"bind must be one of {sorted(BINDS)}", "C6"
        )
    for capability in service.get("capabilities", []):
        report.check(
            capability in ALLOWED_CAPABILITIES,
            where,
            f"capability {capability!r} is outside the allow-list "
            f"{sorted(ALLOWED_CAPABILITIES)}",
            "C6",
        )
    for media_type in service.get("media_types", []):
        report.check(
            media_type in MEDIA_TYPES, where, f"unknown media type {media_type!r}"
        )


def validate_service_health(service: dict, where: str, report: Report) -> None:
    health = service.get("health")
    if health is None:
        return
    kind = health.get("kind")
    report.check(kind in HEALTH_KINDS, where, f"health.kind {kind!r} unknown")
    if kind == "http":
        report.check("path" in health, where, "http health requires a path")
        report.check("port" in service, where, "http health requires a port")
    if "timeout_s" in health:
        report.check(
            isinstance(health["timeout_s"], int) and health["timeout_s"] > 0,
            where,
            "health.timeout_s must be a positive integer",
        )


def validate_service_api(service: dict, where: str, report: Report) -> None:
    api = service.get("api")
    if api is None:
        return
    report.check(api.get("kind") in API_KINDS, where, f"api.kind {api.get('kind')!r} unknown")
    key_source = api.get("key_source")
    report.check(key_source in KEY_SOURCES, where, f"api.key_source {key_source!r} unknown")
    if key_source in {"config-xml", "config-ini", "config-json"}:
        report.check("path" in api, where, f"api.key_source {key_source!r} requires a path")
    # The servarr shape spans two API major versions — Sonarr/Radarr are v3,
    # Lidarr/Prowlarr v1 — so the one client cannot assume one; the version is
    # data, and required. The other kinds have a single fixed version their
    # client already knows.
    if api.get("kind") == "servarr":
        report.check(
            api.get("version") in {1, 3},
            where,
            f"servarr api.version must be 1 or 3, got {api.get('version')!r}",
        )


def validate_service(service: dict, profile_ids: set[str], licences: set[str],
                     service_ids: set[str], profile_of: dict[str, str], report: Report) -> None:
    sid = service.get("id", "<unnamed>")
    where = f"service {sid}"
    for field in SERVICE_REQUIRED:
        report.check(field in service, where, f"missing required field {field!r}")
    report.check(sid not in service_ids, where, "duplicate service id")
    service_ids.add(sid)
    profile_of[sid] = service.get("profile", "")

    report.check(
        service.get("profile") in profile_ids,
        where,
        f"unknown profile {service.get('profile')!r}",
        "B1-R1",
    )
    report.check(
        ":" not in str(service.get("image", "")),
        where,
        "image must not carry a tag; use the separate `tag` field",
    )
    tag = str(service.get("tag", ""))
    report.check(
        tag.lower().lstrip("v") not in FLOATING_TAGS and tag != "",
        where,
        f"floating tag {tag!r}",
        "E1-R1",
    )
    report.check(
        service.get("criticality") in CRITICALITIES,
        where,
        f"criticality must be one of {sorted(CRITICALITIES)}",
        "F2-R3",
    )
    licence = service.get("license")
    report.check(
        licence in licences,
        where,
        f"licence {licence!r} is not a recognised OSI identifier; see "
        "scripts/spdx_osi.txt",
        "F2-R5",
    )
    report.check(
        str(service.get("upstream", "")).startswith("https://"),
        where,
        "upstream must be an https URL",
        "F2-R4",
    )
    validate_last_release(service, where, report)
    validate_service_runtime(service, where, report)
    validate_service_health(service, where, report)
    validate_service_api(service, where, report)


def validate_dependencies(services: list, service_ids: set[str],
                          profile_of: dict[str, str], report: Report) -> None:
    # depends_on needs every id known, so it runs after the first pass.
    for service in services:
        sid = service.get("id", "<unnamed>")
        for dep in service.get("depends_on", []):
            where = f"service {sid}"
            if not report.check(dep in service_ids, where, f"depends_on unknown service {dep!r}"):
                continue
            report.check(
                profile_of[dep] == service.get("profile"),
                where,
                f"depends_on {dep!r} crosses a profile boundary "
                f"({service.get('profile')!r} -> {profile_of[dep]!r}); any subset must boot",
                "B1-R14",
            )


def validate_orphans(profile_ids: set[str], forms: list, services: list, report: Report) -> None:
    # A profile no service claims cannot start anything, so a form naming it is a
    # promise the stack cannot keep.
    claimed = {service.get("profile") for service in services}
    for pid in sorted(profile_ids - claimed):
        report.fail(f"profile {pid}", "no service declares this profile")
    # A service in no form is unreachable through lemonfiber.
    in_forms = {entry for form in forms for entry in form.get("profiles", [])}
    for pid in sorted(profile_ids - in_forms):
        report.fail(f"profile {pid}", "no form activates this profile")


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
    validate_orphans(profile_ids, forms, services, report)


# ── parity with the resolved Compose model ──────────────────────────────────


def load_compose_model(report: Report) -> dict | None:
    env = {
        **os.environ,
        "COMPOSE_PROFILES": "*",
        "DATA_ROOT": DATA_ROOT_SENTINEL,
        # Placeholders for the `:?` guards; the values are never used here.
        "VPN_PROVIDER": os.environ.get("VPN_PROVIDER", "validation-placeholder"),
        "WIREGUARD_PRIVATE_KEY": os.environ.get("WIREGUARD_PRIVATE_KEY", "validation-placeholder"),
    }
    try:
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT, env=env, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        report.fail(
            "compose", "docker is not on PATH, so parity cannot be checked; "
            "install Docker or pass --manifest-only to check stack.toml alone",
        )
        return None
    if result.returncode != 0:
        report.fail("compose", f"`docker compose config` failed:\n{result.stderr.strip()}")
        return None
    return json.loads(result.stdout)


def published_ports(service: dict) -> list[tuple[str, str]]:
    """(published, host_ip) pairs. An absent host_ip means every interface."""
    ports = []
    for spec in service.get("ports") or []:
        published = spec.get("published")
        if published in (None, ""):
            continue
        ports.append((str(published), spec.get("host_ip") or "0.0.0.0"))
    return ports


def validate_parity(manifest: dict, model: dict, report: Report) -> None:
    declared = {service["id"]: service for service in manifest.get("service", [])}
    compose = model.get("services", {})

    for sid in sorted(set(declared) - set(compose)):
        report.fail(f"service {sid}", "in stack.toml but not in the Compose model", "REPO-R18")
    for sid in sorted(set(compose) - set(declared)):
        report.fail(f"service {sid}", "in the Compose model but not in stack.toml", "REPO-R18")

    # Which service publishes on whose behalf. A service sharing another's
    # network namespace cannot publish ports of its own — its gateway does.
    gateway_of: dict[str, str] = {}
    for sid, service in compose.items():
        mode = str(service.get("network_mode", ""))
        if mode.startswith("service:"):
            gateway_of[sid] = mode.split(":", 1)[1]

    for sid in sorted(set(declared) & set(compose)):
        spec, service = declared[sid], compose[sid]
        where = f"service {sid}"

        expected_image = f"{spec.get('image')}:{spec.get('tag')}"
        report.check(
            service.get("image") == expected_image,
            where,
            f"image {service.get('image')!r} does not match the manifest's {expected_image!r}",
            "REPO-R18",
        )

        profiles = service.get("profiles") or []
        report.check(
            profiles == [spec.get("profile")],
            where,
            f"Compose profiles {profiles!r} must be exactly [{spec.get('profile')!r}]",
            "B1-R1",
        )

        for dep in (service.get("depends_on") or {}):
            dep_profile = (compose.get(dep, {}).get("profiles") or [None])[0]
            report.check(
                dep_profile == spec.get("profile"),
                where,
                f"depends_on {dep!r} crosses a profile boundary in compose",
                "B1-R14",
            )

        validate_mounts(sid, service, report)
        validate_capabilities(sid, spec, service, report)

    validate_bindings(declared, compose, gateway_of, report)
    validate_gateways(declared, compose, gateway_of, report)


def validate_mounts(sid: str, service: dict, report: Report) -> None:
    where = f"service {sid}"
    beneath_data_root = []
    for volume in service.get("volumes") or []:
        if volume.get("type") != "bind":
            continue
        source = volume.get("source", "")
        if source == DATA_ROOT_SENTINEL or source.startswith(DATA_ROOT_SENTINEL + "/"):
            beneath_data_root.append(volume)
            continue
        # Every other bind must live in the project, and must not have been
        # resolved against compose/ — the symptom of an include that forgot
        # `project_directory: .`.
        report.check(
            source.startswith(str(ROOT)),
            where,
            f"bind mount source {source!r} is outside the project",
        )
        report.check(
            not source.startswith(str(ROOT / "compose")),
            where,
            f"bind mount source {source!r} resolved against compose/; the include "
            "for this fragment is missing `project_directory: .`",
        )

    if len(beneath_data_root) > 1:
        targets = ", ".join(sorted(v.get("target", "?") for v in beneath_data_root))
        report.fail(
            where,
            f"{len(beneath_data_root)} mounts beneath the data root ({targets}); "
            "exactly one is permitted or imports silently copy instead of hardlink",
            "ADR-0006, C5-R5",
        )
    elif len(beneath_data_root) == 1:
        mount = beneath_data_root[0]
        report.check(
            mount.get("source") == DATA_ROOT_SENTINEL and mount.get("target") == "/data",
            where,
            f"the data mount must be exactly ${{DATA_ROOT}}:/data, got "
            f"{mount.get('source', '').replace(DATA_ROOT_SENTINEL, '${DATA_ROOT}')}"
            f":{mount.get('target')}",
            "ADR-0006",
        )


def validate_capabilities(sid: str, spec: dict, service: dict, report: Report) -> None:
    declared_caps = set(spec.get("capabilities", []))
    compose_caps = set(service.get("cap_add") or [])
    report.check(
        declared_caps == compose_caps,
        f"service {sid}",
        f"cap_add {sorted(compose_caps)} does not match the manifest's "
        f"{sorted(declared_caps)}; only Gluetun may hold NET_ADMIN",
        "C6",
    )


def resolve_port_owner(published: str, publisher: str, owners: dict,
                       clients: dict, declared: dict, report: Report) -> str | None:
    """The service whose bind tier governs this published port, or None when it
    cannot be attributed to a single tier (reported as a failure)."""
    owner = owners.get(published)
    if owner is not None:
        return owner
    # Not a declared primary port — the manifest records one per service, and
    # Caddy's 443 or a discovery port is legitimate. It still has to obey its
    # owner's tier, so attribute it.
    tiered = [c for c in clients.get(publisher, [])
              if c in declared and declared[c].get("bind")]
    if publisher in declared and declared[publisher].get("bind"):
        return publisher
    if len(tiered) == 1:
        return tiered[0]
    report.fail(
        f"service {publisher}",
        f"publishes undeclared port {published} and no single service "
        "in its network namespace declares a bind tier, so the "
        "binding policy cannot be applied",
        "C6",
    )
    return None


def check_bind_tier(owner: str, host_ip: str, declared: dict, report: Report) -> None:
    tier = declared[owner].get("bind")
    if tier == "loopback":
        report.check(
            host_ip == "127.0.0.1",
            f"service {owner}",
            f"admin service published on {host_ip}; must be 127.0.0.1",
            "C6-R1",
        )
    elif tier == "lan":
        report.check(
            host_ip != "127.0.0.1",
            f"service {owner}",
            "household service published on loopback; unreachable from a TV",
            "C6-R2",
        )


def check_declared_published(declared: dict, compose: dict, gateway_of: dict, report: Report) -> None:
    for sid, spec in sorted(declared.items()):
        if "port" not in spec or sid not in compose:
            continue
        publisher = gateway_of.get(sid, sid)
        if str(spec["port"]) not in {p for p, _ in published_ports(compose.get(publisher, {}))}:
            report.fail(
                f"service {sid}",
                f"stack.toml declares port {spec['port']} but neither it nor its "
                f"gateway {publisher!r} publishes it",
                "REPO-R18",
            )


def validate_bindings(declared: dict, compose: dict, gateway_of: dict, report: Report) -> None:
    """Every published port must belong to a declared service and match its tier."""
    # Who each publisher is publishing for: itself, plus anyone in its namespace.
    clients: dict[str, list[str]] = {sid: [sid] for sid in compose}
    for sid, gateway in gateway_of.items():
        clients.setdefault(gateway, [gateway]).append(sid)
        clients[sid] = [c for c in clients.get(sid, []) if c != sid]

    for publisher, service in sorted(compose.items()):
        owners = {
            str(declared[c]["port"]): c
            for c in clients.get(publisher, [])
            if c in declared and "port" in declared[c]
        }
        for published, host_ip in published_ports(service):
            owner = resolve_port_owner(published, publisher, owners, clients, declared, report)
            if owner is not None:
                check_bind_tier(owner, host_ip, declared, report)

    check_declared_published(declared, compose, gateway_of, report)


def validate_gateways(declared: dict, compose: dict, gateway_of: dict, report: Report) -> None:
    """A service holding NET_ADMIN is a tunnel; its profile-mates must route through it."""
    for gateway, spec in sorted(declared.items()):
        if "NET_ADMIN" not in spec.get("capabilities", []):
            continue
        profile = spec.get("profile")
        for sid, other in sorted(declared.items()):
            if sid == gateway or other.get("profile") != profile or sid not in compose:
                continue
            report.check(
                gateway_of.get(sid) == gateway,
                f"service {sid}",
                f"shares the {profile!r} profile with the {gateway!r} tunnel but does "
                f"not use `network_mode: service:{gateway}`; its traffic would bypass "
                "the killswitch, and lemonfiber would report it as leaking",
                "C2-R12",
            )


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
    print(f"{scope} valid: {counts[0]} profiles, {counts[1]} forms, {counts[2]} services")
    return 0


if __name__ == "__main__":
    sys.exit(main())
