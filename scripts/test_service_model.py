"""Negative tests for the service model: each container field held to the model's
rules (container_rules.py), and each Compose entry held to what its service file
declares (model_parity.py). Run by test_validate_manifest.py, beside its own.
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
    (
        "ARCH-R226 the entry publishing another container port",
        patch("compose/usenet.yml", '"127.0.0.1:8085:8080"', '"127.0.0.1:8085:8081"'),
        (
            "service sabnzbd: ports: the entry publishes host:container [(8085, 8081)], and the declaration "
            "says [(8085, 8080)]"
        ),
    ),
    (
        "ARCH-R226 a second port the declaration leaves out",
        patch("services/caddy.toml", "\n[[service.publish]]\nport = 443\n", ""),
        (
            "service caddy: ports: the entry publishes host:container [(80, 80), (443, 443)], and the "
            "declaration says [(80, 80)]"
        ),
    ),
    (
        "ARCH-R236 the guard publishing what runs inside it",
        patch("compose/torrent.yml", '"127.0.0.1:8081:8081"', '"127.0.0.1:8082:8081"'),
        (
            "service gluetun: ports: the entry publishes host:container [(8082, 8081)], and the declaration "
            "says [(8081, 8081)]"
        ),
    ),
    (
        "ARCH-R227 a configuration directory mounted writable where it is declared read-only",
        patch("compose/media.yml", "- ./config/door:/etc/caddy:ro", "- ./config/door:/etc/caddy"),
        "service door: mount /etc/caddy",
    ),
    (
        "ARCH-R227 a declared mount the entry leaves out",
        patch("compose/media.yml", "      - ./config/jellyfin/cache:/cache\n", ""),
        "service jellyfin: mount /cache: the entry mounts (type, source, ro) None",
    ),
    (
        "ARCH-R240 the data mount on a service that does not take data",
        field("bazarr", "takes_data", ""),
        "service bazarr: mount /data",
    ),
    (
        "ARCH-R227 scratch space the declaration leaves out",
        patch("compose/media.yml", 'tmpfs: ["/data:mode=1777", "/config:mode=1777"]', 'tmpfs: ["/data:mode=1777"]'),
        "service door: tmpfs",
    ),
    (
        "ARCH-R229 a constant the entry changed",
        patch("compose/media.yml", 'ND_ENABLEINSIGHTSCOLLECTOR: "false"', 'ND_ENABLEINSIGHTSCOLLECTOR: "true"'),
        "service navidrome: env ND_ENABLEINSIGHTSCOLLECTOR: the entry sets it to the constant 'true'",
    ),
    (
        "ARCH-R231 a setting whose default the entry changed",
        patch("compose/media.yml", "${SEERR_LOG_LEVEL:-info}", "${SEERR_LOG_LEVEL:-debug}"),
        "service seerr: env LOG_LEVEL: the entry defaults it to 'debug', and the declaration says 'info'",
    ),
    (
        "ARCH-R231 a setting the entry writes as a constant",
        patch("compose/search.yml", "LOG_LEVEL: ${FLARESOLVERR_LOG_LEVEL:-info}", "LOG_LEVEL: info"),
        "service flaresolverr: env LOG_LEVEL: the entry sets 'info', which reads no variable",
    ),
    (
        "ARCH-R231 one variable read as two settings",
        field("gluetun", "VPN_PORT_FORWARDING_PROVIDER",
              'VPN_PORT_FORWARDING_PROVIDER = { setting = "gluetun:forwarding-provider", default = "" }'),
        "reads VPN_PROVIDER, which another declaration reads as setting",
    ),
    (
        "ARCH-R232 an address the entry spells otherwise",
        patch("compose/tuning.yml", "UN_SONARR_0_URL: http://sonarr:8989", "UN_SONARR_0_URL: http://sonarr:8990"),
        (
            "service unpackerr: env UN_SONARR_0_URL: the entry sets the address 'http://sonarr:8990', and the "
            "declaration says 'http://sonarr:8989'"
        ),
    ),
    (
        "ARCH-R229 a variable the declaration leaves out",
        patch("compose/media.yml", "      ND_MUSICFOLDER: /data/media/music\n",
              "      ND_MUSICFOLDER: /data/media/music\n      ND_EXTRA: x\n"),
        "service navidrome: env ND_EXTRA: the entry sets ND_EXTRA, and the declaration does not",
    ),
    (
        "ARCH-R233 a user the declaration does not name",
        field("sonarr", "user_mode", 'user_mode = "uid"'),
        "service sonarr: user: the entry runs as None, and the declaration says '${PUID:-1000}:${PGID:-1000}'",
    ),
    (
        "ARCH-R235 a memory limit the entry does not hold",
        field("decline", "mem_limit_mib", "mem_limit_mib = 128"),
        "service decline: mem_limit: the entry limits memory to 67108864, and the declaration says 134217728",
    ),
    (
        "ARCH-R236 a network the declaration does not ask for",
        field("qbittorrent", "network", ""),
        "service qbittorrent: network: the entry runs in (network_mode, depends_on)",
    ),
    (
        "ARCH-R236 a start that does not wait for the guard to be healthy",
        patch("compose/torrent.yml", "condition: service_healthy", "condition: service_started"),
        (
            "service qbittorrent: network: the entry runs in (network_mode, depends_on) ('service:gluetun', "
            "{'gluetun': 'service_started'})"
        ),
    ),
    (
        "ARCH-R237 a device and a capability no declared shape gives",
        field("gluetun", "shape", ""),
        (
            "service gluetun: shape: the entry adds (capabilities, devices, groups) (['NET_ADMIN'], "
            "['/dev/net/tun'], False), and the declaration says ([], [], False)"
        ),
    ),
    (
        "ARCH-R246 a difference held open that no longer differs",
        patch("compose/torrent.yml", "      WIREGUARD_PRIVATE_KEY: ${WIREGUARD_PRIVATE_KEY:-}\n", ""),
        "service gluetun: env WIREGUARD_PRIVATE_KEY is held open in model_parity.OPEN and no longer differs",
    ),
]
