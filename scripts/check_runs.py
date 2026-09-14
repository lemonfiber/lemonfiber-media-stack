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

Three things are asserted, and each fails for a different reason:

  * the project that came up holds exactly the services the manifest puts in
    those profiles, each running the pinned image and none of them restarting;
  * every service answers its declared probe within its declared timeout, which
    a container that merely exists cannot do;
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

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Distinct from the project name a bare `docker compose up` would pick — the
# directory's — because `down --volumes` at the end of this would otherwise
# take an operator's own running stack with it on the machine where both live.
PROJECT = "lemonfiber-runs-check"

# The published ports are bound to 127.0.0.1 for the admin tier and to
# ${LAN_BIND} for the household one; the loopback address reaches both.
HOST = "127.0.0.1"

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
        "image": f"{service['image']}:{service['tag']}",
    }


def ask_http(port: int, path: str) -> tuple[bool, str]:
    """One request to a published port, and what came back.

    A status is what proves something served it, so a 404 or a 401 is a failure
    and not a pass: the path is the manifest's own, and a service answering
    something else about it means the contract is wrong even though the port is
    open.
    """
    url = f"http://{HOST}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=ATTEMPT_TIMEOUT_S) as reply:
            return reply.status < 400, f"HTTP {reply.status}"
    except urllib.error.HTTPError as answered:
        return False, f"HTTP {answered.code}"
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as unreachable:
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


def judge_teardown(remaining: list[str]) -> str | None:
    if remaining:
        return f"down left {sorted(remaining)} behind"
    return None


def unrunnable_reason(profile: str) -> str | None:
    return UNRUNNABLE.get(profile)


def bare_environment(data_root: str) -> dict[str, str]:
    """The environment Compose is given, and nothing else.

    The same discipline `check_forms.py` applies for the same reason: what the
    stack needs must not depend on whose machine is running it. `DATA_ROOT` is
    supplied because a bind mount needs a source; everything else the stack
    wants has a default in the Compose files, and a form that turns out to need
    more than this is one an operator would be stuck on too.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
        "DATA_ROOT": data_root,
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


def run(profiles: list[str]) -> int:
    data = manifest()
    declared = {p["id"] for p in data["profile"]}

    for profile in profiles:
        if profile not in declared:
            print(f"::error::no profile {profile!r} in stack.toml")
            return 2
        reason = unrunnable_reason(profile)
        if reason is not None:
            print(f"::error::profile {profile!r} cannot be started here: {reason}")
            return 2

    # The guarantee under test, asserted before anything is started rather than
    # assumed: a binary on the path is a binary that could have been involved.
    if shutil.which("lemonfiber", path=os.environ.get("PATH", "")) is not None:
        print("::error::a lemonfiber binary is on PATH; this proves nothing while it is")
        return 2

    chosen = services_in(data, profiles)
    if not chosen:
        print(f"::error::profiles {profiles} hold no services")
        return 1

    expected = {s["id"]: f"{s['image']}:{s['tag']}" for s in chosen}
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
            errors += judge_roster(expected, roster(env, empty))
            errors += [verdict for answer in answers if (verdict := judge_probe(answer)) is not None]

            for answer in sorted(answers, key=lambda a: a["id"]):
                mark = "late" if answer.get("late") else "ok  "
                budget = f"{answer['elapsed_s']}s of {answer['timeout_s']}s"
                print(f"  {mark} {answer['id']:<24} {answer['kind']:<9} {budget:<14} {answer['detail']}")

            if errors:
                report_failure(env, empty, sorted(answer["id"] for answer in answers if answer.get("late")))
        finally:
            removed = compose(env, empty, "down", "--volumes", "--remove-orphans")
            if removed.returncode != 0:
                errors.append(f"down failed:\n{removed.stderr.strip()}")

        left = judge_teardown([row["service"] for row in roster(env, empty)])
        if left is not None:
            errors.append(left)

    if errors:
        return report(errors)

    print(f"\n{len(chosen)} service(s) started, answered, and were torn down. No lemonfiber binary involved.")
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


def self_test() -> int:
    """Each verdict, driven against a stack that was never started.

    Every one of them needs Docker, a registry and several minutes to reach,
    which is exactly why none of them would otherwise have been watched fail.
    """
    problems = []

    pinned = {"sonarr": "lscr.io/linuxserver/sonarr:4.0.15", "bazarr": "lscr.io/linuxserver/bazarr:1.4.5"}

    def row(service, image, state="running", restarts=0):
        return {"service": service, "image": image, "state": state, "restarts": restarts}

    sound = [row("sonarr", pinned["sonarr"]), row("bazarr", pinned["bazarr"])]

    roster_cases = (
        ("a service that never came up", [sound[0]], "no container came up for it"),
        ("a service outside the profiles", [*sound, row("gluetun", "qmcgaw/gluetun:v3.40.0")], "outside these profiles"),
        ("a service that exited", [row("sonarr", pinned["sonarr"], state="exited"), sound[1]], "not running"),
        ("a service in a restart loop", [row("sonarr", pinned["sonarr"], restarts=3), sound[1]], "not staying up"),
        ("a service running another image", [row("sonarr", "alpine:3.20"), sound[1]], "the manifest pins"),
    )

    for said, observed, because in roster_cases:
        verdicts = judge_roster(pinned, observed)
        if not verdicts:
            problems.append(f"{said}: was judged sound")
        elif not any(because in verdict for verdict in verdicts):
            problems.append(f"{said}: said {verdicts!r}, which does not mention {because!r}")

    if judge_roster(pinned, sound):
        problems.append("a project holding exactly its own services, running and pinned, was refused")

    # The probe half. A service that answers late is the failure this whole
    # check exists for — a container that is up and serving nothing looks
    # identical to a working stack in `docker compose ps`.
    late = {"id": "sonarr", "kind": "http", "timeout_s": 90, "detail": "ConnectionRefusedError", "late": True}
    if judge_probe(late) is None:
        problems.append("a service that never answered was judged sound")
    if judge_probe({**late, "late": False}) is not None:
        problems.append("a service that answered was refused")

    if judge_teardown(["sonarr"]) is None:
        problems.append("a container left running after down was judged sound")
    if judge_teardown([]) is not None:
        problems.append("a clean teardown was refused")

    if unrunnable_reason("torrent") is None:
        problems.append("the profile needing a VPN subscription is not recorded as unrunnable")
    if unrunnable_reason("search") is not None:
        problems.append("a profile that starts on any machine was recorded as unrunnable")

    # The waiting itself, on a clock that does not tick in real time: one service
    # answering on the third attempt, one never answering at all.
    ticks = iter(range(0, 4000))
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

    # The discrimination the whole check rests on, driven against two sockets
    # rather than two containers. A published port whose container is doing
    # nothing accepts and hangs up; one with a server behind it accepts and
    # waits. Get this wrong and every service passes forever.
    problems += probe_discriminates()

    # A service with no endpoint is proved by surviving a dwell, and the dwell is
    # the only thing standing between "it was created" and "it is running": a
    # container that exits after ten seconds answers the first poll either way.
    dwelling = {"id": "recyclarr", "kind": "container", "settle_s": 30, "timeout_s": 40}
    if attempt(dwelling, 5)[0]:
        problems.append("a container-kind service was judged up before its dwell had passed")
    if not attempt(dwelling, 31)[0]:
        problems.append("a container-kind service still up after its dwell was refused")

    # A manifest profile must be either runnable or recorded as not, with a
    # reason. This is what stops a profile added later from quietly going
    # uncovered — the plan would simply not mention it.
    data = manifest()
    planned = set(runnable_profiles(data))
    unaccounted = {p["id"] for p in data["profile"]} - planned - set(UNRUNNABLE)
    if unaccounted:
        problems.append(f"profiles neither planned nor excused: {sorted(unaccounted)}")

    groups = plan(data)
    together = groups[-1]["profiles"].split(",")
    if set(together) != planned:
        problems.append("the group that starts everything runnable does not hold every runnable profile")
    if {group["profiles"] for group in groups[:-1]} != planned:
        problems.append("some runnable profile is never started on its own")

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA check that cannot tell a running stack from a resolved one is not a check.")
        return 1

    print(
        f"self-test: {len(roster_cases)} broken rosters were named, a published port with nothing "
        "behind it was told from a server waiting on one, and no sound case was refused"
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
