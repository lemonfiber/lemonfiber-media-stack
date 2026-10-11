"""The resolved Compose model held to what each service's declared container
fields say: its ports, mounts, environment, user, posture, shape, memory limit
and the network it runs inside (ADR-0043).

Each service file declares its container in the one service model lemonfiber
writes every Compose entry from. Until lemonfiber writes them, the Compose files
here are what runs, so every difference between the two is named and fails,
except the ones `OPEN` holds: a Compose entry saying something the model has no
field for, each kept here with the question that decides it.
"""

from __future__ import annotations

import pathlib
import re

from container_rules import CONFIGURATION, DATA, KEPT, SHAPES, mistyped, user_of, written_posture
from report import Report

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Interpolated into DATA_ROOT before resolving the model, so that "is this mount
# beneath the data root?" is an unambiguous test on an absolute path rather than
# a guess about how the source YAML spelled it.
DATA_ROOT_SENTINEL = "/__lemonfiber_data_root__"

# What every entry carries that no manifest declares: the operator's time zone,
# and the restart policy.
TIME_ZONE = "TZ"
RESTART = "unless-stopped"
# Who the operator is, as an entry names them.
OPERATOR = "${PUID:-1000}:${PGID:-1000}"
OPERATOR_ENV = {"PUID": "${PUID:-1000}", "PGID": "${PGID:-1000}"}
MIB = 1024 * 1024
# A setting, as an entry reads it from the stack's environment file today.
FROM_ENV_FILE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):-(.*)\}$")
# Where a service's further mounts come from: its own mount tree.
MOUNTS = "mounts"
CONFIG = "config"

# Further mounts whose source is still inside the service's configuration
# directory, which is where the Compose files keep them; lemonfiber writes them
# from the service's mount tree, ./mounts/<id>/<from>, which no container sees
# but through that one mount.
MOVING: set[tuple[str, str]] = {
    ("audiobookshelf", "metadata"),
    ("caddy", "Caddyfile"),
    ("jellyfin", "cache"),
}

POSTURE = (
    "no Compose entry is written with the posture the model writes: the standard set of six kernel "
    "capabilities, no-new-privileges and a limit of 1024 processes, and the confined entries without "
    "the process limit"
)
DOOR_POSTURE = (
    "the door runs with a read-only root and keeps the engine's default kernel capabilities, which is "
    "neither posture; the Caddy binary carries NET_BIND_SERVICE as a file capability and will not "
    "execute where the bounding set lacks it, so it fails under standard and under confined alike"
)
PROXY_STATE = (
    "the proxy keeps its certificates and state in the named volumes caddy_data and caddy_config, "
    "and the model has no named volume; its configuration directory is not mounted at all"
)
NO_CONFIGURATION = "the entry mounts no configuration directory, and the model always mounts one"
PORT_PUSH = (
    "the forwarded-port push: a script mounted from ./scripts, outside the service's own tree, the "
    "commands that run it, and qBittorrent's login, which is another service's setting and a credential"
)
SECRET = "a credential read from the stack's environment file; a stack service declares no [[secret]]"

# Each difference between an entry and its declaration that is not yet decided,
# by service and what differs, with the question that decides it. `*` is every
# service. An entry here that no longer differs fails, so none outlives its
# question.
OPEN: dict[tuple[str, str], str] = {
    ("*", "posture"): POSTURE,
    ("door", "posture"): DOOR_POSTURE,
    ("caddy", "mount /config"): PROXY_STATE,
    ("caddy", "mount /data"): PROXY_STATE,
    ("flaresolverr", "mount /config"): NO_CONFIGURATION,
    ("gluetun", "mount /config"): NO_CONFIGURATION,
    ("gluetun", "mount /gluetun/push-port.sh"): PORT_PUSH,
    ("gluetun", "env VPN_PORT_FORWARDING_UP_COMMAND"): PORT_PUSH,
    ("gluetun", "env VPN_PORT_FORWARDING_DOWN_COMMAND"): PORT_PUSH,
    ("gluetun", "env QBT_USERNAME"): PORT_PUSH,
    ("gluetun", "env QBT_PASSWORD"): PORT_PUSH,
    ("gluetun", "env WIREGUARD_PRIVATE_KEY"): SECRET,
    ("bindery", "env BINDERY_API_KEY"): SECRET,
    ("recyclarr", "env SONARR_API_KEY"): SECRET,
    ("recyclarr", "env RADARR_API_KEY"): SECRET,
    ("unpackerr", "env UN_SONARR_0_API_KEY"): SECRET,
    ("unpackerr", "env UN_RADARR_0_API_KEY"): SECRET,
    ("unpackerr", "env UN_LIDARR_0_API_KEY"): SECRET,
}


def environment(service: dict) -> dict[str, str]:
    """An entry's environment as `name -> value`, however Compose wrote it."""
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {name: "" if value is None else str(value) for name, value in raw.items()}
    return dict(item.split("=", 1) if "=" in item else (item, "") for item in raw)


def constant(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def filler(declared: dict, capability: str, files: str | None = None) -> str | None:
    """The one declared service that fills a capability, narrowed by medium."""
    found = [
        sid for sid, spec in declared.items()
        if capability in spec.get("provides", []) and (files is None or files in spec.get("media_types", []))
    ]
    return found[0] if len(found) == 1 else None


def publisher(sid: str, declared: dict) -> str | None:
    """The entry that publishes a service's ports: its own, or its network's."""
    via = (declared[sid].get("network") or {}).get("via")
    return filler(declared, via) if via else sid


class Differences:
    """Every difference found, by service and what differs."""

    def __init__(self) -> None:
        self.found: dict[tuple[str, str], list[str]] = {}

    def add(self, sid: str, aspect: str, message: str) -> None:
        self.found.setdefault((sid, aspect), []).append(message)

    def same(self, sid: str, aspect: str, entry: object, declared: object, what: str) -> None:
        if entry != declared:
            self.add(sid, aspect, f"the entry {what} {entry!r}, and the declaration says {declared!r}")


def ports(declared: dict, compose: dict, found: Differences) -> None:
    expected: dict[str, set[tuple[int, int]]] = {}
    for sid, spec in declared.items():
        if "port" not in spec:
            continue
        pairs = {(spec["port"], spec.get("listens", spec["port"]))}
        pairs |= {(entry["port"], entry.get("listens", entry["port"])) for entry in spec.get("publish", [])}
        owner = publisher(sid, declared)
        if owner is None:
            found.add(sid, "ports", "runs inside a network nothing declared fills, so nothing publishes it")
            continue
        expected.setdefault(owner, set()).update(pairs)
    for sid in sorted(set(compose) & set(declared)):
        entry = {
            (int(spec["published"]), int(spec["target"]))
            for spec in compose[sid].get("ports") or [] if spec.get("published") not in (None, "")
        }
        found.same(sid, "ports", sorted(entry), sorted(expected.get(sid, set())), "publishes host:container")


def mounts(sid: str, spec: dict, entry: dict, found: Differences) -> None:
    expected: dict[str, tuple[str, str, bool]] = {}
    if spec.get("takes_data"):
        expected[DATA] = ("bind", DATA_ROOT_SENTINEL, False)
    expected[spec.get("config_path", CONFIGURATION)] = (
        "bind", str(ROOT / CONFIG / sid), bool(spec.get("config_ro", False)),
    )
    for mount in spec.get("mount", []):
        tree = CONFIG if (sid, mount["from"]) in MOVING else MOUNTS
        expected[mount["to"]] = ("bind", str(ROOT / tree / sid / mount["from"]), bool(mount.get("ro", False)))
    actual = {
        volume.get("target"): (volume.get("type"), str(volume.get("source")), bool(volume.get("read_only", False)))
        for volume in entry.get("volumes") or []
    }
    for target in sorted(set(expected) | set(actual)):
        found.same(sid, f"mount {target}", actual.get(target), expected.get(target), "mounts (type, source, ro)")
    tmpfs = sorted(str(path).split(":", 1)[0] for path in entry.get("tmpfs") or [])
    found.same(sid, "tmpfs", tmpfs, sorted(spec.get("tmpfs", [])), "keeps scratch space at")


def env(sid: str, spec: dict, raw: dict, declared: dict, settings: dict, found: Differences) -> None:
    written = environment(raw)
    found.same(sid, "defaults", (TIME_ZONE in written, raw.get("restart")), (True, RESTART),
               "writes (TZ, restart)")
    entry = {name: value for name, value in written.items() if name not in (TIME_ZONE, *OPERATOR_ENV)}
    declaration = spec.get("env", {})
    for name in sorted(set(entry) | set(declaration)):
        at = f"env {name}"
        if name not in declaration:
            found.add(sid, at, f"the entry sets {name}, and the declaration does not")
            continue
        if name not in entry:
            found.add(sid, at, f"the declaration sets {name}, and the entry does not")
            continue
        value, have = declaration[name], entry[name]
        if not isinstance(value, dict):
            found.same(sid, at, have, constant(value), "sets it to the constant")
        elif "setting" in value:
            read = FROM_ENV_FILE.match(have)
            if read is None:
                found.add(sid, at, f"the entry sets {have!r}, which reads no variable, and the declaration "
                          f"reads setting {value['setting']}")
                continue
            found.same(sid, at, read.group(2), constant(value["default"]), "defaults it to")
            variable = read.group(1)
            if settings.setdefault(variable, value["setting"]) != value["setting"]:
                found.add(sid, at, f"reads {variable}, which another declaration reads as setting "
                          f"{settings[variable]}, and this one as {value['setting']}")
        elif "url_of" in value:
            reached = filler(declared, value["url_of"], value.get("files"))
            if reached is None:
                found.add(sid, at, f"nothing declared fills {value['url_of']} for {value.get('files')}")
                continue
            listens = declared[reached].get("listens", declared[reached].get("port"))
            found.same(sid, at, have, f"http://{reached}:{listens}", "sets the address")


def user(sid: str, spec: dict, raw: dict, found: Differences) -> None:
    mode = user_of(spec)
    written = environment(raw)
    found.same(sid, "user", raw.get("user"), OPERATOR if mode == "uid" else None, "runs as")
    pair = {name: written.get(name) for name in OPERATOR_ENV}
    wanted = OPERATOR_ENV if mode == "puid-env" else dict.fromkeys(OPERATOR_ENV)
    found.same(sid, "user", pair, wanted, "sets PUID and PGID to")


def posture(sid: str, spec: dict, entry: dict, found: Differences) -> None:
    held = {
        "cap_drop": sorted(entry.get("cap_drop") or []),
        "cap_add": sorted(entry.get("cap_add") or []),
        "security_opt": sorted(entry.get("security_opt") or []),
        "pids_limit": entry.get("pids_limit"),
        "read_only": bool(entry.get("read_only", False)),
    }
    found.same(sid, "posture", held, written_posture(spec), "holds")


def shape(sid: str, spec: dict, entry: dict, found: Differences) -> None:
    taken = SHAPES.get(spec.get("shape"), {"grants": (), "devices": ()})
    devices = sorted(str(device.get("source")) for device in entry.get("devices") or [])
    added = sorted(set(entry.get("cap_add") or []) - set(KEPT))
    found.same(sid, "shape", (added, devices, bool(entry.get("group_add"))),
               (sorted(taken["grants"]), sorted(taken["devices"]), spec.get("shape") == "render"),
               "adds (capabilities, devices, groups)")


def held_and_routed(sid: str, spec: dict, entry: dict, declared: dict, found: Differences) -> None:
    limit = entry.get("mem_limit")
    found.same(sid, "mem_limit", int(limit) if limit else None,
               spec["mem_limit_mib"] * MIB if "mem_limit_mib" in spec else None, "limits memory to")
    via = (spec.get("network") or {}).get("via")
    guard = filler(declared, via) if via else None
    if via and guard is None:
        found.add(sid, "network", f"runs inside {via}, which no one declared service fills")
    wanted = (f"service:{guard}", {guard: "service_healthy"}) if guard else (None, {})
    order = {name: (edge or {}).get("condition") for name, edge in (entry.get("depends_on") or {}).items()}
    found.same(sid, "network", (entry.get("network_mode"), order), wanted, "runs in (network_mode, depends_on)")


def judge(found: Differences, report: Report) -> None:
    """Fail each difference nothing holds open, note each one that is, and fail
    each open entry that no longer differs."""
    held: dict[tuple[str, str], list[str]] = {}
    for (sid, aspect), messages in sorted(found.found.items()):
        key = next((k for k in ((sid, aspect), ("*", aspect)) if k in OPEN), None)
        if key is None:
            for message in messages:
                report.fail(f"service {sid}", f"{aspect}: {message}", "ARCH-R246")
        elif key[0] == "*":
            held.setdefault(key, []).append(sid)
        else:
            held.setdefault(key, [])
            for message in messages:
                report.note(f"service {sid}", f"{aspect}: {message}; open: {OPEN[key]}")
    for (sid, aspect), services in sorted(held.items()):
        if sid == "*":
            report.note(f"{len(services)} services", f"{aspect} differs ({', '.join(services)}); "
                        f"open: {OPEN[sid, aspect]}")
    for sid, aspect in sorted(set(OPEN) - set(held)):
        report.fail(f"service {sid}", f"{aspect} is held open in model_parity.OPEN and no longer differs; "
                    "take it out", "ARCH-R246")


def validate_model(declared: dict, model: dict, raw_model: dict, report: Report) -> None:
    """Every entry against its declaration, for each declaration whose fields read
    as the model reads them; the manifest half names those that do not."""
    declared = {sid: spec for sid, spec in declared.items() if not mistyped(spec, "")}
    compose, raw = model.get("services", {}), raw_model.get("services", {})
    found = Differences()
    settings: dict[str, str] = {}
    ports(declared, compose, found)
    for sid in sorted(set(declared) & set(compose) & set(raw)):
        spec, entry = declared[sid], compose[sid]
        mounts(sid, spec, entry, found)
        env(sid, spec, raw[sid], declared, settings, found)
        user(sid, spec, raw[sid], found)
        posture(sid, spec, entry, found)
        shape(sid, spec, entry, found)
        held_and_routed(sid, spec, entry, declared, found)
    judge(found, report)
