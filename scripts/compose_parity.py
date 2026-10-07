"""The resolved Compose model held in parity with stack.toml: services, images,
profiles, mounts, bindings and the kernel capabilities a service is granted, and
lemonfiber's own services held to the containment their ADRs state.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess

from manifest_rules import grants_of
from pins import rides_the_train
from registry import pinned
from report import Report

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Interpolated into DATA_ROOT before resolving the model, so that "is this mount
# beneath the data root?" is an unambiguous test on an absolute path rather than
# a guess about how the source YAML spelled it.
DATA_ROOT_SENTINEL = "/__lemonfiber_data_root__"

# For each of lemonfiber's own services, the requirement that confines it, and
# every network it is on with every other service on that network, as its ADR
# states them (ADR-0033 §4). A service of lemonfiber's own with no entry here is
# refused: its containment is stated before it runs.
CONFINED: dict[str, tuple[str, dict[str, set[str]]]] = {
    # ADR-0032 §4: reachable only from Seerr, reaching only the services it acts
    # on, publishing nothing.
    "request-gate": ("C6-R22", {
        "requests-gate": {"seerr"},
        "gate-upstream": {"sonarr", "radarr", "jellyfin"},
    }),
    # ADR-0029 §6: one network shared only with Jellyfin, and the one its
    # published port needs.
    "decline": ("C6-R20", {
        "decline-upstream": {"jellyfin"},
        "decline": set(),
    }),
}
# What a service of lemonfiber's own with no entry in CONFINED is refused under.
UNSTATED = "ADR-0033"
# The bridge option that turns off address translation off the host: a network
# carrying a published port still answers, and reaches nothing beyond the host.
NO_MASQUERADE = "com.docker.network.bridge.enable_ip_masquerade"
# A service whose only path to some others is through one of lemonfiber's own:
# Seerr reaches Sonarr, Radarr and Jellyfin through the request gate and no other
# way (C6-R22).
ONLY_THROUGH: dict[str, tuple[str, set[str]]] = {
    "seerr": ("request-gate", {"sonarr", "radarr", "jellyfin"}),
}
ROOT_USERS = {"0", "root"}


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

        expected_image = pinned(spec)
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
        validate_grants(sid, spec, service, report)

    validate_bindings(declared, compose, gateway_of, report)
    validate_listening(declared, compose, gateway_of, report)
    validate_gateways(declared, compose, gateway_of, report)
    validate_confinement(declared, model, report)


def networks_of(service: dict) -> set[str]:
    """The networks a resolved service is on; the resolved model names `default`
    for a service whose entry names none."""
    return set(service.get("networks") or {"default": None})


def confined_runtime(sid: str, service: dict, requirement: str, report: Report) -> None:
    """What every one of lemonfiber's own services runs with: a read-only root, a
    user that is not root, no kernel capability, no privilege gained after start,
    a memory limit, and its own configuration directory as its one mount
    (ADR-0029 §6, ADR-0032 §4)."""
    where = f"service {sid}"
    report.check(service.get("read_only") is True, where,
                 "runs with a writable root; lemonfiber's own images run read_only", requirement)
    report.check("ALL" in (service.get("cap_drop") or []), where,
                 "keeps kernel capabilities; lemonfiber's own images drop ALL", requirement)
    report.check(
        any(str(option).replace("=", ":") in {"no-new-privileges", "no-new-privileges:true"}
            for option in service.get("security_opt") or []),
        where, "can gain privileges after it starts; set no-new-privileges", requirement,
    )
    report.check(bool(service.get("mem_limit")), where, "runs with no memory limit", requirement)
    user = str(service.get("user") or "")
    report.check(bool(user) and user.split(":", 1)[0] not in ROOT_USERS, where,
                 f"runs as {user or 'the image default'!r}; run it as ${{PUID}}:${{PGID}}", requirement)
    mounts = service.get("volumes") or []
    own = [mount for mount in mounts
           if mount.get("type") == "bind" and mount.get("target") == "/config"
           and mount.get("source") == str(ROOT / "config" / sid)]
    report.check(
        len(mounts) == 1 and len(own) == 1, where,
        f"mounts {sorted(str(mount.get('target')) for mount in mounts)}; its one mount is "
        f"./config/{sid}:/config, with no data root and no engine socket", requirement,
    )


def confined_networks(sid: str, spec: dict, compose: dict, networks: dict, internal: set[str],
                      requirement: str, stated: dict[str, set[str]], report: Report) -> None:
    """The networks its ADR puts it on, and nothing else on them but the services
    the ADR names. A service that publishes nothing sits on internal networks only,
    and one that publishes a port has one network that is not internal for it."""
    where = f"service {sid}"
    joined = networks_of(compose[sid])
    report.check(joined == set(stated), where,
                 f"is on {sorted(joined)}; its ADR puts it on {sorted(stated)}", requirement)
    for network in sorted(joined & set(stated)):
        peers = {other for other, service in compose.items()
                 if other != sid and network in networks_of(service)}
        report.check(peers == stated[network], where,
                     f"shares {network!r} with {sorted(peers)}; its ADR says {sorted(stated[network])}",
                     requirement)
    open_networks = sorted(joined - internal)
    wanted = 1 if "port" in spec else 0
    report.check(
        len(open_networks) == wanted, where,
        f"is on {len(open_networks)} network(s) that are not internal ({open_networks}); "
        f"{'one carries its published port' if wanted else 'it publishes nothing and has no egress'}",
        requirement,
    )
    # A published port needs a network that is not internal, and such a network
    # routes off the host unless it translates no address there. Without that
    # translation the port still answers, and the service cannot reach out.
    for network in open_networks:
        options = (networks.get(network) or {}).get("driver_opts") or {}
        report.check(
            str(options.get(NO_MASQUERADE, "")).lower() == "false", where,
            f"publishes on {network!r}, which reaches off the host; set {NO_MASQUERADE}: "
            '"false" on it so the service has no egress', requirement,
        )


def validate_confinement(declared: dict, model: dict, report: Report) -> None:
    """lemonfiber's own services run as their ADRs confine them (ADR-0033 §4)."""
    compose = model.get("services", {})
    networks = model.get("networks") or {}
    internal = {name for name, network in networks.items() if (network or {}).get("internal")}
    for sid, spec in sorted(declared.items()):
        if sid not in compose or not rides_the_train(str(spec.get("image", ""))):
            continue
        requirement, stated = CONFINED.get(sid, (UNSTATED, None))
        confined_runtime(sid, compose[sid], requirement, report)
        if report.check(stated is not None, f"service {sid}",
                        "is lemonfiber's own image and its containment is not stated in CONFINED",
                        UNSTATED):
            confined_networks(sid, spec, compose, networks, internal, requirement, stated, report)

    for sid, (gate, upstreams) in sorted(ONLY_THROUGH.items()):
        if sid not in compose or gate not in compose:
            continue
        reached = {other for other, service in compose.items() if other in upstreams
                   and networks_of(service) & networks_of(compose[sid])}
        report.check(not reached, f"service {sid}",
                     f"shares a network with {sorted(reached)}, which it reaches only through "
                     f"{gate}", "C6-R22")


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


def validate_grants(sid: str, spec: dict, service: dict, report: Report) -> None:
    declared_caps = set(grants_of(spec))
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


def validate_listening(declared: dict, compose: dict, gateway_of: dict, report: Report) -> None:
    """A service's `listens` is the inside end of the port it publishes, where it publishes one."""
    for sid, spec in sorted(declared.items()):
        listens, port = spec.get("listens"), spec.get("port")
        if listens is None or port is None:
            continue
        publisher = compose.get(gateway_of.get(sid, sid), {})
        for mapping in publisher.get("ports") or []:
            if str(mapping.get("published")) != str(port):
                continue
            report.check(
                str(mapping.get("target")) == str(listens),
                f"service {sid}",
                f"listens on {listens}, but the port it publishes, {port}, reaches "
                f"{mapping.get('target')} inside its container",
                "ARCH-R144",
            )


def validate_gateways(declared: dict, compose: dict, gateway_of: dict, report: Report) -> None:
    """A service granted NET_ADMIN is a tunnel; its profile-mates must route through it."""
    for gateway, spec in sorted(declared.items()):
        if "NET_ADMIN" not in grants_of(spec):
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
