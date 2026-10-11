"""A service's container fields, held to the one service model every Compose
entry is written from (spec 20-architecture/contracts/plugin-manifest-service.md,
ADR-0043).

The rules are lemonfiber's own, read here as the reader in
`crates/lemonfiber-manifest/src/container.rs` states them: `Container::broken()`
for the fields, and `contained()` in `crates/lemonfiber-manifest/src/validate.rs`
for the shape a stack service may take. Each rule refuses what that reader
refuses, naming the field and the fault as that reader does.
"""

from __future__ import annotations

import re

from report import UNNAMED, Report

# Where a service's configuration directory lands when it names nowhere.
CONFIGURATION = "/config"
# Where the single data mount lands inside every container that takes data.
DATA = "/data"
# The variables lemonfiber writes itself, which no manifest may name.
RESERVED = ("TZ", "PUID", "PGID")
# The settings the core publishes to every service.
PUBLISHED = ("domain", "lan-host")
EGRESS_GUARD = "network.egress-guard"
MEDIA_SERVE = "media.serve"
# The capabilities a service may run inside the network of.
VIA = (EGRESS_GUARD,)
USER_MODES = ("image", "puid-env", "uid")
POSTURES = ("standard", "confined")
# The kernel capabilities `standard` gives back after dropping every one.
KEPT = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "KILL")
# How many processes `standard` lets a container hold at once.
PROCESSES = 1024
# What `standard` writes so that no privilege is gained after start.
NO_NEW_PRIVILEGES = "no-new-privileges:true"
# What each shape adds, and the capability a service taking it provides.
SHAPES: dict[str, dict] = {
    "egress-guard": {"grants": ("NET_ADMIN",), "devices": ("/dev/net/tun",), "serves": EGRESS_GUARD},
    "render": {"grants": (), "devices": ("/dev/dri",), "serves": MEDIA_SERVE},
}
# The fields a stack service is read with; any other is refused, as the reader
# refuses an unknown field.
SERVICE_FIELDS = frozenset({
    "id", "name", "profile", "image", "tag", "digest", "port", "bind", "health", "api", "listens",
    "criticality", "license", "upstream", "last_release", "describes", "without_it", "reaches",
    "asks_for", "media_types", "provides", "claim", "host_managed", "memory_mib",
    "publish", "takes_data", "config_path", "config_ro", "mount", "tmpfs", "env", "user_mode",
    "posture", "mem_limit_mib", "network", "shape",
    "grants", "capabilities", "depends_on",
})
# Read so that each can be refused naming what replaces it (ARCH-R246).
RETIRED = {
    "grants": "a kernel capability is a shape's to give",
    "capabilities": "a kernel capability is a shape's to give",
    "depends_on": "the one start-order edge the model writes is network.via's",
}
CORE_WORD = r"[a-z](?:[a-z0-9-]*[a-z0-9])?"
CORE_NAME = re.compile(rf"^{CORE_WORD}\.{CORE_WORD}$")
PLAIN = re.compile(r"^[A-Za-z0-9._/-]*$")
VARIABLE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
NOT_PLAIN = (
    'is not one plain absolute path: not the root, with no "..", in letters, digits and "._/-" alone'
)
NO_KIND = (
    "is none of the four kinds of value: a constant, { setting, default }, { url_of, files, native } "
    "or { secret }"
)


def is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_port(value: object) -> bool:
    return is_int(value) and 1 <= value <= 65535


def is_constant(value: object) -> bool:
    return isinstance(value, (str, bool)) or is_int(value)


def kind_of(value: object) -> str | None:
    """Which of the four kinds an environment value is, or None for none of them."""
    if is_constant(value):
        return "constant"
    if not isinstance(value, dict):
        return None
    keys = set(value)
    if keys == {"setting", "default"} and isinstance(value["setting"], str) and is_constant(value["default"]):
        return "setting"
    if "url_of" in keys and keys <= {"url_of", "files", "native"} and all(
        isinstance(value[key], str) for key in keys
    ):
        return "url_of"
    if keys == {"secret"} and isinstance(value["secret"], str):
        return "secret"
    return None


def user_of(service: dict) -> str:
    """Who it runs as: the declared mode, or `uid` where it is confined, else `image`."""
    declared = service.get("user_mode")
    if declared is not None:
        return declared
    return "uid" if service.get("posture") == "confined" else "image"


def target(path: str, takes_data: bool) -> str | None:
    """What is wrong with `path` as a target inside a container, if anything."""
    if not path.startswith("/") or path == "/" or ".." in path or not PLAIN.match(path):
        return NOT_PLAIN
    in_data = path == DATA or path.startswith(f"{DATA}/")
    return "is inside the data mount" if takes_data and in_data else None


def is_relative(path: str) -> bool:
    return (
        path != ""
        and not path.startswith("/")
        and PLAIN.match(path) is not None
        and all(step not in ("", ".", "..") for step in path.split("/"))
    )


def shape_granting(granted: str) -> str | None:
    """The shape that gives a kernel capability, if any does."""
    return next((name for name, shape in SHAPES.items() if granted in shape["grants"]), None)


def retired(service: dict, where: str, report: Report) -> None:
    """A field the model replaced, refused naming what replaces it."""
    for name, replaced in RETIRED.items():
        if name not in service:
            continue
        instead = ""
        if name != "depends_on":
            named = [
                f"{granted} is {shape}'s" if (shape := shape_granting(str(granted))) else f"no shape gives {granted}"
                for granted in service.get(name) or []
            ]
            instead = f"; {', '.join(named)}" if named else ""
        report.fail(where, f"declares {name}, which is not a field: {replaced}{instead}", "ARCH-R246")


TYPED = (
    ("publish", lambda v: isinstance(v, list) and all(
        isinstance(e, dict) and set(e) <= {"port", "listens"} and is_port(e.get("port"))
        and ("listens" not in e or is_port(e["listens"])) for e in v),
     "an array of tables holding a port and, where it differs, listens"),
    ("takes_data", lambda v: isinstance(v, bool), "a boolean"),
    ("config_path", lambda v: isinstance(v, str), "a string"),
    ("config_ro", lambda v: isinstance(v, bool), "a boolean"),
    ("mount", lambda v: isinstance(v, list) and all(
        isinstance(e, dict) and set(e) <= {"from", "to", "ro"} and isinstance(e.get("from"), str)
        and isinstance(e.get("to"), str) and isinstance(e.get("ro", False), bool) for e in v),
     "an array of tables holding from, to and, where it is, ro"),
    ("tmpfs", lambda v: isinstance(v, list) and all(isinstance(e, str) for e in v), "an array of paths"),
    ("env", lambda v: isinstance(v, dict), "a table of variables"),
    ("user_mode", lambda v: v in USER_MODES, f"one of {list(USER_MODES)}"),
    ("posture", lambda v: v in POSTURES, f"one of {list(POSTURES)}"),
    ("mem_limit_mib", lambda v: is_int(v) and 0 <= v < 2**32, "a whole number of MiB"),
    ("network", lambda v: isinstance(v, dict) and set(v) == {"via"} and isinstance(v["via"], str),
     "a table holding via alone"),
    ("shape", lambda v: v in SHAPES, f"one of {sorted(SHAPES)}"),
)


def mistyped(service: dict, where: str) -> list[tuple[str, str]]:
    """Each container field not of the type the model reads it as."""
    found = [
        (where, f"{name} must be {wanted}, not {service[name]!r}")
        for name, ok, wanted in TYPED
        if name in service and not ok(service[name])
    ]
    env = service.get("env")
    if isinstance(env, dict):
        found += [
            (f"{where}.env.{name}", NO_KIND)
            for name, value in env.items()
            if kind_of(value) is None
        ]
    return found


def validate_service_fields(service: dict, where: str, report: Report) -> None:
    """Every field the model reads, of the type it reads, and no field it does not."""
    for name in sorted(set(service) - SERVICE_FIELDS):
        report.fail(where, f"declares {name!r}, which is not a field of a stack service", "ARCH-R225")
    retired(service, where, report)
    for at, message in mistyped(service, where):
        report.fail(at, message, "ARCH-R225")


def published(service: dict, at, report: Report) -> None:
    further = [entry["port"] for entry in service.get("publish", [])]
    if "port" not in service and further:
        report.fail(at("publish"), "publishes further ports but declares no port", "ARCH-R226")
    seen: list[int] = []
    for port in ([service["port"]] if "port" in service else []) + further:
        if port in seen:
            report.fail(at("publish"), f"publishes {port} twice", "ARCH-R226")
        seen.append(port)


def mounted(service: dict, at, report: Report) -> None:
    takes_data = service.get("takes_data", False)
    if "config_path" in service and (wrong := target(service["config_path"], True)):
        report.fail(at("config_path"), f"{service['config_path']} {wrong}", "ARCH-R227")
    targets = [service.get("config_path", CONFIGURATION)]

    def landed(where: str, to: str) -> None:
        if wrong := target(to, takes_data):
            report.fail(where, f"{to} {wrong}", "ARCH-R227")
        if to in targets:
            report.fail(where, f"{to} is the target of another mount", "ARCH-R227")
        targets.append(to)

    for mount in service.get("mount", []):
        where = at(f"mount {mount['to']}")
        if not is_relative(mount["from"]):
            report.fail(
                where, f"{mount['from']} is not a relative path inside its own configuration directory",
                "ARCH-R227",
            )
        landed(where, mount["to"])
    for path in service.get("tmpfs", []):
        landed(at(f"tmpfs {path}"), path)


def has_control(value: object) -> bool:
    return isinstance(value, str) and any(ord(letter) < 32 or 127 <= ord(letter) < 160 for letter in value)


def valued(value: object, namespace: str) -> list[tuple[str, str]]:
    """What is wrong with one environment value, each fault with its requirement."""
    kind = kind_of(value)
    if kind == "constant":
        return [(f"{value!r} holds a control character", "ARCH-R230")] if has_control(value) else []
    found = []
    if kind == "setting":
        if has_control(value["default"]):
            found.append((f"{value['default']!r} holds a control character", "ARCH-R230"))
        setting = value["setting"]
        space, _, rest = setting.partition(":")
        if setting not in PUBLISHED and not (space == namespace and rest):
            refused = f"names setting {setting}, which is neither one the core publishes nor in the {namespace} namespace"
            found.append((refused, "ARCH-R231"))
    elif kind == "url_of" and not CORE_NAME.match(value["url_of"]):
        found.append((f"asks for the address of {value['url_of']}, which is not a core capability", "ARCH-R232"))
    elif kind == "secret":
        found.append((f"names secret {value['secret']}, which the manifest does not declare", "ARCH-R247"))
    return found


def environed(service: dict, at, report: Report) -> None:
    namespace = service.get("id", UNNAMED)
    for name, value in service.get("env", {}).items():
        where = at(f"env.{name}")
        if not VARIABLE.match(name):
            report.fail(where, 'is not upper-case letters, digits and "_", starting with a letter or "_"',
                        "ARCH-R229")
        if name in RESERVED:
            report.fail(where, f"{name} is lemonfiber's to write", "ARCH-R229")
        for message, requirement in valued(value, namespace):
            report.fail(where, message, requirement)


def held(service: dict, at, report: Report) -> None:
    confined = service.get("posture") == "confined"
    if confined and user_of(service) != "uid":
        report.fail(at("user_mode"), "must be uid for a service that speaks a contract or is confined",
                    "ARCH-R233")
    if confined and "shape" in service:
        report.fail(at("shape"), f"is confined and takes {service['shape']}, which a confined service may not",
                    "ARCH-R234")
    if service.get("mem_limit_mib") == 0:
        report.fail(at("mem_limit_mib"), "is zero", "ARCH-R235")


def routed(service: dict, at, report: Report) -> None:
    via = (service.get("network") or {}).get("via")
    if via is None:
        return
    if via not in VIA:
        report.fail(at("network.via"), f"names {via}, which no service may run inside the network of",
                    "ARCH-R236")
    if via in service.get("provides", []):
        report.fail(at("network.via"), f"names {via}, which the service provides itself", "ARCH-R236")


def shaped(service: dict, at, report: Report) -> None:
    shape = service.get("shape")
    if shape is not None and SHAPES[shape]["serves"] not in service.get("provides", []):
        report.fail(at("shape"), f"takes {shape}, which only a service providing {SHAPES[shape]['serves']} may",
                    "ARCH-R237")


def validate_service_container(service: dict, report: Report) -> None:
    """Every rule its container fields break, each as where and what."""
    if mistyped(service, ""):
        return
    sid = service.get("id", UNNAMED)

    def at(field: str) -> str:
        return f"service {sid}.{field}"

    for rule in (published, mounted, environed, held, routed, shaped):
        rule(service, at, report)


def written_posture(service: dict) -> dict:
    """The Compose keys a service's posture is written as: every kernel capability
    dropped, the six `standard` keeps and what its shape adds given back, no
    privilege gained after start and a process limit; `confined` gives none back
    and has a read-only root."""
    confined = service.get("posture") == "confined"
    grants = SHAPES[service["shape"]]["grants"] if service.get("shape") in SHAPES else ()
    return {
        "cap_drop": ["ALL"],
        "cap_add": sorted(grants if confined else (*KEPT, *grants)),
        "security_opt": [NO_NEW_PRIVILEGES],
        "pids_limit": PROCESSES,
        "read_only": confined,
    }
