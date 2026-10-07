"""stack.toml against the contract at
spec 20-architecture/contracts/stack-manifest.md: its versions, profiles, forms,
services, claims and removals. The `[[wiring]]` table is held in wiring_rules.py.
"""

from __future__ import annotations

import datetime
import json
import pathlib
import re

from pins import OWN_LICENCE, rides_the_train
from registry import DIGEST, by_digest
from report import UNNAMED, Report

ROOT = pathlib.Path(__file__).resolve().parent.parent

SUPPORTED_SCHEMA_VERSIONS = {1}
CRITICALITIES = {"critical", "core", "important", "enhancing", "optional"}
BINDS = {"loopback", "lan"}
HEALTH_KINDS = {"http", "tcp", "container"}
API_KINDS = {
    "servarr",
    "sabnzbd",
    "qbittorrent",
    "seerr",
    "bindery",
    "jellyfin",
    "bazarr",
    "audiobookshelf",
    "nzbhydra2",
}
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
# The media types a service may say it handles, which is what points it at the
# part of the library it works on. Published in the stack manifest contract
# rather than only here: this set is read by anything that describes a library,
# including a plugin's manifest, and a vocabulary only its own validator can see
# is one nothing outside can declare into. `comics` is in it although nothing
# bundled declares it, because a set that admits only what is already bundled is
# one a plugin cannot extend the library with.
MEDIA_TYPES = {"tv", "movies", "music", "books", "comics"}
# A core capability name: `area.verb`, lowercase, exactly one dot. A plugin's own
# carries a colon instead and is that plugin's to declare, never a bundled
# service's. Which names exist is lemonfiber's to say — it generates the
# published vocabulary from this field and refuses a service naming one the
# vocabulary does not carry. What is checked here is the shape, because a name
# of the wrong shape is not a name the other side could ever recognise.
CORE_CAPABILITY = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# Anything beyond this list is a privilege the stack has not justified (C6).
ALLOWED_GRANTS = {"NET_ADMIN"}
# Tags that move under you. A pin that means "whatever is newest" is not a pin.
FLOATING_TAGS = {"latest", "stable", "edge", "nightly", "develop", "dev", "main", "master", "rolling"}

SERVICE_REQUIRED = (
    "id", "name", "profile", "image", "tag", "digest",
    "criticality", "license", "upstream", "last_release", "describes", "without_it",
)
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+].*)?$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STACK_TOML = "stack.toml"
# Where the stack keeps each service's recordings, one directory per service id.
RECORDINGS = "recordings"


def grants_of(service: dict) -> list:
    """The kernel capabilities a service is granted, under either spelling.

    The field was `capabilities` until 0.16.0 and the contract still accepts that
    spelling, because a rename is not a reason to refuse to read somebody's own
    stack description. A fork that has not renamed its manifest is validated the
    same way this one is.
    """
    return service.get("grants", service.get("capabilities", []))


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
        where = f"profile {profile.get('id', UNNAMED)}"
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
        where = f"form {form.get('id', UNNAMED)}"
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
    for granted in grants_of(service):
        report.check(
            granted in ALLOWED_GRANTS,
            where,
            f"kernel capability {granted!r} is outside the allow-list "
            f"{sorted(ALLOWED_GRANTS)}",
            "C6",
        )
    for media_type in service.get("media_types", []):
        report.check(
            media_type in MEDIA_TYPES, where, f"unknown media type {media_type!r}"
        )
    if "memory_mib" in service:
        estimate = service["memory_mib"]
        report.check(
            isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0,
            where,
            f"memory_mib must be a whole number of MiB above zero, not {estimate!r}",
            "B1-R18",
        )


def validate_service_provides(service: dict, where: str, report: Report) -> None:
    """What this service can do, so that wiring can ask for it rather than name it.

    Two bundled services declaring the same capability is not a collision and is
    not checked for: it is what a vocabulary is for, and which of them fills it
    is the operator's to choose. A service declaring the same name twice is a
    typo, and is.
    """
    declared = service.get("provides")
    if declared is None:
        return
    if not report.check(
        isinstance(declared, list) and all(isinstance(name, str) for name in declared),
        where,
        "provides must be an array of capability names",
    ):
        return
    seen: set[str] = set()
    for name in declared:
        report.check(
            CORE_CAPABILITY.match(name) is not None,
            where,
            f"capability {name!r} is not a core name — those are `area.verb`, lowercase, "
            "with exactly one dot; a name carrying a colon belongs to the plugin whose "
            "id prefixes it",
            "F4-R4",
        )
        report.check(name not in seen, where, f"capability {name!r} is declared twice")
        seen.add(name)


def validate_service_errand(service: dict, where: str, report: Report) -> None:
    """Where a service reaches when it runs, and what it asks for when it gets there.

    One answer in two halves, so it is both fields or neither: a service saying
    where it goes without saying what for would be reported as half an errand,
    and one saying what it asks for without saying of whom attributes the errand
    to nobody.

    An empty `reaches` is an answer rather than a missing value — it is how a
    service that talks to nothing says so, and the three that do still say what
    they do instead. That is why `asks_for` is the half that may not be blank:
    there is no case where saying nothing is the way to say nothing.
    """
    present = [field for field in ("reaches", "asks_for") if field in service]
    if not present:
        return
    if len(present) == 1:
        missing = "asks_for" if present[0] == "reaches" else "reaches"
        report.fail(
            where,
            f"declares {present[0]!r} without {missing!r}; where a service reaches and what it "
            "asks for there are one answer, and half of it is attributed to nobody",
            "F2-R10",
        )
    for field in present:
        report.check(
            isinstance(service[field], str), where, f"{field} must be a string", "F2-R10"
        )
    if "asks_for" in service:
        report.check(
            bool(str(service["asks_for"]).strip()),
            where,
            "asks_for must say what it asks for, including where the answer is that it asks "
            "nothing; an empty reaches already says it goes nowhere",
            "F2-R10",
        )


def validate_errands(services: list, report: Report) -> None:
    """Either this manifest says what its services reach, or it does not.

    The pair is optional, so a stack that has written none of it down still
    validates; lemonfiber reports every one of its services as one nothing has
    described, which is a true answer about a stack that described nothing.

    A manifest that answers for some services and not others is the worse case,
    and the one refused here: silence about a service cannot be told from a
    service that reaches nothing, and the two are opposite claims about what
    leaves somebody's machine. Nothing else answers now — the table lemonfiber
    used to carry for the services it shipped is gone, which is what put this
    prose here — so a service left out of a manifest that answers for the rest
    is a service nobody will notice is undescribed.
    """
    answered = {
        str(service.get("id", UNNAMED))
        for service in services
        if "reaches" in service or "asks_for" in service
    }
    if not answered:
        return
    silent = sorted(
        str(service.get("id", UNNAMED))
        for service in services
        if str(service.get("id", UNNAMED)) not in answered
    )
    for sid in silent:
        report.fail(
            f"service {sid}",
            "says nothing about what it reaches, in a manifest where other services do; "
            "a service nobody has written this down for cannot be told from one that "
            "reaches nothing",
            "F2-R10",
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
    # Where lemonfiber and the services that ask reach it: the port it answers on
    # inside the stack's network, which is not always the one it publishes.
    listens = service.get("listens")
    report.check(
        isinstance(listens, int) and not isinstance(listens, bool) and 1 <= listens <= 65535,
        where,
        "declares an api but no listens, the port it answers on inside the stack's network",
        "ARCH-R144",
    )
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


def validate_recording(sid: str, fixture: str, pinned: str, where: str, report: Report) -> None:
    """One recording a claim names: kept with its own service's, and taken from
    the image the manifest pins.

    A recording of another build passes while describing software nobody is
    installing, which is the shape a pin moved without re-recording takes; so it
    is refused here rather than judged.
    """
    kept = f"{RECORDINGS}/{sid}/"
    beneath = fixture[len(kept):] if fixture.startswith(kept) else ""
    if not report.check(
        beneath != "" and ".." not in beneath.split("/"),
        where,
        f"fixture {fixture!r} is not under {kept}, where this service's recordings are kept",
        "ARCH-R136",
    ):
        return
    path = ROOT / fixture
    if not report.check(path.is_file(), where, f"fixture {fixture} names no recording here", "ARCH-R136"):
        return
    try:
        recorded = json.loads(path.read_text(encoding="utf-8")).get("recorded_from")
    except (ValueError, AttributeError):
        recorded = None
    report.check(
        recorded == pinned,
        where,
        f"recording {fixture} was recorded from {recorded!r}, and this service pins {pinned}; "
        "a recording of another build is evidence about software nobody is installing, so "
        "re-record it from the pinned image",
        "ARCH-R136",
    )


def validate_service_claims(service: dict, where: str, report: Report) -> None:
    """The evidence for what `provides` declares, held to the declaration.

    Which probes a claim must bind, and what each expectation may say, are the
    published vocabulary's to decide, and the core's judge holds them; this holds
    what the manifest alone can show. A claim names a capability the service
    provides, once, and every recording it names is this service's own and was
    taken from the image it pins.
    """
    claims = service.get("claim", [])
    if not report.check(
        isinstance(claims, list) and all(isinstance(claim, dict) for claim in claims),
        where,
        "claim must be an array of [[service.claim]] tables",
        "ARCH-R136",
    ):
        return
    sid = service.get("id", UNNAMED)
    provides = service.get("provides") or []
    pinned = by_digest(service)
    seen: set[str] = set()
    for claim in claims:
        capability = claim.get("capability")
        at = f"{where}.claim {capability}"
        if not report.check(isinstance(capability, str), where, "a claim names no capability", "ARCH-R136"):
            continue
        report.check(
            capability in provides,
            at,
            f"{capability} is not in this service's `provides`, so the claim demonstrates "
            "something the service has not said it can do",
            "ARCH-R136",
        )
        report.check(capability not in seen, at, f"{capability} is claimed twice", "ARCH-R136")
        seen.add(capability)
        probes = claim.get("probe", [])
        if not report.check(
            isinstance(probes, list) and all(isinstance(probe, dict) for probe in probes),
            at,
            "probe must be an array of [[service.claim.probe]] tables",
            "ARCH-R136",
        ):
            continue
        for probe in probes:
            probe_at = f"{at}.probe {probe.get('id', UNNAMED)}"
            fixture = probe.get("fixture")
            if report.check(isinstance(fixture, str), probe_at, "names no fixture", "ARCH-R136"):
                validate_recording(sid, fixture, pinned, probe_at, report)


def validate_service(service: dict, profile_ids: set[str], licences: set[str],
                     service_ids: set[str], profile_of: dict[str, str], report: Report) -> None:
    sid = service.get("id", UNNAMED)
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
        ":" not in str(service.get("image", "")) and "@" not in str(service.get("image", "")),
        where,
        "image must not carry a tag or a digest; use the separate `tag` and `digest` fields",
    )
    tag = str(service.get("tag", ""))
    report.check(
        tag.lower().lstrip("v") not in FLOATING_TAGS and tag != "",
        where,
        f"floating tag {tag!r}",
        "E1-R1",
    )
    # What runs. The tag is a label a publisher can move; the digest of the
    # multi-architecture index cannot move, and is what Compose resolves. Whether
    # it is an index, and the one the tag named, is asked of the registry by
    # check_images.py; the shape is all an offline check can see.
    if "digest" in service:
        report.check(
            bool(DIGEST.match(str(service["digest"]))),
            where,
            f"digest must be sha256: and 64 lowercase hex digits, got {service['digest']!r}",
            "E1-R1",
        )
    report.check(
        service.get("criticality") in CRITICALITIES,
        where,
        f"criticality must be one of {sorted(CRITICALITIES)}",
        "F2-R3",
    )
    licence = service.get("license")
    if rides_the_train(str(service.get("image", ""))):
        # Built by lemonfiber from its own code, so it carries lemonfiber's
        # licence, and nothing else.
        report.check(
            licence == OWN_LICENCE,
            where,
            f"licence {licence!r} on lemonfiber's own image; it carries lemonfiber's own, "
            f"{OWN_LICENCE}",
            "F2-R5",
        )
    else:
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
    validate_service_errand(service, where, report)
    validate_service_provides(service, where, report)
    validate_service_claims(service, where, report)
    validate_service_runtime(service, where, report)
    validate_service_health(service, where, report)
    validate_service_api(service, where, report)


def validate_dependencies(services: list, service_ids: set[str],
                          profile_of: dict[str, str], report: Report) -> None:
    # depends_on needs every id known, so it runs after the first pass.
    for service in services:
        sid = service.get("id", UNNAMED)
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


def validate_removals(removals: list, service_ids: set[str], report: Report) -> None:
    """What left the stack, why, and what took the job over.

    The table is optional — a stack that has removed nothing has nothing to
    declare — but an entry in it is not optional about its own fields. A removal
    recorded without a reason is the same silence as no record at all, read by a
    later operator as a service that simply stopped existing.

    `replaced_by` may name another removal as well as a live service: a
    replacement can itself be replaced, and an entry about the past should not
    have to be rewritten when that happens.
    """
    removed_ids: set[str] = set()
    for entry in removals:
        rid = str(entry.get("id", UNNAMED))
        where = f"removed {rid}"
        for field in ("id", "removed_in", "reason"):
            report.check(field in entry, where, f"missing required field {field!r}", "F2-R13")
        report.check(rid not in removed_ids, where, "duplicate removal id", "F2-R13")
        removed_ids.add(rid)
        report.check(
            rid not in service_ids,
            where,
            "names a service this stack still declares; a service is present or removed, not both",
            "F2-R13",
        )
        version = entry.get("removed_in")
        report.check(
            isinstance(version, str) and bool(SEMVER.match(version)),
            where,
            f"removed_in must be the stack version it went in, as semver; got {version!r}",
            "F2-R13",
        )
        reason = entry.get("reason")
        report.check(
            isinstance(reason, str) and bool(reason.strip()),
            where,
            "reason must say why it went; an empty one records nothing",
            "F2-R13",
        )

    for entry in removals:
        if "replaced_by" not in entry:
            continue
        rid, replacement = str(entry.get("id", UNNAMED)), entry["replaced_by"]
        where = f"removed {rid}"
        report.check(
            replacement != rid,
            where,
            f"replaced_by names {replacement!r}, which is the service that was removed",
            "F2-R13",
        )
        report.check(
            replacement in service_ids or replacement in removed_ids,
            where,
            f"replaced_by {replacement!r} is neither a service this stack declares nor a "
            "removal it records",
            "F2-R13",
        )
