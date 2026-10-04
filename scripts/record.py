#!/usr/bin/env python3
"""Record what each bundled service answers to the probes it claims.

A `[[service.claim]]` in stack.toml binds every probe of a capability to a
request on the service and to a recording of the answer, taken from the image
the manifest pins (`ARCH-R136`). This takes those recordings.

For each service named it starts the pinned `image@digest` fresh, in a scratch
directory with nothing in it but the templates this repository ships, under a
container name and a port nothing else uses. It does whatever first run the
service needs before it will answer an operator, with credentials generated for
that run and thrown away with it, asks each bound probe, and writes the answer
to the fixture the claim names. The container and its scratch directory are
removed whether or not that worked.

A probe the published vocabulary says is asked with the operator's credential is
asked with the one this run made; every other probe presents nothing. The
recording keeps the request the claim declares and never the credential: a
claim cannot present one, and a recording is committed to a public repository.

What comes back is scrubbed before it is written. Every credential the run
made, the container's id, hostname and addresses, the scratch directory's path
and the recording machine's own name are replaced with a placeholder, and so is
each place a recipe names as describing the machine or the instance rather than
the service. A recording carries the shape of the answer and nothing about where
it was taken. Read each one anyway before committing it.

Needs Docker and the network, and the vocabulary lemonfiber publishes, read on
standard input:

    python3 scripts/record.py sonarr radarr < capability-vocabulary.json

`--self-test` proves the scrubbing and the reading of an answer, offline.
Exit 0 = every recording written, 1 = one was not.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import http.client
import http.cookiejar
import json
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from registry import by_digest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACK_TOML = "stack.toml"

# Distinct from every name `docker compose` gives the stack's own containers,
# so nothing here can address — or remove — a container the operator runs.
CONTAINER_PREFIX = "lemonfiber-record-"

# Published on loopback only, on a port the kernel chose as free, so a recording
# can be taken on a machine already running the stack.
HOST = "127.0.0.1"
SCHEME = "http"

# What a recording keeps of a body that is not JSON. Enough to say what it is —
# an HTML login page, an XML document — and too little to carry a page.
BODY_STARTS_WITH = 64

# The response headers an expectation can constrain.
KEPT_HEADERS = ("content-type",)

# How long a service may take to answer at all after it starts, and how often it
# is asked meanwhile.
READY_S = 240
POLL_S = 2.0
ATTEMPT_TIMEOUT_S = 10

# The capabilities the recipes write notes for, as the published vocabulary
# names them.
INDEXER_SEARCH = "indexer.search"
INDEXER_PROXY = "indexer.proxy"
DOWNLOAD_USENET = "download.usenet"
DOWNLOAD_TORRENT = "download.torrent"
EGRESS_GUARD = "network.egress-guard"
LIBRARY_CURATE = "library.curate"
SUBTITLES_FETCH = "subtitles.fetch"
MEDIA_SERVE = "media.serve"
IDENTITY_SOURCE = "identity.source"
REQUEST_INTAKE = "request.intake"

# The media type every JSON exchange here is sent and asked for as.
JSON_TYPE = "application/json"
# Calibre-Web-Automated's sign-in page, which is also the first thing it serves.
CALIBRE_WEB_LOGIN = "/login"

# What every scrubbed value is replaced with.
REDACTED = "<redacted>"

# The uid and gid the images that drop privileges run as here. The stack takes
# both from the operator's environment; a recording has no operator.
PUID = "1000"
PGID = "1000"
TZ = "Etc/UTC"
# What the LinuxServer.io images read to drop privileges, and the zone every
# image is told it is in.
LSIO_ENV = {"PUID": PUID, "PGID": PGID, "TZ": TZ}

# The name this run's operator is given wherever a first run asks for one.
OPERATOR = "operator"
# The administrator a fresh qBittorrent and a fresh Calibre-Web-Automated each
# ship with, as their images document them. Signed in as only inside a
# container this run made and removes.
QBITTORRENT_ADMIN = "admin"
CALIBRE_WEB_ADMIN = ("admin", "admin123")


@dataclasses.dataclass
class Credential:
    """How an operator's request is told apart from an anonymous one."""

    headers: dict[str, str] = dataclasses.field(default_factory=dict)
    query: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Answer:
    status: int
    headers: dict[str, str]
    body: bytes


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, and recorded as one rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


class Run:
    """One fresh container of one pinned image, and what its first run made."""

    def __init__(self, sid: str, reference: str, port: int, scratch: pathlib.Path) -> None:
        self.sid = sid
        self.reference = reference
        self.port = port
        self.scratch = scratch
        self.name = f"{CONTAINER_PREFIX}{sid}"
        self.host_port = 0
        self.secrets: set[str] = set()
        # A key minted before the first start, for a service that takes its API
        # key from its environment then and generates its own otherwise.
        self.key = secrets.token_hex(16)
        # A WireGuard key pair's worth of random keys, for a tunnel whose far end
        # does not exist.
        self.wireguard = base64.b64encode(secrets.token_bytes(32)).decode()
        self.peer = base64.b64encode(secrets.token_bytes(32)).decode()
        self.made(self.key, self.wireguard, self.peer)

    @property
    def base(self) -> str:
        return f"{SCHEME}://{HOST}:{self.host_port}"

    def made(self, *values: str) -> None:
        """Values this run made that no recording may carry."""
        self.secrets.update(value for value in values if value)

    def ask(self, method: str, path: str, *, headers: dict[str, str] | None = None,
            body: bytes | None = None, query: dict[str, str] | None = None) -> Answer:
        url = self.base + path
        if query:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode(query)
        # Addressed as the stack addresses it, on the port it listens on, rather
        # than on the one this run published: qBittorrent refuses a Host naming
        # any other port, with a 401 that would read as the probe's refusal.
        sent = {"Host": f"{HOST}:{self.port}", **(headers or {})}
        request = urllib.request.Request(url, data=body, method=method, headers=sent)
        try:
            with OPENER.open(request, timeout=ATTEMPT_TIMEOUT_S) as reply:
                return Answer(reply.status, {k.lower(): v for k, v in reply.headers.items()}, reply.read())
        except urllib.error.HTTPError as refused:
            return Answer(refused.code, {k.lower(): v for k, v in refused.headers.items()}, refused.read())

    def json(self, method: str, path: str, document: object = None, *,
             headers: dict[str, str] | None = None) -> Answer:
        sent = {"Content-Type": JSON_TYPE, "Accept": JSON_TYPE, **(headers or {})}
        body = None if document is None else json.dumps(document).encode()
        return self.ask(method, path, headers=sent, body=body)

    def form(self, path: str, fields: dict[str, str], *, headers: dict[str, str] | None = None) -> Answer:
        sent = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        return self.ask("POST", path, headers=sent, body=urllib.parse.urlencode(fields).encode())

    def wait(self, path: str) -> None:
        """Until the service answers `path` with anything but an error."""
        deadline = time.monotonic() + READY_S
        last = "nothing"
        while time.monotonic() < deadline:
            try:
                answer = self.ask("GET", path)
                if answer.status < 400:
                    return
                last = f"status {answer.status}"
            except (OSError, http.client.HTTPException) as unreachable:
                last = str(unreachable)
            time.sleep(POLL_S)
        raise RuntimeError(f"{self.sid} did not answer {path} within {READY_S}s; last: {last}")

    def file(self, inside: str) -> str:
        """A file in the container's configuration, read from the scratch directory."""
        return (self.scratch / inside).read_text(encoding="utf-8")

    def logs(self) -> str:
        done = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True, check=False)
        return done.stdout + done.stderr

    def identities(self) -> set[str]:
        """What names this container and the machine it ran on."""
        inspected = json.loads(subprocess.run(["docker", "inspect", self.name], capture_output=True,
                                              text=True, check=True).stdout)[0]
        found = {inspected["Id"], inspected["Id"][:12], inspected["Config"]["Hostname"], str(self.scratch)}
        for network in (inspected.get("NetworkSettings", {}).get("Networks") or {}).values():
            found.update({network.get("IPAddress", ""), network.get("Gateway", ""),
                          network.get("MacAddress", "")})
        found.add(socket.gethostname())
        return {value for value in found if value}


@dataclasses.dataclass
class Recipe:
    """What one service needs to run fresh and answer its operator.

    `setup` does the first run and returns the operator's credential, telling the
    run every secret it made. `notes` is each recording's note, by capability and
    probe: why this answer is the evidence, which no body says by itself.
    """

    setup: Callable[[Run], Credential]
    notes: dict[tuple[str, str], str]
    config: str = "/config"
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    templates: tuple[str, ...] = ()
    run_args: tuple[str, ...] = ()
    ready: str | None = None
    # The port asked, where it is not the one the manifest says it listens on.
    port: int | None = None
    # Places in an answer that describe the machine or the instance rather than
    # the service — a disk's size, a server's id — written as JSON Pointers, and
    # redacted.
    redact: tuple[str, ...] = ()


# ── first runs ──────────────────────────────────────────────────────────────


def first(run: Run, read: Callable[[], str], pattern: str, where: str) -> str:
    """What a service writes on first start, once it has written it."""
    deadline = time.monotonic() + READY_S
    while time.monotonic() < deadline:
        try:
            found = re.search(pattern, read())
        except OSError:
            found = None
        if found:
            run.made(found.group(1))
            return found.group(1)
        time.sleep(POLL_S)
    raise RuntimeError(f"{run.sid} wrote nothing matching {pattern!r} to {where}")


def written(run: Run, inside: str, pattern: str) -> str:
    return first(run, lambda: run.file(inside), pattern, inside)


def servarr(run: Run) -> Credential:
    """Sonarr, Radarr, Lidarr and Prowlarr write their own API key on first start."""
    return Credential(headers={"X-Api-Key": written(run, "config.xml", r"<ApiKey>([0-9a-f]{32})</ApiKey>")})


def sabnzbd(run: Run) -> Credential:
    """SABnzbd writes its API key into sabnzbd.ini on first start, beside the template."""
    key = written(run, "sabnzbd.ini", r"(?m)^api_key = ([0-9a-f]{32})$")
    written(run, "sabnzbd.ini", r"(?m)^nzb_key = ([0-9a-f]{32})$")
    return Credential(query={"apikey": key})


def qbittorrent(run: Run) -> Credential:
    """The LinuxServer image prints a password for this session only; signing in
    with it opens a session the cookie carries."""
    password = first(run, run.logs, r"temporary password is provided for this session: (\S+)", "its log")
    answer = run.form("/api/v2/auth/login", {"username": QBITTORRENT_ADMIN, "password": password},
                      headers={"Referer": f"{SCHEME}://{HOST}:{run.port}"})
    cookie = answer.headers.get("set-cookie", "").split(";", 1)[0]
    if answer.status >= 300 or not cookie:
        raise RuntimeError(f"qbittorrent refused the session password: {answer.status}")
    run.made(cookie, cookie.split("=", 1)[-1])
    return Credential(headers={"Cookie": cookie})


def bazarr(run: Run) -> Credential:
    """Bazarr writes its API key into config.yaml on first start."""
    return Credential(headers={"X-API-KEY": written(run, "config/config.yaml", r"(?m)^  apikey: ([0-9a-f]{32})$")})


def password(run: Run) -> str:
    """A password for this run's operator, made here and thrown away with it."""
    made = secrets.token_urlsafe(18)
    run.made(made)
    return made


def jellyfin(run: Run) -> Credential:
    """Through the first-run wizard's own endpoints, then signed in as the user it made."""
    client = 'MediaBrowser Client="lemonfiber-record", Device="record", DeviceId="record", Version="1"'
    secret = password(run)
    run.json("POST", "/Startup/Configuration",
             {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
    run.json("GET", "/Startup/User")
    run.json("POST", "/Startup/User", {"Name": OPERATOR, "Password": secret})
    run.json("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
    done = run.json("POST", "/Startup/Complete")
    if done.status >= 300:
        raise RuntimeError(f"jellyfin did not complete its first run: {done.status}")
    signed = run.json("POST", "/Users/AuthenticateByName", {"Username": OPERATOR, "Pw": secret},
                      headers={"Authorization": client})
    if signed.status != 200:
        raise RuntimeError(f"jellyfin refused the user it made: {signed.status}")
    answer = json.loads(signed.body)
    token = answer["AccessToken"]
    run.made(token, answer.get("User", {}).get("Id", ""), answer.get("ServerId", ""))
    return Credential(headers={"Authorization": f'{client}, Token="{token}"'})


def audiobookshelf(run: Run) -> Credential:
    """Its root user is made by the first call to /init, then signed in as."""
    secret = password(run)
    made = run.json("POST", "/init", {"newRoot": {"username": OPERATOR, "password": secret}})
    if made.status >= 300:
        raise RuntimeError(f"audiobookshelf did not make its root user: {made.status}")
    signed = run.json("POST", "/login", {"username": OPERATOR, "password": secret},
                      headers={"x-return-tokens": "true"})
    if signed.status != 200:
        raise RuntimeError(f"audiobookshelf refused the user it made: {signed.status}")
    user = json.loads(signed.body)["user"]
    token = user.get("accessToken") or user["token"]
    run.made(token, user.get("refreshToken", ""), user.get("token", ""), user.get("id", ""))
    return Credential(headers={"Authorization": f"Bearer {token}"})


def navidrome(run: Run) -> Credential:
    """Its first admin is made by the first call to createAdmin, which signs it in."""
    made = run.json("POST", "/auth/createAdmin", {"username": OPERATOR, "password": password(run)})
    if made.status != 200:
        raise RuntimeError(f"navidrome did not make its admin: {made.status}")
    answer = json.loads(made.body)
    run.made(answer["token"], answer.get("id", ""), answer.get("subsonicSalt", ""),
             answer.get("subsonicToken", ""))
    return Credential(headers={"x-nd-authorization": f"Bearer {answer['token']}"})


def calibre_web(run: Run) -> Credential:
    """Signed in through its own login form, as the administrator every fresh
    instance ships with; the session that opens is what its JSON views read."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
    host = {"Host": f"{HOST}:{run.port}"}
    with opener.open(urllib.request.Request(run.base + CALIBRE_WEB_LOGIN, headers=host),
                     timeout=ATTEMPT_TIMEOUT_S) as page:
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.read().decode("utf-8", "replace"))
    if token is None:
        raise RuntimeError("calibre-web-automated served a login page with no form token")
    username, secret = CALIBRE_WEB_ADMIN
    fields = {"username": username, "password": secret, "csrf_token": token.group(1),
              "remember_me": "on", "submit": ""}
    request = urllib.request.Request(run.base + CALIBRE_WEB_LOGIN, data=urllib.parse.urlencode(fields).encode(),
                                     headers=host)
    try:
        opener.open(request, timeout=ATTEMPT_TIMEOUT_S)
    except urllib.error.HTTPError as answered:
        # A signed-in form is answered with a redirect to the library.
        if answered.code != 302 or answered.headers.get("Location") != "/":
            raise RuntimeError(f"calibre-web-automated refused its own administrator: {answered.code}") from None
    cookies = {cookie.name: cookie.value for cookie in jar}
    if "remember_token" not in cookies:
        raise RuntimeError("calibre-web-automated opened no session")
    run.made(token.group(1), *cookies.values())
    return Credential(headers={"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())})


def minted(run: Run) -> Credential:
    """The key this run gave the service before its first start."""
    return Credential(headers={"X-Api-Key": run.key})


def anonymous(_run: Run) -> Credential:
    return Credential()


# ── what each service is ────────────────────────────────────────────────────

GUARDED_CURATE = (
    "The wanted list, asked for presenting nothing, on a fresh instance. A refusal is "
    "the pass: a curating service holds every key the stack gave it and can be made to "
    "download anything, so its API answering an anonymous caller at all is the failure."
)
WANTED_CURATE = (
    "The same read with the API key this instance wrote on first start. Recorded empty, "
    "because a fresh instance wants nothing yet; what it shows is that the list is there "
    "and answers in the shape a filer reads."
)

RECIPES: dict[str, Recipe] = {
    "prowlarr": Recipe(
        setup=servarr,
        env=LSIO_ENV,
        notes={
            (INDEXER_SEARCH, "guarded"): (
                "The indexers this instance holds, asked for presenting nothing. A refusal is "
                "the pass: an indexer account is an account somebody paid for, and listing them "
                "to the network is the failure whether or not it still searches."
            ),
            (INDEXER_SEARCH, "indexers"): (
                "The same read with the API key this instance wrote on first start. Recorded "
                "from a fresh instance, so the list is empty; what it shows is that the indexers "
                "are a list the operator's credential can read."
            ),
        },
    ),
    **{
        sid: Recipe(
            setup=servarr,
            env=LSIO_ENV,
            notes={
                (LIBRARY_CURATE, "guarded"): GUARDED_CURATE,
                (LIBRARY_CURATE, "wanted"): WANTED_CURATE,
            },
        )
        for sid in ("sonarr", "radarr", "lidarr")
    },
    "sabnzbd": Recipe(
        setup=sabnzbd,
        env=LSIO_ENV,
        templates=("config/sabnzbd/sabnzbd.ini",),
        redact=tuple(
            f"/queue/{name}"
            for name in ("diskspace1", "diskspace2", "diskspace1_norm", "diskspace2_norm",
                         "diskspacetotal1", "diskspacetotal2")
        ),
        notes={
            (DOWNLOAD_USENET, "guarded"): (
                "The queue, asked for presenting nothing, on a fresh instance started from the "
                "template this repository ships. A refusal is the pass: the queue names what the "
                "household is downloading and the account paying for it."
            ),
            (DOWNLOAD_USENET, "queue"): (
                "The same read with the API key this instance wrote on first start, passed as "
                "SABnzbd takes it. Recorded empty, because nothing has been handed to it yet; "
                "what it shows is the queue a filer reads finished paths from."
            ),
        },
    ),
    "bazarr": Recipe(
        setup=bazarr,
        env=LSIO_ENV,
        redact=("/data/operating_system", "/data/cpu_cores"),
        notes={
            (SUBTITLES_FETCH, "guarded"): (
                "Its own state, asked for presenting nothing, on a fresh instance. A refusal is "
                "the pass: it holds subtitle-provider accounts and a path into the library, and "
                "neither is something the network may read."
            ),
            (SUBTITLES_FETCH, "status"): (
                "The same read with the API key this instance wrote on first start. What it is "
                "and which version, answered without a provider being asked anything; the host's "
                "operating system and core count are redacted, being the recording machine's "
                "rather than Bazarr's."
            ),
        },
    ),
    "jellyfin": Recipe(
        setup=jellyfin,
        ready="/Startup/Configuration",
        env={"TZ": TZ, "JELLYFIN_CACHE_DIR": "/config/cache"},
        run_args=("--user", f"{PUID}:{PGID}"),
        notes={
            (MEDIA_SERVE, "guarded"): (
                "The catalogue, asked for presenting nothing, on an instance whose first run is "
                "done. A refusal is the pass, and the probe the vocabulary exists for: a media "
                "server on the household network that answered this would be publishing the "
                "household's library to every device on it."
            ),
            (MEDIA_SERVE, "catalogue"): (
                "The same read signed in as the user its first run made. Recorded with no library "
                "added, so the catalogue holds only the playlists folder Jellyfin makes itself; "
                "what it shows is the catalogue a player reads, in the shape it reads it."
            ),
            (IDENTITY_SOURCE, "identifies"): (
                "The public server information, asked for presenting nothing. An answer is the "
                "pass: something has to be answerable before anybody signs in, and what it is is "
                "which server this is. The server's id is redacted, and so are the container's "
                "name and address it reports."
            ),
            (IDENTITY_SOURCE, "guarded"): (
                "The accounts it holds, asked for presenting nothing. A refusal is the pass: "
                "which server, to anybody; who is on it, to nobody."
            ),
        },
        redact=("/Id",),
    ),
    "audiobookshelf": Recipe(
        setup=audiobookshelf,
        env={"TZ": TZ},
        ready="/healthcheck",
        notes={
            (MEDIA_SERVE, "guarded"): (
                "The libraries it serves, asked for presenting nothing, on an instance whose root "
                "user exists. A refusal is the pass: a library server on the household network "
                "that answered this would be publishing the household's collection to it."
            ),
            (MEDIA_SERVE, "catalogue"): (
                "The same read signed in as the root user its first run made. Recorded with no "
                "library added, so the list is empty; what it shows is the catalogue a player "
                "reads, in the shape it reads it."
            ),
        },
    ),
    "navidrome": Recipe(
        setup=navidrome,
        config="/data",
        env={"TZ": TZ},
        run_args=("--user", f"{PUID}:{PGID}"),
        ready="/ping",
        notes={
            (MEDIA_SERVE, "guarded"): (
                "The albums it serves, asked for presenting nothing, on an instance whose admin "
                "exists. A refusal is the pass: a library server on the household network that "
                "answered this would be publishing the household's collection to it."
            ),
            (MEDIA_SERVE, "catalogue"): (
                "The same read signed in as the admin its first run made. Recorded with no music "
                "in its folder, so the list is empty; what it shows is the catalogue a player "
                "reads, in the shape it reads it."
            ),
        },
    ),
    "bindery": Recipe(
        setup=minted,
        env={"TZ": TZ, "BINDERY_API_KEY": "{key}"},
        run_args=("--user", f"{PUID}:{PGID}"),
        notes={
            (LIBRARY_CURATE, "guarded"): GUARDED_CURATE,
            (LIBRARY_CURATE, "wanted"): (
                "The same read with the API key this instance was given before its first start, "
                "as the stack gives it one. Recorded empty, because a fresh instance wants nothing "
                "yet; what it shows is that the list is there and answers as a list."
            ),
        },
    ),
    "seerr": Recipe(
        setup=anonymous,
        config="/app/config",
        env={"TZ": TZ},
        run_args=("--user", f"{PUID}:{PGID}"),
        notes={
            (REQUEST_INTAKE, "identifies"): (
                "Its own version and state, asked for presenting nothing. An answer is the pass: "
                "this is the one service a household member reaches without being an operator, "
                "so it has a front door that answers before anybody signs in."
            ),
            (REQUEST_INTAKE, "guarded"): (
                "What has been requested, asked for presenting nothing. A refusal is the pass: a "
                "request list readable by the network is a list of what everybody in the house "
                "is waiting to watch."
            ),
        },
    ),
    "gluetun": Recipe(
        setup=anonymous,
        config="/gluetun",
        port=8000,
        env={
            "TZ": TZ,
            "VPN_SERVICE_PROVIDER": "custom",
            "VPN_TYPE": "wireguard",
            "WIREGUARD_PRIVATE_KEY": "{wireguard}",
            "WIREGUARD_PUBLIC_KEY": "{peer}",
            "WIREGUARD_ADDRESSES": "192.0.2.2/32",
            "VPN_ENDPOINT_IP": "192.0.2.1",
            "VPN_ENDPOINT_PORT": "51820",
        },
        run_args=("--cap-add", "NET_ADMIN", "--device", "/dev/net/tun:/dev/net/tun"),
        ready="/v1/vpn/status",
        notes={
            (EGRESS_GUARD, "identifies"): (
                "The tunnel's state, asked of the control server from beside it, presenting "
                "nothing. Recorded with a tunnel dialled at a documentation address (192.0.2.1) "
                "with keys made for this run, because no provider account is used to record. "
                "The tunnel never comes up, so this shows that the guard can be asked and answers "
                "in the shape the probe reads, not that a tunnel was up."
            ),
        },
    ),
    "calibre-web-automated": Recipe(
        setup=calibre_web,
        env=LSIO_ENV,
        ready=CALIBRE_WEB_LOGIN,
        notes={
            (MEDIA_SERVE, "guarded"): (
                "The OPDS catalogue a reader's device pulls from, asked for presenting nothing. "
                "A refusal is the pass: a library server on the household network that answered "
                "this would be publishing the household's books to it."
            ),
            (MEDIA_SERVE, "catalogue"): (
                "The catalogue as its own views read it, in a session signed in as the "
                "administrator every fresh instance ships with. Asked here rather than at /opds, "
                "which answers XML and so could not show the body this probe requires; recorded "
                "with no books added, so the list is empty."
            ),
        },
    ),
    "flaresolverr": Recipe(
        setup=anonymous,
        env={"TZ": TZ},
        ready="/",
        notes={
            (INDEXER_PROXY, "identifies"): (
                "Its own state, asked for presenting nothing. It holds nothing of the operator's "
                "and is reached only from inside the stack, so an answer is the pass, and what "
                "the answer has to show is that the thing behind the port is FlareSolverr and "
                "which version of it."
            ),
        },
    ),
    "qbittorrent": Recipe(
        setup=qbittorrent,
        env={**LSIO_ENV, "WEBUI_PORT": "8081"},
        ready="/api/v2/app/version",
        notes={
            (DOWNLOAD_TORRENT, "guarded"): (
                "The torrent list, asked for with no session, on a fresh instance. A refusal is "
                "the pass: a torrent client's queue is the one list in this stack whose "
                "disclosure has consequences outside the household."
            ),
            (DOWNLOAD_TORRENT, "queue"): (
                "The same read in a session opened with the password this instance printed for "
                "its first run. Recorded from the image alone, outside the tunnel it runs behind "
                "in the stack, because the queue answers the same either way; empty, because "
                "nothing has been handed to it yet."
            ),
        },
    ),
}


# ── recording ───────────────────────────────────────────────────────────────


def scrubbed(value: object, hidden: set[str]) -> object:
    """`value` with every hidden string, wherever it appears, replaced."""
    if isinstance(value, str):
        for secret in sorted(hidden, key=len, reverse=True):
            if secret in value:
                value = REDACTED if value == secret else value.replace(secret, REDACTED)
        return value
    if isinstance(value, list):
        return [scrubbed(item, hidden) for item in value]
    if isinstance(value, dict):
        return {scrubbed(key, hidden): scrubbed(item, hidden) for key, item in value.items()}
    return value


def redacted(document: object, places: tuple[str, ...]) -> object:
    """`document` with the value at each place it holds replaced.

    A place it does not hold is passed over: one recipe serves every probe of a
    service, and each answer holds only some of its places.
    """
    for place in places:
        steps = [step.replace("~1", "/").replace("~0", "~") for step in place.split("/")[1:]]
        holder = document
        for step in steps[:-1]:
            holder = holder.get(step) if isinstance(holder, dict) else None
        if isinstance(holder, dict) and steps and steps[-1] in holder:
            holder[steps[-1]] = REDACTED
    return document


def response_of(answer: Answer, hidden: set[str], places: tuple[str, ...] = ()) -> dict:
    """An answer in the terms a recording keeps it: status, the headers an
    expectation reads, and the body as JSON or as the start of it."""
    kept: dict = {"status": answer.status}
    headers = {name: answer.headers[name] for name in KEPT_HEADERS if name in answer.headers}
    if headers:
        kept["headers"] = headers
    text = answer.body.decode("utf-8", errors="replace")
    try:
        kept["json"] = redacted(json.loads(text), places) if text.strip() else None
    except ValueError:
        kept["json"] = None
    if kept["json"] is None and text.strip():
        kept["body_starts_with"] = text[:BODY_STARTS_WITH]
    return scrubbed(kept, hidden)


def operator_probes(vocabulary: dict) -> set[tuple[str, str]]:
    return {
        (capability["name"], probe["id"])
        for capability in vocabulary.get("capabilities", [])
        for probe in capability.get("probes", [])
        if probe.get("credential") == "operator"
    }


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def start(run: Run, recipe: Recipe) -> None:
    for template in recipe.templates:
        source = ROOT / template
        target = run.scratch / source.name
        shutil.copyfile(source, target)
    run.host_port = free_port()
    command = [
        "docker", "run", "-d", "--name", run.name,
        "-p", f"{HOST}:{run.host_port}:{run.port}",
        "-v", f"{run.scratch}:{recipe.config}",
        *(item for name, value in environment(run, recipe).items() for item in ("-e", f"{name}={value}")),
        *recipe.run_args,
        run.reference,
    ]
    subprocess.run(command, capture_output=True, text=True, check=True)


def environment(run: Run, recipe: Recipe) -> dict[str, str]:
    """A recipe's environment, with the keys this run minted written in."""
    minted = {"key": run.key, "wireguard": run.wireguard, "peer": run.peer}
    return {name: value.format(**minted) for name, value in recipe.env.items()}


def stop(run: Run) -> None:
    subprocess.run(["docker", "rm", "-f", "-v", run.name], capture_output=True, check=False)


def record(service: dict, recipe: Recipe, operator: set[tuple[str, str]], hold: bool) -> list[str]:
    sid = service["id"]
    reference = by_digest(service)
    port = recipe.port or service.get("listens") or service.get("port")
    scratch = pathlib.Path(tempfile.mkdtemp(prefix=f"record-{sid}-"))
    run = Run(sid, reference, port, scratch)
    written: list[str] = []
    # Pulled for this run, so removed with it. An image that was already here is
    # somebody else's, whether or not anything is running it.
    pulled = subprocess.run(["docker", "image", "inspect", reference], capture_output=True,
                            check=False).returncode != 0
    try:
        start(run, recipe)
        health = service.get("health", {})
        run.wait(recipe.ready or health.get("path", "/"))
        credential = recipe.setup(run)
        hidden = run.secrets | run.identities()
        for claim in service.get("claim", []):
            capability = claim["capability"]
            for probe in claim.get("probe", []):
                asked = probe["request"]
                headers = {"Accept": asked["accept"]} if "accept" in asked else {}
                query: dict[str, str] = {}
                if (capability, probe["id"]) in operator:
                    headers.update(credential.headers)
                    query.update(credential.query)
                answer = run.ask(asked["method"], asked["path"], headers=headers, query=query)
                recording = {
                    "recorded_from": reference,
                    "note": recipe.notes[(capability, probe["id"])],
                    "request": {key: asked[key] for key in ("method", "path", "accept") if key in asked},
                    "response": response_of(answer, hidden, recipe.redact),
                }
                target = ROOT / probe["fixture"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(recording, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                written.append(f"{probe['fixture']}: {answer.status}")
        if hold:
            print(f"held: {run.name} at {run.base}, until `docker rm -f -v {run.name}`")
    finally:
        if not hold:
            stop(run)
            shutil.rmtree(scratch, ignore_errors=True)
            if pulled:
                # Refused by Docker while any container uses it, which is the guard.
                subprocess.run(["docker", "rmi", reference], capture_output=True, check=False)
    return written


def self_test() -> int:
    hidden = {"abc123", "host-1"}
    assert scrubbed({"key": "abc123", "at": "host-1:80/x", "n": 3}, hidden) == {
        "key": REDACTED, "at": f"{REDACTED}:80/x", "n": 3,
    }
    assert scrubbed(["abc123abc123"], hidden) == [f"{REDACTED}{REDACTED}"]
    empty = response_of(Answer(401, {"content-length": "0"}, b""), set())
    assert empty == {"status": 401, "json": None}, empty
    html = response_of(Answer(200, {"content-type": "text/html"}, b"<!doctype html>" + b"x" * 200), set())
    assert html["json"] is None and html["body_starts_with"].startswith("<!doctype html>")
    assert len(html["body_starts_with"]) == BODY_STARTS_WITH
    parsed = response_of(Answer(200, {"content-type": JSON_TYPE}, b'{"token":"abc123"}'), hidden)
    assert parsed["json"] == {"token": REDACTED} and parsed["headers"] == {"content-type": JSON_TYPE}
    placed = response_of(Answer(200, {}, b'{"q":{"disk":"31.3","n":1},"a~b":2}'), set(), ("/q/disk", "/a~0b", "/x/y"))
    assert placed["json"] == {"q": {"disk": REDACTED, "n": 1}, "a~b": REDACTED}, placed
    vocabulary = {"capabilities": [{"name": "a.b", "probes": [
        {"id": "guarded", "credential": "none"}, {"id": "read", "credential": "operator"}]}]}
    assert operator_probes(vocabulary) == {("a.b", "read")}
    run = Run("thing", "example/thing@sha256:0", 80, pathlib.Path("/nowhere"))
    minted = environment(run, Recipe(setup=anonymous, notes={}, env={"KEY": "{key}", "TZ": TZ}))
    assert minted == {"KEY": run.key, "TZ": TZ} and run.key in run.secrets, minted
    print("self-test: what a run made and where it ran scrubbed, answers kept as a recording keeps them")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("services", nargs="*", help="service ids in stack.toml")
    parser.add_argument("--hold", action="store_true", help="leave the container running, to look at by hand")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not args.services:
        parser.error("name at least one service")
    if sys.stdin.isatty():
        parser.error("give lemonfiber's published capability-vocabulary.json on standard input")

    manifest = tomllib.loads((ROOT / STACK_TOML).read_text(encoding="utf-8"))
    services = {service["id"]: service for service in manifest.get("service", [])}
    try:
        operator = operator_probes(json.load(sys.stdin))
    except (ValueError, AttributeError, KeyError, TypeError) as unreadable:
        parser.error(f"standard input is not a capability vocabulary: {unreadable}")
    failed = 0
    for sid in args.services:
        if sid not in services or sid not in RECIPES:
            print(f"{sid}: no such service, or no recipe for recording it", file=sys.stderr)
            failed += 1
            continue
        try:
            for line in record(services[sid], RECIPES[sid], operator, args.hold):
                print(f"{sid}: {line}")
        except (RuntimeError, OSError, subprocess.CalledProcessError, KeyError) as broke:
            print(f"{sid}: not recorded: {broke}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
