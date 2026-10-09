#!/usr/bin/env python3
"""Ask the door what it lets through, and prove it refuses the rest.

The door, config/door/Caddyfile, is the household's only way to Jellyfin, whose
byte endpoints answer anybody holding an item's id. Before an item's bytes go
on, it asks Jellyfin `GET /Items/{id}` with the token the request presented,
and anything but a 2xx is the refusal. It also tells Jellyfin which client a
request came from, and Jellyfin trusts that from the door's fixed address alone.

This starts the door as the manifest pins it, with the Caddyfile this
repository ships and a throwaway certificate pair, on the two networks
compose.yml declares for it at the addresses the Compose model fixes, in front
of a stand-in for Jellyfin, and asks it every case in CASES:

  * from the household, on both of its ports;
  * from inside the `door` network, at the proxy's fixed address and at another
    one, each naming a client in X-Forwarded-For.

The stand-in is the same pinned Caddy, answering the door's question the way
Jellyfin does: 200 for the item the member may see, 404 for one they may not,
401 for a token that is absent or unknown, and 403 for the member arriving from
outside the house, whose account here may not play from there. Every other
request it answers with what it was told: the path, the X-Forwarded-For it
was given and the address the door reached it from. recordings/door/ holds
what Jellyfin itself answered to the same cases, which `record_door.py` takes;
each recording is held to its case's verdict here.

Then the stand-in is stopped, and a request it would have passed is refused.

    python3 scripts/check_door.py              # needs Docker
    python3 scripts/check_door.py --self-test  # four broken doors and three bad recordings, each caught

The two door networks are made with the subnets compose.yml states, so this
cannot run beside a stack that already holds them. Exit 0 = the door holds,
1 = it does not.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator

import door_pair
import stack_manifest
from compose_parity import DOOR, DOOR_UPSTREAM, fixed_address, load_compose_model
from registry import pinned
from report import Report

ROOT = pathlib.Path(__file__).resolve().parent.parent
CADDYFILE = ROOT / "config" / "door" / "Caddyfile"
RECORDINGS = ROOT / "recordings" / "door"

# Distinct from every name `docker compose` gives the stack's own containers and
# networks, so nothing here can address or remove one the operator runs.
PREFIX = "lemonfiber-door-check-"
HOST = "127.0.0.1"
# What the door's plain port speaks, inside the door's own network.
SCHEME = "http"
# Where the door listens, plain and TLS, inside its container.
PLAIN, TLS = 8096, 8920
# The network the door publishes on and the proxy reaches it on.
DOOR_NETWORK = "door"
PROXY = "caddy"

# A client outside the house, from the documentation range, and one inside it,
# on the kind of private network a household's router hands out.
REMOTE = "203.0.113.7"
HOUSEHOLD = str(ipaddress.ip_network("192.168.1.0/24")[20])
# A token nobody holds.
UNKNOWN_TOKEN = "0" * 32

ATTEMPT_TIMEOUT_S = 20
READY_S = 60
POLL_S = 0.5
# What the stand-in prefixes every request it serves with.
SERVED = "served "

# Who sends a request: a household device through a published port, the proxy
# from its fixed address, or another container on the `door` network.
FROM_HOUSEHOLD, FROM_PROXY, FROM_NEIGHBOUR = "household", "proxy", "neighbour"
PASS, REFUSE = "pass", "refuse"

# How a case presents the member's session, and what each is called in a recording.
PRESENTS = {
    "nothing": "nothing",
    "authorization": "the member's session, in the Authorization header",
    "x-emby-token": "the member's session, in X-Emby-Token",
    "x-mediabrowser-token": "the member's session, in X-MediaBrowser-Token",
    "query": "the member's session, in the path's query",
    "unknown": "a token Jellyfin does not know, in X-Emby-Token",
}


@dataclasses.dataclass(frozen=True)
class Case:
    """One request, and whether the door lets it through.

    `path` names the item the member may see as `{open}`, the one in a library
    they were not given as `{closed}`, and the member's token as `{token}`; the
    same item in another spelling is `{open_dashed}` and so on.
    """

    name: str
    path: str
    expect: str
    note: str
    method: str = "GET"
    presents: str = "nothing"
    origin: str = FROM_HOUSEHOLD
    forwarded_for: str = ""


STREAM = "/Videos/{open}/stream?static=true"

CASES = (
    Case("stream-presenting-nothing", STREAM, REFUSE,
         "A film's stream asked for presenting nothing. Jellyfin alone serves it; the door asks first and is told 401."),
    Case("stream-with-the-member-session", STREAM, PASS,
         "The member's own session asks for a film in a library they were given. Jellyfin answers the door's question 200, and the stream goes on.",
         presents="authorization"),
    Case("stream-outside-the-member-libraries", "/Videos/{closed}/stream?static=true", REFUSE,
         "The member's session asks for a film in a library they were not given. Jellyfin alone serves it; the door is told 404.",
         presents="authorization"),
    Case("stream-with-an-unknown-token", STREAM, REFUSE,
         "A token Jellyfin never issued, or one it has ended.", presents="unknown"),
    Case("stream-with-x-emby-token", STREAM, PASS,
         "The member's session in the legacy X-Emby-Token header, which the question carries over.",
         presents="x-emby-token"),
    Case("stream-with-x-mediabrowser-token", STREAM, PASS,
         "The member's session in the legacy X-MediaBrowser-Token header.", presents="x-mediabrowser-token"),
    Case("stream-with-apikey-in-the-query", "/Videos/{open}/stream?static=true&ApiKey={token}", PASS,
         "The member's session in the query, the way an HLS playlist's own URLs carry it. The door puts it in the question's query.",
         presents="query"),
    Case("stream-with-api_key-in-the-query", "/Videos/{open}/stream?static=true&api_key={token}", PASS,
         "The member's session in the legacy api_key query parameter.", presents="query"),
    Case("stream-outside-the-member-libraries-with-the-query", "/Videos/{closed}/stream?static=true&ApiKey={token}",
         REFUSE, "A film the member may not see, with their session in the query.", presents="query"),
    Case("stream-with-a-parameter-smuggled-in-the-token", "/Videos/{open}/stream?static=true&ApiKey={token}%26userId%3D0",
         REFUSE, "A token value carrying an encoded second parameter. The door escapes it into the question, so Jellyfin reads one token that is not the member's.",
         presents="query"),
    Case("hls-playlist-with-the-member-session",
         "/Videos/{open}/master.m3u8?MediaSourceId={open}&VideoCodec=h264&AudioCodec=aac&PlaySessionId=door&DeviceId=door",
         PASS, "The HLS master playlist for a film the member may see.", presents="authorization"),
    Case("image-presenting-nothing", "/Items/{open}/Images/Primary", REFUSE,
         "An item's primary image asked for presenting nothing. Jellyfin alone serves it."),
    Case("image-with-the-member-session", "/Items/{open}/Images/Primary", PASS,
         "The primary image of a film the member may see.", presents="authorization"),
    Case("image-outside-the-member-libraries", "/Items/{closed}/Images/Primary", REFUSE,
         "The primary image of a film the member may not see.", presents="authorization"),
    Case("download-outside-the-member-libraries", "/Items/{closed}/Download", REFUSE,
         "The file of a film the member may not see.", presents="authorization"),
    Case("audio-presenting-nothing", "/Audio/{open}/stream?static=true", REFUSE,
         "An item's audio stream asked for presenting nothing."),
    Case("subtitle-presenting-nothing", "/Videos/{open}/{open}/Subtitles/0/Stream.vtt", REFUSE,
         "A subtitle stream asked for presenting nothing."),
    Case("attachment-presenting-nothing", "/Videos/{open}/{open}/Attachments/0", REFUSE,
         "A video's attachment asked for presenting nothing."),
    Case("live-recording-presenting-nothing", "/LiveTv/LiveRecordings/{open}/stream", REFUSE,
         "A live-TV recording's file asked for presenting nothing."),
    Case("live-stream-file-presenting-nothing", "/LiveTv/LiveStreamFiles/{open}/stream.ts", REFUSE,
         "A live-TV stream's file asked for presenting nothing."),
    Case("legacy-emby-prefix-presenting-nothing", "/emby/Videos/{closed}/stream?static=true", REFUSE,
         "The same stream under /emby, which Jellyfin strips and serves."),
    Case("legacy-mediabrowser-prefix-presenting-nothing", "/mediabrowser/Videos/{closed}/stream?static=true", REFUSE,
         "The same stream under /mediabrowser, which Jellyfin strips and serves."),
    Case("id-in-braces", "/Videos/%7B{closed}%7D/stream?static=true", REFUSE,
         "An id in braces, which Jellyfin also reads as an id. The door cannot ask about it and refuses it."),
    Case("id-padded-with-a-space", "/Items/%20{closed}/Images/Primary", REFUSE,
         "An id led by a space, which Jellyfin reads as the id. The door refuses it."),
    Case("id-with-dashes-outside-the-member-libraries", "/Videos/{closed_dashed}/stream?static=true", REFUSE,
         "A film the member may not see, its id written with dashes.", presents="authorization"),
    Case("id-with-dashes-with-the-member-session", "/Videos/{open_dashed}/stream?static=true", PASS,
         "A film the member may see, its id written with dashes.", presents="authorization"),
    Case("id-in-capitals-presenting-nothing", "/VIDEOS/{closed_upper}/STREAM?static=true", REFUSE,
         "The route and the id in capitals; Jellyfin's routes ignore case, and so does the door."),
    Case("traversal-to-another-item", "/Videos/{open}/../{closed}/stream?static=true", REFUSE,
         "A path that names one item and climbs to another. The door asks about the one the path resolves to.",
         presents="authorization"),
    Case("catalogue-with-the-member-session", "/Items?Recursive=true", PASS,
         "Discovery passes through; Jellyfin answers it under the member's own policy.", presents="authorization"),
    Case("public-information", "/System/Info/Public", PASS,
         "What a client asks before anybody signs in passes through."),
    Case("web-client", "/web/", PASS, "Jellyfin's web client passes through."),
    Case("stop-transcoding", "/Videos/ActiveEncodings?deviceId=door&playSessionId=door", PASS,
         "A route under /Videos that names no item passes; Jellyfin requires a session for it.",
         method="DELETE", presents="authorization"),
    Case("household-claiming-a-remote-client", STREAM, PASS,
         "A household device naming a client outside the house in X-Forwarded-For. The door trusts nobody but the proxy to say that, so Jellyfin is told the device's own address.",
         presents="authorization", forwarded_for=REMOTE),
    Case("proxy-naming-a-remote-client", STREAM, REFUSE,
         "The proxy naming a client outside the house. The door trusts the proxy's fixed address, and Jellyfin, told that client, refuses a member who may not play from outside.",
         presents="authorization", origin=FROM_PROXY, forwarded_for=REMOTE),
    Case("proxy-naming-a-household-client", STREAM, PASS,
         "The proxy naming a client inside the house.",
         presents="authorization", origin=FROM_PROXY, forwarded_for=HOUSEHOLD),
    Case("neighbour-claiming-a-remote-client", STREAM, PASS,
         "Another container on the door's network naming a client outside the house. It is not the proxy, so Jellyfin is told the container's own address.",
         presents="authorization", origin=FROM_NEIGHBOUR, forwarded_for=REMOTE),
)


def dashed(item: str) -> str:
    return f"{item[:8]}-{item[8:12]}-{item[12:16]}-{item[16:20]}-{item[20:]}"


def request_of(case: Case, open_item: str, closed_item: str, token: str) -> tuple[str, dict[str, str]]:
    """The path and headers a case sends, with the items and the token filled in."""
    path = case.path.format(
        open=open_item, closed=closed_item, token=token, open_dashed=dashed(open_item),
        closed_dashed=dashed(closed_item), closed_upper=closed_item.upper(),
    )
    headers = {
        "authorization": {"Authorization": f'MediaBrowser Client="door", Device="door", DeviceId="door", '
                                           f'Version="1", Token="{token}"'},
        "x-emby-token": {"X-Emby-Token": token},
        "x-mediabrowser-token": {"X-MediaBrowser-Token": token},
        "unknown": {"X-Emby-Token": UNKNOWN_TOKEN},
    }.get(case.presents, {})
    if case.forwarded_for:
        headers = {**headers, "X-Forwarded-For": case.forwarded_for}
    return path, headers


@dataclasses.dataclass
class Answer:
    status: int
    body: str
    content_type: str = ""


def docker(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, errors="replace", check=False,
                          env={**os.environ, **(env or {})})


def must(done: subprocess.CompletedProcess, what: str) -> str:
    if done.returncode != 0:
        raise RuntimeError(f"{what}: {done.stderr.strip()[-400:]}")
    return done.stdout.strip()


@dataclasses.dataclass
class Layout:
    """The door's networks and addresses, as the resolved Compose model states them."""

    pools: dict[str, tuple[str, str, bool]]
    door: dict[str, str]
    proxy: str
    neighbour: str


def layout() -> Layout:
    report = Report()
    model = load_compose_model(report)
    if model is None:
        raise RuntimeError("; ".join(report.errors))
    networks, services = model.get("networks") or {}, model.get("services") or {}
    pools = {}
    for name in (DOOR_NETWORK, DOOR_UPSTREAM):
        config = (((networks.get(name) or {}).get("ipam") or {}).get("config") or [{}])[0]
        pools[name] = (config.get("subnet", ""), config.get("ip_range", ""), bool((networks.get(name) or {}).get("internal")))
    door = {name: fixed_address(services.get(DOOR, {}), name) or "" for name in pools}
    proxy = fixed_address(services.get(PROXY, {}), DOOR_NETWORK) or ""
    if not all([*door.values(), proxy, *(pool[0] and pool[1] for pool in pools.values())]):
        raise RuntimeError("the Compose model fixes no subnet or no address for the door or the proxy; "
                           "validate_manifest.py says which")
    subnet, handed_out = (ipaddress.ip_network(part) for part in pools[DOOR_NETWORK][:2])
    # Another address on the door's network: one nobody holds, Docker does not
    # hand out, and is not the gateway Docker keeps, the subnet's first host.
    hosts = list(subnet.hosts())
    neighbour = next(str(address) for address in hosts[1:]
                     if str(address) not in {door[DOOR_NETWORK], proxy} and address not in handed_out)
    return Layout(pools, door, proxy, neighbour)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


class Rig:
    """The door's two networks, Jellyfin or its stand-in behind them, and the
    door itself, all removed again by name whatever happened."""

    def __init__(self, place: Layout) -> None:
        self.place = place
        self.run = PREFIX + secrets.token_hex(3)
        self.networks = {name: f"{self.run}-{name}" for name in place.pools}
        self.containers: list[str] = []
        self.door_name = f"{self.run}-door"
        self.upstream_name = f"{self.run}-jellyfin"
        self.ports: dict[int, int] = {}
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix=PREFIX))

    def __enter__(self) -> Rig:
        for name, network in self.networks.items():
            subnet, handed_out, internal = self.place.pools[name]
            must(docker("network", "create", "--subnet", subnet, "--ip-range", handed_out,
                        *(["--internal"] if internal else []), network),
                 f"the {name} network ({subnet}) could not be made; a stack holding it may be running here")
        door_pair.write(self.workdir)
        return self

    def __exit__(self, *_: object) -> None:
        for name in reversed(self.containers):
            docker("rm", "-fv", name)
        for network in self.networks.values():
            docker("network", "rm", network)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def start_upstream(self, image: str, options: list[str], *, published: int | None = None) -> None:
        """Jellyfin, or what stands in for it, on `door-upstream` alone, where the
        door reaches it as `jellyfin`. Published on loopback where asked, through a
        network of its own, because an internal network publishes nothing."""
        name = self.upstream_name
        create = ["create", "--name", name]
        if published:
            direct = f"{self.run}-direct"
            must(docker("network", "create", direct), "a network for Jellyfin's own port")
            self.networks["direct"] = direct
            create += ["--network", direct, "-p", f"{HOST}:{published}:8096"]
        else:
            create += ["--network", self.networks[DOOR_UPSTREAM], "--network-alias", "jellyfin"]
        must(docker(*create, *options, image), "Jellyfin, or its stand-in, could not be made")
        self.containers.append(name)
        if published:
            must(docker("network", "connect", "--alias", "jellyfin", self.networks[DOOR_UPSTREAM], name),
                 "Jellyfin could not join the door's upstream network")
        must(docker("start", name), "Jellyfin's stand-in did not start")

    def start_door(self, caddyfile: str) -> None:
        """The door, as compose/media.yml runs it, on both networks at its fixed addresses."""
        docker("rm", "-fv", self.door_name)
        directory = self.workdir / "door"
        directory.mkdir(exist_ok=True)
        (directory / "Caddyfile").write_text(caddyfile, encoding="utf-8")
        for name in (door_pair.CERTIFICATE, door_pair.KEY):
            shutil.copy2(self.workdir / name, directory / name)
        self.ports = {PLAIN: free_port(), TLS: free_port()}
        must(docker(
            "create", "--name", self.door_name,
            "--network", self.networks[DOOR_NETWORK], "--ip", self.place.door[DOOR_NETWORK],
            "--network-alias", DOOR,
            "-p", f"{HOST}:{self.ports[PLAIN]}:{PLAIN}", "-p", f"{HOST}:{self.ports[TLS]}:{TLS}",
            "--user", f"{os.getuid()}:{os.getgid()}", "--read-only",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/data:mode=1777", "--tmpfs", "/config:mode=1777",
            "-v", f"{directory}:/etc/caddy:ro",
            door_image(),
        ), "the door could not be made")
        if self.door_name not in self.containers:
            self.containers.append(self.door_name)
        must(docker("network", "connect", "--ip", self.place.door[DOOR_UPSTREAM],
                    self.networks[DOOR_UPSTREAM], self.door_name), "the door could not join its upstream network")
        must(docker("start", self.door_name), "the door did not start")
        deadline = time.monotonic() + READY_S
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, http.client.HTTPException):
                self.ask_household(PLAIN, "GET", "/", {})
                return
            time.sleep(POLL_S)
        raise RuntimeError(f"the door did not answer on {PLAIN}: {docker('logs', self.door_name).stderr[-400:]}")

    def pinned_context(self) -> ssl.SSLContext:
        """Trust in the one certificate written beside the door, and nothing else,
        the way a client pins the door, for the address it is asked at."""
        return ssl.create_default_context(cafile=str(self.workdir / door_pair.CERTIFICATE))

    def ask_household(self, port: int, method: str, path: str, headers: dict[str, str]) -> Answer:
        context = self.pinned_context() if port == TLS else None
        return ask_at(self.ports[port], method, path, headers, context)

    def ask_inside(self, address: str, method: str, path: str, headers: dict[str, str]) -> Answer:
        """A request from a container on the door's network, at `address`.

        The headers reach curl through the environment, so no token is ever on a
        command line.
        """
        env = {f"DOOR_HEADER_{number}": f"{name}: {value}" for number, (name, value) in enumerate(headers.items())}
        env["DOOR_URL"] = f"{SCHEME}://{DOOR}:{PLAIN}{path}"
        env["DOOR_METHOD"] = method
        script = (
            'set -- -s -X "$DOOR_METHOD" -w "\\n%{http_code} %{content_type}"; '
            'for n in 0 1 2 3; do eval "h=\\${DOOR_HEADER_$n:-}"; [ -n "$h" ] && set -- "$@" -H "$h"; done; '
            'exec curl "$@" "$DOOR_URL"'
        )
        done = must(docker("run", "--rm", "--network", self.networks[DOOR_NETWORK], "--ip", address,
                           *[flag for name in env for flag in ("-e", name)], "--entrypoint", "sh",
                           door_image(), "-c", script, env=env),
                    "a request from inside the door's network")
        body, _, last = done.rpartition("\n")
        status, _, content_type = last.partition(" ")
        return Answer(int(status), body, content_type)

    def ask(self, case: Case, port: int, request: tuple[str, dict[str, str]]) -> Answer:
        path, headers = request
        if case.origin == FROM_HOUSEHOLD:
            return self.ask_household(port, case.method, path, headers)
        address = self.place.proxy if case.origin == FROM_PROXY else self.place.neighbour
        return self.ask_inside(address, case.method, path, headers)

    def presented_fingerprint(self) -> str:
        with socket.create_connection((HOST, self.ports[TLS]), timeout=ATTEMPT_TIMEOUT_S) as raw, \
                self.pinned_context().wrap_socket(raw, server_hostname=HOST) as wrapped:
            der = wrapped.getpeercert(binary_form=True) or b""
        return hashlib.sha256(der).hexdigest()


def ask_at(port: int, method: str, path: str, headers: dict[str, str],
           context: ssl.SSLContext | None = None) -> Answer:
    """One request to a port published on loopback, over TLS where a context is given."""
    connection = (http.client.HTTPSConnection(HOST, port, context=context, timeout=ATTEMPT_TIMEOUT_S)
                  if context else http.client.HTTPConnection(HOST, port, timeout=ATTEMPT_TIMEOUT_S))
    try:
        connection.request(method, path, headers=headers)
        reply = connection.getresponse()
        body = reply.read(4096).decode("utf-8", errors="replace")
        return Answer(reply.status, body, reply.getheader("Content-Type") or "")
    finally:
        connection.close()


def door_image() -> str:
    return pinned(next(s for s in stack_manifest.load(ROOT)["service"] if s["id"] == DOOR))


def id_pattern(item: str) -> str:
    return "-?".join((item[:8], item[8:12], item[12:16], item[16:20], item[20:]))


def stand_in(open_item: str, token: str) -> str:
    """Jellyfin's answers to the door's question, and a record of everything else.

    The member here may not play from outside the house, so a question naming
    a client outside it is refused, as Jellyfin refuses it.
    """
    member = (f"header({{'X-Emby-Token': '{token}'}}) || header({{'X-MediaBrowser-Token': '{token}'}}) || "
              f"header({{'Authorization': '*Token=\"{token}\"*'}}) || query({{'ApiKey': '{token}'}}) || "
              f"query({{'api_key': '{token}'}})")
    question = r"(?i)^/Items/[0-9a-f-]+$"
    return f"""{{
	admin off
	auto_https off
}}

:8096 {{
	@remote {{
		path_regexp {question}
		header X-Forwarded-For {REMOTE}
	}}
	@seen {{
		path_regexp (?i)^/Items/{id_pattern(open_item)}$
		expression `{member}`
	}}
	@unseen {{
		path_regexp {question}
		expression `{member}`
	}}
	@question path_regexp {question}
	route {{
		respond @remote 403
		respond @seen `{{"Id":"{open_item}"}}` 200
		respond @unseen 404
		respond @question 401
		respond "{SERVED}{{path}} for {{header.X-Forwarded-For}} from {{remote_host}}" 200
	}}
}}
"""


def told(answer: Answer) -> tuple[str, str]:
    """What the stand-in was told: the client named, and the address it came from."""
    _, _, rest = answer.body.partition(" for ")
    forwarded, _, origin = rest.rpartition(" from ")
    return forwarded.strip(), origin.strip()


def verdict(answer: Answer, stand_in_behind: bool) -> str | None:
    if answer.status >= 400:
        return REFUSE
    if answer.status < 300 and (not stand_in_behind or answer.body.startswith(SERVED)):
        return PASS
    return None


def told_problems(rig: Rig, case: Case, where: str, answer: Answer) -> list[str]:
    """What Jellyfin was told about a request the door passed: reached from the
    door's fixed address, and, from inside the door's network, the client only
    the proxy may name."""
    problems: list[str] = []
    forwarded, origin = told(answer)
    if origin != rig.place.door[DOOR_UPSTREAM]:
        problems.append(f"{where}: Jellyfin was reached from {origin}, not the door's fixed address "
                        f"{rig.place.door[DOOR_UPSTREAM]}, the one it trusts")
    expected = {FROM_PROXY: case.forwarded_for, FROM_NEIGHBOUR: rig.place.neighbour}.get(case.origin)
    if expected is not None and forwarded != expected:
        problems.append(f"{where}: Jellyfin was told the client is {forwarded!r}, not {expected!r}")
    return problems


def judge(rig: Rig, items: tuple[str, str], token: str) -> list[str]:
    """Every case on every port it is asked on, against its verdict and, for each
    that passed, against what Jellyfin was told about the client."""
    problems: list[str] = []
    households: set[str] = set()
    for case in CASES:
        ports = (PLAIN, TLS) if case.origin == FROM_HOUSEHOLD else (PLAIN,)
        for port in ports:
            answer = rig.ask(case, port, request_of(case, *items, token))
            got = verdict(answer, stand_in_behind=True)
            where = f"{case.name} on {port}"
            if got != case.expect:
                problems.append(f"{where}: expected the door to {case.expect}, and it answered {answer.status}")
            elif got == PASS:
                problems += told_problems(rig, case, where, answer)
                if case.origin == FROM_HOUSEHOLD:
                    households.add(told(answer)[0])
    if len(households) != 1 or households & {REMOTE, HOUSEHOLD, ""} or any("," in one for one in households):
        problems.append(f"household requests named {sorted(households)} to Jellyfin; each should name the one "
                        "address the device connected from, and nothing it claimed")
    return problems


def held_to_recordings(directory: pathlib.Path = RECORDINGS) -> list[str]:
    """Each case's recording of Jellyfin itself agrees with the case's verdict, and
    every recording here is one of the cases."""
    problems: list[str] = []
    names = {case.name for case in CASES}
    for stray in sorted(path.stem for path in directory.glob("*.json") if path.stem not in names):
        problems.append(f"recordings/door/{stray}.json records no case in CASES")
    for case in CASES:
        path = directory / f"{case.name}.json"
        if not path.is_file():
            problems.append(f"recordings/door/{case.name}.json is missing; `just record-door` takes it")
            continue
        recorded = json.loads(path.read_text(encoding="utf-8"))
        if recorded.get("request", {}).get("path") != case.path:
            problems.append(f"recordings/door/{case.name}.json asked {recorded.get('request', {}).get('path')!r}, "
                            f"and the case asks {case.path!r}; record it again")
            continue
        status = recorded.get("door", {}).get("status", 0)
        if verdict(Answer(status, ""), stand_in_behind=False) != case.expect:
            problems.append(f"recordings/door/{case.name}.json: Jellyfin behind the door answered {status}, "
                            f"and the case expects the door to {case.expect}")
    return problems


@contextlib.contextmanager
def rigged() -> Iterator[tuple[Rig, tuple[str, str], str]]:
    """A rig with the stand-in behind it, and the items and token it answers for."""
    items = (secrets.token_hex(16), secrets.token_hex(16))
    token = secrets.token_hex(16)
    with Rig(layout()) as rig:
        stand = rig.workdir / "stand-in"
        stand.mkdir()
        (stand / "Caddyfile").write_text(stand_in(items[0], token), encoding="utf-8")
        rig.start_upstream(door_image(), ["-v", f"{stand}:/etc/caddy:ro"])
        yield rig, items, token


def check() -> int:
    problems = held_to_recordings()
    with rigged() as (rig, items, token):
        rig.start_door(CADDYFILE.read_text(encoding="utf-8"))
        problems += judge(rig, items, token)
        written = door_pair.fingerprint(rig.workdir / door_pair.CERTIFICATE)
        presented = rig.presented_fingerprint()
        if presented != written:
            problems.append(f"{TLS} presented a certificate with SHA-256 {presented}, not the one written beside "
                            f"the Caddyfile, {written}")
        must(docker("stop", rig.upstream_name), "Jellyfin's stand-in did not stop")
        unanswered = CASES[1]
        answer = rig.ask(unanswered, PLAIN, request_of(unanswered, *items, token))
        if verdict(answer, stand_in_behind=True) != REFUSE:
            problems.append(f"with Jellyfin not answering, {unanswered.name} was answered {answer.status}")
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(f"\n{len(problems)} problem(s). The door lets through what it must refuse, or refuses what it "
              "must let through.", file=sys.stderr)
        return 1
    print(f"the door held: {len(CASES)} cases on both ports and from inside its network, the certificate it "
          "presents is the one written beside it, and with Jellyfin silent it refuses")
    return 0


# Doors that are wrong in one way each, made from the shipped one so they stay
# close to it, and the case each must be caught by.
BROKEN = (
    ("a door that guards nothing", "stream-presenting-nothing",
     lambda text: text.replace("(?i)^(?:/emby|/mediabrowser)?", "(?i)^/nowhere(?:/emby|/mediabrowser)?")),
    ("a door that lets nothing through", "public-information",
     lambda text: text.replace("\timport guard\n", "\trespond 403\n")),
    ("a door that believes any private address about the client", "neighbour-claiming-a-remote-client",
     lambda text: text.replace("trusted_proxies static 10.80.96.3", "trusted_proxies static private_ranges")),
    ("a door that hands on the proxy's chain as it came", "proxy-naming-a-remote-client",
     lambda text: text.replace("\theader_up X-Forwarded-For {client_ip}\n", "")),
)


def recordings_caught() -> int:
    """A recording that disagrees with its case, one missing and one stray, each named."""
    failures = 0
    refused = next(case for case in CASES if case.expect == REFUSE)
    with tempfile.TemporaryDirectory() as tmp:
        directory = pathlib.Path(tmp)
        for case in CASES:
            shutil.copyfile(RECORDINGS / f"{case.name}.json", directory / f"{case.name}.json")
        if held_to_recordings(directory):
            print("::error::self-test: the recordings as committed were refused")
            return 1
        passed = {"request": {"path": refused.path}, "door": {"status": 200}}
        (directory / f"{refused.name}.json").write_text(json.dumps(passed), encoding="utf-8")
        (directory / f"{CASES[-1].name}.json").unlink()
        (directory / "no-such-case.json").write_text("{}", encoding="utf-8")
        found = held_to_recordings(directory)
        for said, needle in (("a recording that passes what its case refuses", refused.name),
                             ("a missing recording", CASES[-1].name), ("a stray recording", "no-such-case")):
            if any(needle in problem for problem in found):
                print(f"  ok   {said} named")
            else:
                print(f"::error::self-test: {said} was not named: {found}")
                failures += 1
    return failures


def self_test() -> int:
    shipped = CADDYFILE.read_text(encoding="utf-8")
    failures = recordings_caught()
    with rigged() as (rig, items, token):
        for said, caught_by, broken in BROKEN:
            text = broken(shipped)
            if text == shipped:
                print(f"::error::self-test: {said} is the shipped door; the fixture is stale")
                failures += 1
                continue
            rig.start_door(text)
            problems = judge(rig, items, token)
            if any(problem.startswith(caught_by) for problem in problems):
                print(f"  ok   {said} caught by {caught_by}")
            else:
                print(f"::error::self-test: {said} was not caught by {caught_by}: {problems or 'nothing'}")
                failures += 1
    if failures:
        return 1
    print("\nself-test passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true", help="prove each kind of broken door is caught")
    arguments = parser.parse_args()
    try:
        return self_test() if arguments.self_test else check()
    except RuntimeError as failed:
        print(f"::error::{failed}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
