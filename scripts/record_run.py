"""One fresh container of one pinned image, as record.py runs it: how it is
addressed, asked and read, and what a recipe tells it about the service.
"""

from __future__ import annotations

import base64
import dataclasses
import http.client
import io
import json
import secrets
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

# Distinct from every name `docker compose` gives the stack's own containers,
# so nothing here can address — or remove — a container the operator runs.
CONTAINER_PREFIX = "lemonfiber-record-"

# Published on loopback only, on a port the kernel chose as free, so a recording
# can be taken on a machine already running the stack.
HOST = "127.0.0.1"
SCHEME = "http"

# How long a service may take to answer at all after it starts, and how often it
# is asked meanwhile.
READY_S = 240
POLL_S = 2.0
ATTEMPT_TIMEOUT_S = 10

# The media type every JSON exchange here is sent and asked for as.
JSON_TYPE = "application/json"


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

    def __init__(self, sid: str, reference: str, port: int, config: str) -> None:
        self.sid = sid
        self.reference = reference
        self.port = port
        self.config = config
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
        """A file in the container's configuration, read out of the container.

        Through `docker cp`, which needs nothing inside the image to read with,
        so a distroless one is read like any other.
        """
        copied = subprocess.run(["docker", "cp", f"{self.name}:{self.config}/{inside}", "-"],
                                capture_output=True, check=False)
        if copied.returncode != 0:
            raise OSError(f"{self.sid} has no {inside} in its configuration yet")
        with tarfile.open(fileobj=io.BytesIO(copied.stdout)) as archive:
            member = next((one for one in archive.getmembers() if one.isfile()), None)
            held = archive.extractfile(member) if member else None
            if held is None:
                raise OSError(f"{self.sid}'s {inside} is not a file")
            return held.read().decode("utf-8")

    def logs(self) -> str:
        done = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True, check=False)
        return done.stdout + done.stderr

    def identities(self) -> set[str]:
        """What names this container and the machine it ran on."""
        inspected = json.loads(subprocess.run(["docker", "inspect", self.name], capture_output=True,
                                              text=True, check=True).stdout)[0]
        found = {inspected["Id"], inspected["Id"][:12], inspected["Config"]["Hostname"]}
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
    # Members redacted wherever they appear, at any depth: identifiers an
    # instance gives what it holds, which a list of items repeats once per item.
    redact_named: tuple[str, ...] = ()
