"""Negative tests for the service model: each container field held to the model's
rules (container_rules.py). Run by test_validate_manifest.py, beside its own.
"""

from __future__ import annotations

from test_validate_manifest import field, patch

CASES = [
    (
        "ARCH-R225 a field the model does not read",
        field("sonarr", "takes_data", "takes_data = true\ntakes-data = true"),
        "service sonarr: declares 'takes-data', which is not a field of a stack service",
    ),
    (
        "ARCH-R225 a field of the wrong type",
        field("sonarr", "user_mode", 'user_mode = "root"'),
        "service sonarr: user_mode must be one of ['image', 'puid-env', 'uid'], not 'root'",
    ),
    (
        "ARCH-R229 a value of none of the four kinds",
        field("seerr", "LOG_LEVEL", 'LOG_LEVEL = { setting = "seerr:log-level" }'),
        "service seerr.env.LOG_LEVEL: is none of the four kinds of value",
    ),
    (
        "ARCH-R226 further ports without a port",
        field("request-gate", "posture", 'posture = "confined"\npublish = [{ port = 9999 }]'),
        "service request-gate.publish: publishes further ports but declares no port",
    ),
    (
        "ARCH-R226 one port published twice",
        patch("services/caddy.toml", "port = 443", "port = 80"),
        "service caddy.publish: publishes 80 twice",
    ),
    (
        "ARCH-R227 a configuration directory inside the data mount",
        field("homepage", "config_path", 'config_path = "/data/homepage"'),
        "service homepage.config_path: /data/homepage is inside the data mount",
    ),
    (
        "ARCH-R227 a configuration directory that is not an absolute path",
        field("seerr", "config_path", 'config_path = "app/config"'),
        "service seerr.config_path: app/config is not one plain absolute path",
    ),
    (
        "ARCH-R227 a mount from outside the service's own tree",
        patch("services/jellyfin.toml", 'from = "cache"', 'from = "/etc"'),
        "service jellyfin.mount /cache: /etc is not a relative path inside its own configuration directory",
    ),
    (
        "ARCH-R227 a mount climbing out of the service's own tree",
        patch("services/audiobookshelf.toml", 'from = "metadata"', 'from = "../sonarr"'),
        "service audiobookshelf.mount /metadata: ../sonarr is not a relative path",
    ),
    (
        "ARCH-R227 a mount landing inside the data mount",
        patch("services/jellyfin.toml", 'to = "/cache"', 'to = "/data/cache"'),
        "service jellyfin.mount /data/cache: /data/cache is inside the data mount",
    ),
    (
        "ARCH-R227 a mount landing on the configuration directory",
        patch("services/jellyfin.toml", 'to = "/cache"', 'to = "/config"'),
        "service jellyfin.mount /config: /config is the target of another mount",
    ),
    (
        "ARCH-R227 scratch space landing on the configuration directory",
        field("door", "tmpfs", 'tmpfs = ["/data", "/etc/caddy"]'),
        "service door.tmpfs /etc/caddy: /etc/caddy is the target of another mount",
    ),
    (
        "ARCH-R229 a variable named in lower case",
        field("navidrome", "ND_DATAFOLDER", 'ND_DATAFOLDER = "/config"\nnd_extra = "x"'),
        'service navidrome.env.nd_extra: is not upper-case letters, digits and "_"',
    ),
    (
        "ARCH-R229 a variable lemonfiber writes itself",
        field("navidrome", "ND_DATAFOLDER", 'ND_DATAFOLDER = "/config"\nTZ = "UTC"'),
        "service navidrome.env.TZ: TZ is lemonfiber's to write",
    ),
    (
        "ARCH-R230 a constant holding a control character",
        patch("services/navidrome.toml", 'ND_DATAFOLDER = "/config"', 'ND_DATAFOLDER = "/config\\u0007"'),
        "service navidrome.env.ND_DATAFOLDER: '/config\\x07' holds a control character",
    ),
    (
        "ARCH-R231 a setting in another service's namespace",
        field("seerr", "LOG_LEVEL", 'LOG_LEVEL = { setting = "flaresolverr:log-level", default = "info" }'),
        (
            "service seerr.env.LOG_LEVEL: names setting flaresolverr:log-level, which is neither one the core "
            "publishes nor in the seerr namespace"
        ),
    ),
    (
        "ARCH-R232 an address asked of a service by name",
        field("unpackerr", "UN_SONARR_0_URL", 'UN_SONARR_0_URL = { url_of = "sonarr" }'),
        "service unpackerr.env.UN_SONARR_0_URL: asks for the address of sonarr, which is not a core capability",
    ),
    (
        "ARCH-R232 one variable holding the address of every filler",
        field("bazarr", "user_mode", 'user_mode = "puid-env"\nenv = { CURATOR = { url_of = "library.curate" } }'),
        "service bazarr.env.CURATOR: asks for the address of library.curate, which bazarr asks each of",
    ),
    (
        "ARCH-R247 a secret the manifest does not declare",
        field("bindery", "BINDERY_LIBRARY_DIR",
              'BINDERY_LIBRARY_DIR = "/data/media/books"\nBINDERY_API_KEY = { secret = "bindery-key" }'),
        "service bindery.env.BINDERY_API_KEY: names secret bindery-key, which the manifest does not declare",
    ),
    (
        "ARCH-R233 a confined service its image runs as",
        field("request-gate", "posture", 'posture = "confined"\nuser_mode = "image"'),
        "service request-gate.user_mode: must be uid for a service that speaks a contract or is confined",
    ),
    (
        "ARCH-R234 a confined service taking a shape",
        field("request-gate", "posture", 'posture = "confined"\nshape = "render"'),
        "service request-gate.shape: is confined and takes render, which a confined service may not",
    ),
    (
        "ARCH-R235 a memory limit of nothing",
        field("decline", "mem_limit_mib", "mem_limit_mib = 0"),
        "service decline.mem_limit_mib: is zero",
    ),
    (
        "ARCH-R236 inside the network of a capability outside the closed set",
        field("qbittorrent", "network", 'network = { via = "media.serve" }'),
        "service qbittorrent.network.via: names media.serve, which no service may run inside the network of",
    ),
    (
        "ARCH-R236 inside the network of a capability it provides itself",
        field("gluetun", "shape", 'shape = "egress-guard"\nnetwork = { via = "network.egress-guard" }'),
        "service gluetun.network.via: names network.egress-guard, which the service provides itself",
    ),
    (
        "ARCH-R237 a shape taken by a service that does not serve it",
        field("sonarr", "user_mode", 'user_mode = "puid-env"\nshape = "egress-guard"'),
        "service sonarr.shape: takes egress-guard, which only a service providing network.egress-guard may",
    ),
]
