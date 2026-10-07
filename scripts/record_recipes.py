"""What each bundled service needs to run fresh and answer its operator: the
first run that makes the operator's credential, and the note each recording
carries.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from record_run import ATTEMPT_TIMEOUT_S, HOST, POLL_S, READY_S, SCHEME, Credential, NoRedirect, Recipe, Run

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

# Calibre-Web-Automated's sign-in page, which is also the first thing it serves.
CALIBRE_WEB_LOGIN = "/login"

# The uid and gid the LinuxServer.io images drop privileges to here. The stack
# takes both from the operator's environment; a recording has no operator. Every
# other image runs as the user it was built with, which owns the volume its
# configuration is on: the stack's `user:` pair is about owning files on the
# host, and a recording's volume is never on the host.
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
                "what it shows is the catalogue a player reads, in the shape it reads it. The ids "
                "the instance gives the folder are redacted, as the server's id is."
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
        redact_named=("Id", "ItemId", "Key"),
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
        # Its image has no /config, so the volume Docker makes there is root's,
        # and the image's own nonroot user could not write its database to it.
        run_args=("--user", "0:0"),
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
        # The sign-in page, which answers before a session exists; every API
        # path refuses a caller without one.
        ready="/",
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
