#!/usr/bin/env python3
"""Start the stack with plain `docker compose`, and prove it came up.

Every other check in this repository reads the project rather than running it.
`check_forms.py` resolves each form through `docker compose config`, which
answers whether Compose can *build* a model — not whether that model starts,
stays up, or serves anything. The distance between those two answers is the
whole of `F1-R1`: the stack is only operable without lemonfiber if it runs
without lemonfiber, and resolving is not running.

So this starts one. For each profile named it brings the services up with
nothing but `docker compose`, then asks each of them the question the manifest
itself declares — the `health` table, on the port the manifest publishes it on,
inside the timeout the manifest gives it — and tears the project down again.

Four things are asserted, and each fails for a different reason:

  * the project that came up holds exactly the services the manifest puts in
    those profiles, each running the pinned image and none of them restarting;
  * every service answers its declared probe within its declared timeout, which
    a container that merely exists cannot do;
  * a service recorded in `KNOWN_BROKEN` is *still* broken — the register points
    the other way, so an entry cannot outlive the defect it describes;
  * `down` leaves nothing behind.

`--plan` prints the profiles this can be asked to run, which is what CI fans its
matrix out over, so a profile added to the manifest is covered without anybody
remembering to add it. `UNRUNNABLE` is the other half of that: a profile that
cannot be started by a machine with no accounts, named here with the reason, so
the omission is a decision on the record rather than a gap.

Run from the repo root. `--self-test` proves the verdicts without Docker.
Exit 0 = the stack ran, 1 = it did not, 2 = it was asked for something it
cannot start.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request

from registry import pinned

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Distinct from the project name a bare `docker compose up` would pick — the
# directory's — because `down --volumes` at the end of this would otherwise
# take an operator's own running stack with it on the machine where both live.
PROJECT = "lemonfiber-runs-check"

# The published ports are bound to 127.0.0.1 for the admin tier and to
# ${LAN_BIND} for the household one; the loopback address reaches both.
HOST = "127.0.0.1"

# Plain, and deliberately: nothing in this stack terminates TLS. Certificates
# are Caddy's job on the operator's own domain, and self-signed ones are
# refused on purpose — see the proxy profile. A probe speaks what the service
# speaks, and here that is cleartext across a loopback interface.
SCHEME = "http"

# A profile nobody without an account can start, and why. Naming it here is what
# keeps the omission visible: --plan leaves it out, and asking for it by hand is
# refused with the reason rather than with a timeout nobody can read.
UNRUNNABLE = {
    "torrent": (
        "gluetun needs a real VPN provider and key to establish a tunnel, and "
        "qbittorrent waits on gluetun being healthy before it starts at all. "
        "Neither is reachable from a machine holding no subscription, so this "
        "profile is verified by hand on hardware — the M1 exit criterion the "
        "stack README already carries."
    ),
}

# Services this check knows do not start, and does not fail for. Empty, and
# deliberately kept: the first run of this check found three — Jellyfin, Seerr
# and Bindery, none of which could write a `/config` Docker had created as root
# — and the register is what proved the fix, by going red the moment all three
# answered and asking to be emptied.
#
# The register is inverted. A service named here that starts and answers **fails**
# this check, because the entry has outlived what it describes and the next
# reader would take it for a standing defect. Nothing goes in here to make a run
# pass: an entry is a reason `F1-R1` is not yet true, and belongs in a change
# that says so.
KNOWN_BROKEN: dict[str, str] = {}

# A service whose declared health is `container` publishes no port and answers
# no request, so the only runtime evidence available is that it is still there a
# while after being started — long enough that a configuration it cannot accept
# would have stopped it. Both services of that kind here are daemons that idle
# when unseeded, which is exactly the state CI starts them in.
CONTAINER_SETTLE_S = 30

# How often a pending probe is retried. Short enough that the elapsed time
# reported is worth reading, long enough not to hammer a service still booting.
POLL_S = 1.0

# Room for one more poll past the dwell, so a container-kind service is never
# reported late for the arithmetic rather than for its behaviour.
POLL_MARGIN_S = 10

# A single attempt's own ceiling. Separate from the manifest's timeout, which
# bounds the whole wait: a connect that hangs must not eat the budget the
# manifest gave the service to boot in.
ATTEMPT_TIMEOUT_S = 5

# How long a connected socket is held open waiting for the far end to say
# something or hang up. A server with nothing to say yet says nothing, and that
# silence is the evidence; see `ask_tcp` for why a connection on its own is not.
LISTEN_READ_S = 1.5


def manifest() -> dict:
    return tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))


def runnable_profiles(data: dict) -> list[str]:
    """Every profile the manifest declares that a machine with no accounts can start."""
    return [p["id"] for p in data["profile"] if p["id"] not in UNRUNNABLE]


def plan(data: dict) -> list[dict]:
    """The groups CI fans out over: each profile on its own, and then all of them.

    Two different promises, and neither implies the other. A profile alone is
    what `--profile subs up` has to do for a form to be a subset of the stack
    rather than a slice of one; all of them at once is what an operator actually
    starts, and the only place a collision between two profiles — a port, a name,
    a volume — can show itself.

    Derived from the manifest rather than listed in the workflow, so a profile
    added later is covered by the next run instead of by somebody remembering.
    """
    profiles = runnable_profiles(data)
    groups = [{"name": profile, "profiles": profile} for profile in profiles]
    groups.append({"name": "every runnable profile at once", "profiles": ",".join(profiles)})
    return groups


def services_in(data: dict, profiles: list[str]) -> list[dict]:
    return [s for s in data["service"] if s["profile"] in profiles]


def probe_of(service: dict) -> dict:
    """What will be asked of one service, and how long it has to answer.

    Read from the manifest rather than chosen here: the point is to prove the
    contract lemonfiber consumes, not a second opinion about it.
    """
    health = service.get("health", {})
    kind = health.get("kind", "container")
    # A container-kind service is asked nothing, so its budget is the dwell it
    # has to survive plus room for one more poll — never being late is the point.
    default_timeout = CONTAINER_SETTLE_S + POLL_MARGIN_S
    return {
        "id": service["id"],
        "kind": kind,
        "port": service.get("port"),
        "path": health.get("path", ""),
        "timeout_s": health.get("timeout_s", default_timeout),
        "settle_s": CONTAINER_SETTLE_S if kind == "container" else 0,
        "image": pinned(service),
    }


def ask_http(port: int, path: str) -> tuple[bool, str]:
    """One request to a published port, and what came back.

    A status is what proves something served it, so a 404 or a 401 is a failure
    and not a pass: the path is the manifest's own, and a service answering
    something else about it means the contract is wrong even though the port is
    open.
    """
    url = f"{SCHEME}://{HOST}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=ATTEMPT_TIMEOUT_S) as reply:
            return reply.status < 400, f"HTTP {reply.status}"
    except urllib.error.HTTPError as answered:
        return False, f"HTTP {answered.code}"
    except (http.client.HTTPException, OSError) as unreachable:
        return False, f"{type(unreachable).__name__}: {unreachable}"


def ask_tcp(port: int) -> tuple[bool, str]:
    """A connection that is held, which is not the same as a connection accepted.

    Docker publishes a port by putting a proxy in front of it, and that proxy
    accepts before it has any idea whether anything inside the container is
    listening. A service replaced by `sleep infinity` therefore answers a bare
    connect exactly as the real one does — which is the shape of every
    integration test that proves nothing.

    What separates them is what happens next: the proxy, finding nothing behind
    it, closes at once and the read returns end-of-file. A server waiting for a
    request says nothing and the read times out. So silence is the pass here,
    and being hung up on is the failure.
    """
    try:
        with socket.create_connection((HOST, port), timeout=ATTEMPT_TIMEOUT_S) as held:
            held.settimeout(LISTEN_READ_S)
            try:
                first = held.recv(1)
            except TimeoutError:
                return True, "held open"
            if first:
                return True, f"greeted with {first!r}"
            return False, "accepted, then closed at once — nothing is listening behind the published port"
    except OSError as refused:
        return False, f"{type(refused).__name__}: {refused}"


def attempt(probe: dict, elapsed: float) -> tuple[bool, str]:
    if probe["kind"] == "http":
        return ask_http(probe["port"], probe["path"])
    if probe["kind"] == "tcp":
        return ask_tcp(probe["port"])
    # `container`: nothing to ask, so the evidence is that it is still there
    # after a dwell long enough for a configuration it cannot accept to have
    # stopped it. What it is doing meanwhile the roster says.
    settled = elapsed >= probe["settle_s"]
    return settled, f"still up after {int(elapsed)}s" if settled else f"{int(elapsed)}s of dwell"


def wait_for_all(probes: list[dict], ask, clock, pause) -> list[dict]:
    """Every probe, retried until it answers or its own budget runs out.

    Round-robin rather than one service at a time, so a form takes as long as
    its slowest member rather than as long as all of them added together — and
    so the elapsed time recorded against each is the time that service took,
    not the time it spent queued behind another.

    `ask`, `clock` and `pause` are passed in so the waiting can be driven
    without Docker and without spending the wall clock it describes.
    """
    started = clock()
    pending = {probe["id"]: probe for probe in probes}
    last: dict[str, str] = {probe["id"]: "not yet asked" for probe in probes}
    answered: list[dict] = []

    while pending:
        for name, probe in list(pending.items()):
            elapsed = clock() - started
            ok, detail = ask(probe, elapsed)
            last[name] = detail
            if ok:
                answered.append({**probe, "elapsed_s": round(elapsed, 1), "detail": detail})
                del pending[name]
            elif elapsed >= probe["timeout_s"]:
                answered.append({**probe, "elapsed_s": round(elapsed, 1), "detail": detail, "late": True})
                del pending[name]
        if pending:
            pause(POLL_S)

    return answered


def judge_probe(answer: dict) -> str | None:
    if answer.get("late"):
        return (
            f"{answer['id']}: never answered its {answer['kind']} probe in the "
            f"{answer['timeout_s']}s the manifest allows it — last: {answer['detail']}"
        )
    return None


def judge_roster(expected: dict[str, str], observed: list[dict]) -> list[str]:
    """What the project that came up holds, against what the manifest says it should.

    Four different failures, and each one means something else: a service that
    never appeared, one that appeared and should not have, one that is there but
    not running, and one running an image that is not the pin.
    """
    problems: list[str] = []
    seen = {row["service"]: row for row in observed}

    for name in sorted(set(expected) - set(seen)):
        problems.append(f"{name}: the manifest puts it in these profiles, and no container came up for it")

    for name in sorted(set(seen) - set(expected)):
        problems.append(f"{name}: came up, and the manifest puts it outside these profiles")

    for name in sorted(set(expected) & set(seen)):
        row = seen[name]
        if row["state"] != "running":
            problems.append(f"{name}: is {row['state']}, not running")
        elif row["restarts"]:
            problems.append(f"{name}: has restarted {row['restarts']} time(s) — it is not staying up")
        if row["image"] != expected[name]:
            problems.append(f"{name}: is running {row['image']}, and the manifest pins {expected[name]}")

    return problems


def judge_known(broken: set[str], answers: list[dict], observed: list[dict]) -> list[str]:
    """A recorded defect that is no longer there.

    The register points the other way from every other verdict here. While a
    service in it stays broken this says nothing — the failure is described in
    `KNOWN_BROKEN` and describing it twice adds nothing. What it refuses is the
    entry that has been fixed and not removed, because that is the state in which
    a reader is told a working service is broken, and in which a regression in it
    would be invisible.
    """
    running = {row["service"] for row in observed if row["state"] == "running" and not row["restarts"]}
    answered = {answer["id"] for answer in answers if not answer.get("late")}

    return [
        f"{name}: is up and answering, so it is no longer broken — take it out of KNOWN_BROKEN"
        for name in sorted(broken & running & answered)
    ]


def judge_teardown(remaining: list[str]) -> str | None:
    if remaining:
        return f"down left {sorted(remaining)} behind"
    return None


def unrunnable_reason(profile: str) -> str | None:
    return UNRUNNABLE.get(profile)


def bare_environment(data_root: str) -> dict[str, str]:
    """The environment Compose is given, and nothing else.

    The same discipline `check_forms.py` applies for the same reason: what the
    stack needs must not depend on whose machine is running it. What is supplied
    is exactly what `.env.example` tells an operator to set and no more — a data
    root, because a bind mount needs a source, and the uid pair, because that
    file defines it as `id -u` and `id -g` and every service that drops
    privileges is started as it.

    Leaving the pair at its default was this check's own first mistake. A runner
    whose user is 1001 wrote a clone owned by 1001 and then started services as
    1000, which is not a configuration any operator following the instructions
    would have — and it made three services look broken for a reason that was
    half the harness's.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
        "DATA_ROOT": data_root,
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
    }


def compose(env: dict[str, str], empty_env_file: str, *args: str) -> subprocess.CompletedProcess:
    command = ["docker", "compose", "--env-file", empty_env_file, "--project-name", PROJECT, *args]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def roster(env: dict[str, str], empty_env_file: str) -> list[dict]:
    """Every container the project holds, with what the daemon says about it.

    `compose ps` names them; `docker inspect` is asked for the rest, because the
    image a container was created from and the number of times it has restarted
    are what separate a service that is up from one that is looping.
    """
    listed = compose(env, empty_env_file, "ps", "--all", "--format", "json")
    rows = [json.loads(line) for line in listed.stdout.splitlines() if line.strip()]

    observed = []
    for row in rows:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}|{{.State.Status}}|{{.RestartCount}}", row["ID"]],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        image, state, restarts = [*inspected.stdout.strip().split("|"), "", "", "0"][:3]
        observed.append(
            {
                "service": row["Service"],
                "image": image,
                "state": state,
                "restarts": int(restarts or 0),
            }
        )
    return observed


def report_failure(env: dict[str, str], empty_env_file: str, names: list[str]) -> None:
    """The logs of whatever did not come up, so the failure is readable from the run."""
    for name in names:
        logs = compose(env, empty_env_file, "logs", "--tail", "40", name)
        print(f"\n--- {name} ---")
        print(logs.stdout.strip() or logs.stderr.strip() or "(no output)")


def refuse(profiles: list[str], declared: set[str]) -> str | None:
    """Why this cannot be asked, where it cannot.

    Ahead of any container, because each of these is a reason the answer would
    mean nothing rather than a reason it would be no — and a binary on the path
    is the one that would make the whole run worthless without failing it.
    """
    for profile in profiles:
        if profile not in declared:
            return f"no profile {profile!r} in stack.toml"
        reason = unrunnable_reason(profile)
        if reason is not None:
            return f"profile {profile!r} cannot be started here: {reason}"

    if shutil.which("lemonfiber", path=os.environ.get("PATH", "")) is not None:
        return "a lemonfiber binary is on PATH; this proves nothing while it is"

    return None


def describe(answers: list[dict], broken: set[str]) -> None:
    for answer in sorted(answers, key=lambda a: a["id"]):
        if answer["id"] in broken:
            print(f"  ---- {answer['id']:<24} known: {KNOWN_BROKEN[answer['id']]}")
            continue
        mark = "late" if answer.get("late") else "ok  "
        budget = f"{answer['elapsed_s']}s of {answer['timeout_s']}s"
        print(f"  {mark} {answer['id']:<24} {answer['kind']:<9} {budget:<14} {answer['detail']}")


def judge_everything(expected: dict[str, str], answers: list[dict], observed: list[dict]) -> list[str]:
    """Every verdict about one started project, with the recorded defects held apart.

    A service in `KNOWN_BROKEN` is left out of the roster and probe verdicts —
    both of which it would fail, for a reason already written down — and handed
    to the register instead, which refuses it only once it works.
    """
    broken = {name for name in expected if name in KNOWN_BROKEN}
    proved = {name: image for name, image in expected.items() if name not in broken}

    errors = judge_roster(proved, [row for row in observed if row["service"] not in broken])
    errors += [
        verdict
        for answer in answers
        if answer["id"] not in broken and (verdict := judge_probe(answer)) is not None
    ]
    return errors + judge_known(broken, answers, observed)


def run(profiles: list[str]) -> int:
    data = manifest()

    refusal = refuse(profiles, {p["id"] for p in data["profile"]})
    if refusal is not None:
        print(f"::error::{refusal}")
        return 2

    chosen = services_in(data, profiles)
    if not chosen:
        print(f"::error::profiles {profiles} hold no services")
        return 1

    expected = {s["id"]: pinned(s) for s in chosen}
    probes = [probe_of(service) for service in chosen]
    errors: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        empty = str(pathlib.Path(tmp) / "empty.env")
        pathlib.Path(empty).write_text("", encoding="utf-8")
        data_root = pathlib.Path(tmp) / "data"
        data_root.mkdir()
        env = bare_environment(str(data_root))

        selected = [argument for profile in profiles for argument in ("--profile", profile)]

        print(f"starting {len(chosen)} service(s) across {','.join(profiles)} with plain docker compose")
        started = compose(env, empty, *selected, "up", "--detach", "--quiet-pull")

        if started.returncode != 0:
            errors.append(f"the project did not start:\n{started.stderr.strip()}")
            compose(env, empty, "down", "--volumes", "--remove-orphans")
            return report(errors)

        try:
            answers = wait_for_all(probes, attempt, time.monotonic, time.sleep)
            broken = {name for name in expected if name in KNOWN_BROKEN}
            errors += judge_everything(expected, answers, roster(env, empty))
            describe(answers, broken)

            if errors:
                late = (answer["id"] for answer in answers if answer.get("late"))
                report_failure(env, empty, sorted(name for name in late if name not in broken))
        finally:
            removed = compose(env, empty, "down", "--volumes", "--remove-orphans")
            if removed.returncode != 0:
                errors.append(f"down failed:\n{removed.stderr.strip()}")

        left = judge_teardown([row["service"] for row in roster(env, empty)])
        if left is not None:
            errors.append(left)

    if errors:
        return report(errors)

    unproved = sorted(name for name in expected if name in KNOWN_BROKEN)
    if unproved:
        print(f"\n{len(unproved)} service(s) are recorded as not starting from a clean clone: {unproved}")
    print(
        f"\n{len(expected) - len(unproved)} service(s) started, answered, and were torn down. "
        "No lemonfiber binary involved."
    )
    return 0


def report(errors: list[str]) -> int:
    print()
    print("\n".join(f"::error::{error}" for error in errors))
    return 1


def probe_discriminates() -> list[str]:
    """`ask_tcp` against a socket that hangs up and one that does not.

    Both are listeners and both accept, which is exactly the trap: what Docker
    leaves in front of a container that serves nothing is a socket of the first
    kind, and it is indistinguishable from the second until something reads.
    """
    problems = []

    holding = socket.socket()
    holding.bind((HOST, 0))
    holding.listen(1)

    hanging_up = socket.socket()
    hanging_up.bind((HOST, 0))
    hanging_up.listen(1)

    def accept_then_close() -> None:
        connection, _ = hanging_up.accept()
        connection.close()

    closer = threading.Thread(target=accept_then_close, daemon=True)
    closer.start()

    try:
        ok, detail = ask_tcp(hanging_up.getsockname()[1])
        if ok:
            problems.append(f"a port that accepts and hangs up was judged up: {detail}")

        ok, detail = ask_tcp(holding.getsockname()[1])
        if not ok:
            problems.append(f"a port with a server waiting behind it was refused: {detail}")
    finally:
        closer.join(timeout=LISTEN_READ_S + ATTEMPT_TIMEOUT_S)
        holding.close()
        hanging_up.close()

    # And a port with nothing on it at all, which is the easy half.
    spare = socket.socket()
    spare.bind((HOST, 0))
    closed_port = spare.getsockname()[1]
    spare.close()
    if ask_tcp(closed_port)[0]:
        problems.append("a closed port was judged up")

    return problems


def roster_verdicts() -> list[str]:
    """`judge_roster` against five projects that came up wrong, and one that did not."""
    problems = []
    pinned = {"sonarr": "lscr.io/linuxserver/sonarr:4.0.15", "bazarr": "lscr.io/linuxserver/bazarr:1.4.5"}

    def row(service, image, state="running", restarts=0):
        return {"service": service, "image": image, "state": state, "restarts": restarts}

    sound = [row("sonarr", pinned["sonarr"]), row("bazarr", pinned["bazarr"])]
    stray = row("gluetun", "qmcgaw/gluetun:v3.40.0")

    cases = (
        ("a service that never came up", [sound[0]], "no container came up for it"),
        ("a service outside the profiles", [*sound, stray], "outside these profiles"),
        ("a service that exited", [row("sonarr", pinned["sonarr"], state="exited"), sound[1]], "not running"),
        ("a service in a restart loop", [row("sonarr", pinned["sonarr"], restarts=3), sound[1]], "not staying up"),
        ("a service running another image", [row("sonarr", "alpine:3.20"), sound[1]], "the manifest pins"),
    )

    for said, observed, because in cases:
        verdicts = judge_roster(pinned, observed)
        if not verdicts:
            problems.append(f"{said}: was judged sound")
        elif not any(because in verdict for verdict in verdicts):
            problems.append(f"{said}: said {verdicts!r}, which does not mention {because!r}")

    if judge_roster(pinned, sound):
        problems.append("a project holding exactly its own services, running and pinned, was refused")

    return problems


def probe_verdicts() -> list[str]:
    """The three judgements about one service, and the dwell a portless one gets.

    A service that answers late is the failure this whole check exists for: a
    container that is up and serving nothing is indistinguishable from a working
    stack in `docker compose ps`.
    """
    problems = []

    late = {"id": "sonarr", "kind": "http", "timeout_s": 90, "detail": "ConnectionRefusedError", "late": True}
    if judge_probe(late) is None:
        problems.append("a service that never answered was judged sound")
    if judge_probe({**late, "late": False}) is not None:
        problems.append("a service that answered was refused")

    if judge_teardown(["sonarr"]) is None:
        problems.append("a container left running after down was judged sound")
    if judge_teardown([]) is not None:
        problems.append("a clean teardown was refused")

    # The dwell is the only thing between "it was created" and "it is running"
    # for a service with nothing to ask: one that exits after ten seconds
    # answers the first poll exactly as one that stays.
    dwelling = {"id": "recyclarr", "kind": "container", "settle_s": 30, "timeout_s": 40}
    if attempt(dwelling, 5)[0]:
        problems.append("a container-kind service was judged up before its dwell had passed")
    if not attempt(dwelling, 31)[0]:
        problems.append("a container-kind service still up after its dwell was refused")

    return problems


def waiting_verdicts() -> list[str]:
    """The waiting itself, on a clock that does not tick in real time.

    One service answering on the third attempt and one never answering at all,
    so that both a budget honoured and a budget spent are watched happening.
    """
    problems = []
    ticks = iter(range(4000))
    replies = {"slow": [False, False, True], "dead": [False] * 200}

    def fake_ask(probe, _elapsed):
        answers = replies[probe["id"]]
        return (answers.pop(0) if answers else False), "pretend"

    waited = wait_for_all(
        [
            {"id": "slow", "kind": "http", "timeout_s": 10, "port": 1, "path": "/", "settle_s": 0},
            {"id": "dead", "kind": "http", "timeout_s": 3, "port": 2, "path": "/", "settle_s": 0},
        ],
        fake_ask,
        lambda: next(ticks),
        lambda _: None,
    )
    by_id = {answer["id"]: answer for answer in waited}
    if by_id["slow"].get("late"):
        problems.append("a service that answered inside its budget was recorded late")
    if not by_id["dead"].get("late"):
        problems.append("a service that never answered was not recorded late")

    return problems


def register_verdicts() -> list[str]:
    """The inverted register, driven both ways.

    A recorded defect that is still there says nothing; one that has been fixed
    is refused, which is what stops the register from outliving what it records.
    """
    problems = []
    broken = {"jellyfin"}
    down = [{"service": "jellyfin", "image": "jellyfin/jellyfin:10.10.3", "state": "restarting", "restarts": 4}]
    up = [{"service": "jellyfin", "image": "jellyfin/jellyfin:10.10.3", "state": "running", "restarts": 0}]

    if judge_known(broken, [{"id": "jellyfin", "late": True}], down):
        problems.append("a service still failing as recorded was refused")
    if not judge_known(broken, [{"id": "jellyfin"}], up):
        problems.append("a recorded defect that is up and answering was not refused")

    # An entry nothing starts is an entry nothing can ever clear, and it would
    # sit in the register describing a service no run has looked at since.
    profile_of = {service["id"]: service["profile"] for service in manifest()["service"]}
    for name in KNOWN_BROKEN:
        if name not in profile_of:
            problems.append(f"{name} is recorded broken and the manifest has no such service")
        elif profile_of[name] in UNRUNNABLE:
            problems.append(f"{name} is recorded broken in {profile_of[name]}, which nothing here starts")

    return problems


def plan_verdicts() -> list[str]:
    """Every profile the manifest declares is either started or excused, by name.

    This is what stops a profile added later from quietly going uncovered: the
    plan would simply not mention it, and nothing would say so.
    """
    problems = []

    if unrunnable_reason("torrent") is None:
        problems.append("the profile needing a VPN subscription is not recorded as unrunnable")
    if unrunnable_reason("search") is not None:
        problems.append("a profile that starts on any machine was recorded as unrunnable")

    data = manifest()
    planned = set(runnable_profiles(data))
    unaccounted = {p["id"] for p in data["profile"]} - planned - set(UNRUNNABLE)
    if unaccounted:
        problems.append(f"profiles neither planned nor excused: {sorted(unaccounted)}")

    groups = plan(data)
    if set(groups[-1]["profiles"].split(",")) != planned:
        problems.append("the group that starts everything runnable does not hold every runnable profile")
    if {group["profiles"] for group in groups[:-1]} != planned:
        problems.append("some runnable profile is never started on its own")

    return problems


def self_test() -> int:
    """Each verdict, driven against a stack that was never started.

    Every one of them needs Docker, a registry and several minutes to reach,
    which is exactly why none of them would otherwise have been watched fail.
    """
    problems = [
        *roster_verdicts(),
        *probe_verdicts(),
        *waiting_verdicts(),
        # The discrimination the whole check rests on, driven against two
        # sockets rather than two containers. A published port whose container
        # is doing nothing accepts and hangs up; one with a server behind it
        # accepts and waits. Get this wrong and every service passes forever.
        *probe_discriminates(),
        *register_verdicts(),
        *plan_verdicts(),
    ]

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA check that cannot tell a running stack from a resolved one is not a check.")
        return 1

    print(
        "self-test: every verdict was driven — a roster that is wrong five ways, a probe that timed "
        "out, a teardown that did not, a published port with nothing behind it — and no sound case "
        "was refused"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        help="a profile to start, or several separated by commas; repeatable",
    )
    parser.add_argument("--plan", action="store_true", help="print the groups this can start, as JSON")
    parser.add_argument("--self-test", action="store_true", help="prove the verdicts, without Docker")
    arguments = parser.parse_args()

    if arguments.self_test:
        return self_test()

    if arguments.plan:
        print(json.dumps(plan(manifest())))
        return 0

    if not arguments.profile:
        parser.error("name at least one --profile, or --plan to see which there are")

    named = [profile for argument in arguments.profile for profile in argument.split(",") if profile]
    return run(named)


if __name__ == "__main__":
    sys.exit(main())
