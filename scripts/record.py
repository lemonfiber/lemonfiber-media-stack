#!/usr/bin/env python3
"""Record what each bundled service answers to the probes it claims.

A `[[service.claim]]` in stack.toml binds every probe of a capability to a
request on the service and to a recording of the answer, taken from the image
the manifest pins (`ARCH-R136`). This takes those recordings.

For each service named it starts the pinned `image@digest` fresh, its
configuration on a volume of its own holding nothing but the templates this
repository ships, under a container name and a port nothing else uses. It does
whatever first run the service needs before it will answer an operator, with
credentials generated for that run and thrown away with it, asks each bound
probe, and writes the answer to the fixture the claim names. The container and
its volume are removed whether or not that worked, and nothing it wrote is ever
on the host's disk.

A credential this run makes is never on a command line, where any process can
read it, and never in what this prints: a value the container is started with
reaches Docker through the environment, and a failure is reported with every
credential the run made taken out of it.

A probe the published vocabulary says is asked with the operator's credential is
asked with the one this run made; every other probe presents nothing. The
recording keeps the request the claim declares and never the credential: a
claim cannot present one, and a recording is committed to a public repository.

What comes back is scrubbed before it is written. Every credential the run
made — as it was made, in either case, and URL-, base64- or JSON-encoded — the
container's id, hostname and addresses, and the recording machine's own name are
replaced with a placeholder wherever they appear, and so is each place a recipe
names as describing the machine or the instance rather than the service. Then
the recording is read once more as the text it will be written as, and if any
credential the run made is still in it, in any of those forms, it is not
written. Read each one anyway before committing it.

Needs Docker and the network, and the vocabulary lemonfiber publishes, read on
standard input:

    python3 scripts/record.py sonarr radarr < capability-vocabulary.json

What each service needs for its first run, and the note each of its recordings
carries, is its recipe in record_recipes.py. The container a run starts, and how
it is asked, is record_run.py.

`--self-test` proves the scrubbing and the reading of an answer, offline.
Exit 0 = every recording written, 1 = one was not.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tomllib
import urllib.parse

from record_recipes import RECIPES, TZ, anonymous
from record_run import HOST, JSON_TYPE, Answer, Credential, Recipe, Run
from registry import by_digest

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACK_TOML = "stack.toml"

# What a recording keeps of a body that is not JSON. Enough to say what it is —
# an HTML login page, an XML document — and too little to carry a page.
BODY_STARTS_WITH = 64

# The response headers an expectation can constrain.
KEPT_HEADERS = ("content-type",)

# What every scrubbed value is replaced with.
REDACTED = "<redacted>"
# The shortest value scrubbing looks for. Below this a value is a word that
# occurs in answers by chance, and every credential a run makes is longer.
SHORTEST_HIDDEN = 4


# ── recording ───────────────────────────────────────────────────────────────


def forms(value: str) -> set[str]:
    """Every spelling of `value` an answer could echo it back in.

    As it was made, URL-encoded either way, base64-encoded with and without
    padding and in the URL-safe alphabet, and escaped as a JSON string would
    carry it. Case is not spelled out here: every comparison ignores it.
    """
    if len(value) < SHORTEST_HIDDEN:
        return set()
    encoded = base64.b64encode(value.encode()).decode()
    url_safe = base64.urlsafe_b64encode(value.encode()).decode()
    return {
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
        encoded, encoded.rstrip("="),
        url_safe, url_safe.rstrip("="),
        json.dumps(value)[1:-1],
    }


def hiding(hidden: set[str]) -> re.Pattern[str] | None:
    """One pattern matching every form of every hidden value, longest first."""
    every = sorted({one for value in hidden for one in forms(value)}, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, every)), re.IGNORECASE) if every else None


def scrubbed(value: object, hidden: set[str]) -> object:
    """`value` with every hidden string, in every form and either case, replaced
    wherever it appears: in a string at any depth, and in a key."""
    pattern = hiding(hidden)
    return value if pattern is None else scrubbed_by(value, pattern)


def scrubbed_by(value: object, pattern: re.Pattern[str]) -> object:
    if isinstance(value, str):
        return REDACTED if pattern.fullmatch(value) else pattern.sub(REDACTED, value)
    if isinstance(value, list):
        return [scrubbed_by(item, pattern) for item in value]
    if isinstance(value, dict):
        return {scrubbed_by(key, pattern): scrubbed_by(item, pattern) for key, item in value.items()}
    return value


def still_carries(text: str, made: set[str]) -> bool:
    """Whether `text` holds any credential this run made, in any form or case."""
    pattern = hiding(made)
    return pattern is not None and pattern.search(text) is not None


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


def redacted_named(document: object, names: tuple[str, ...]) -> object:
    """`document` with every member called one of `names` replaced, at any depth."""
    if isinstance(document, list):
        return [redacted_named(item, names) for item in document]
    if isinstance(document, dict):
        return {key: REDACTED if key in names else redacted_named(item, names) for key, item in document.items()}
    return document


def response_of(answer: Answer, hidden: set[str], places: tuple[str, ...] = (),
                names: tuple[str, ...] = ()) -> dict:
    """An answer in the terms a recording keeps it: status, the headers an
    expectation reads, and the body as JSON or as the start of it."""
    kept: dict = {"status": answer.status}
    headers = {name: answer.headers[name] for name in KEPT_HEADERS if name in answer.headers}
    if headers:
        kept["headers"] = headers
    text = answer.body.decode("utf-8", errors="replace")
    try:
        kept["json"] = redacted_named(redacted(json.loads(text), places), names) if text.strip() else None
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


def created(run: Run, recipe: Recipe) -> tuple[list[str], dict[str, str]]:
    """The command that creates the container, and the environment it is run in.

    Each variable is named on the command line and valued in the environment,
    where Docker reads it from: a command line is readable by every process on
    the machine and is what a failed command reports, and a value this run
    minted belongs in neither.
    """
    variables = environment(run, recipe)
    command = [
        "docker", "create", "--name", run.name,
        "-p", f"{HOST}:{run.host_port}:{run.port}",
        # A volume of the container's own rather than a directory on the host,
        # removed with it: what the service writes there includes the keys this
        # run made, and nothing of that reaches the host's disk.
        "-v", recipe.config,
        *(item for name in variables for item in ("-e", name)),
        *recipe.run_args,
        run.reference,
    ]
    return command, {**os.environ, **variables}


def start(run: Run, recipe: Recipe) -> None:
    run.host_port = free_port()
    command, env = created(run, recipe)
    subprocess.run(command, capture_output=True, text=True, check=True, env=env)
    for template in recipe.templates:
        source = ROOT / template
        subprocess.run(["docker", "cp", str(source), f"{run.name}:{recipe.config}/{source.name}"],
                       capture_output=True, text=True, check=True)
    subprocess.run(["docker", "start", run.name], capture_output=True, text=True, check=True)


def environment(run: Run, recipe: Recipe) -> dict[str, str]:
    """A recipe's environment, with the keys this run minted written in."""
    minted = {"key": run.key, "wireguard": run.wireguard, "peer": run.peer}
    return {name: value.format(**minted) for name, value in recipe.env.items()}


def stop(run: Run) -> None:
    subprocess.run(["docker", "rm", "-f", "-v", run.name], capture_output=True, check=False)


def probed(run: Run, recipe: Recipe, capability: str, probe: dict, presented: Credential,
           hidden: set[str]) -> str:
    """Ask one bound probe, and write what came back to the fixture it names."""
    asked = probe["request"]
    headers = {"Accept": asked["accept"]} if "accept" in asked else {}
    answer = run.ask(asked["method"], asked["path"], headers={**headers, **presented.headers},
                     query=presented.query)
    recording = {
        "recorded_from": run.reference,
        "note": recipe.notes[(capability, probe["id"])],
        "request": {key: asked[key] for key in ("method", "path", "accept") if key in asked},
        "response": response_of(answer, hidden, recipe.redact, recipe.redact_named),
    }
    text = json.dumps(recording, indent=2, ensure_ascii=False) + "\n"
    if still_carries(text, run.secrets):
        raise RuntimeError(
            f"{probe['fixture']} still carries a credential this run made after scrubbing, "
            "so it was not written"
        )
    target = ROOT / probe["fixture"]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return f"{probe['fixture']}: {answer.status}"


def record(service: dict, recipe: Recipe, operator: set[tuple[str, str]], hold: bool) -> list[str]:
    sid = service["id"]
    reference = by_digest(service)
    port = recipe.port or service.get("listens") or service.get("port")
    run = Run(sid, reference, port, recipe.config)
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
            for probe in claim.get("probe", []):
                presented = credential if (claim["capability"], probe["id"]) in operator else Credential()
                written.append(probed(run, recipe, claim["capability"], probe, presented, hidden))
        if hold:
            print(f"held: {run.name} at {run.base}, until `docker rm -f -v {run.name}`")
    except Exception as broke:
        # Reported with every credential the run made taken out, and without
        # the exception it came from, whose arguments are what was not scrubbed.
        raise RuntimeError(str(scrubbed(str(broke), run.secrets))) from None
    finally:
        if not hold:
            stop(run)
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
    named = response_of(Answer(200, {}, b'{"Id":"a","Items":[{"Id":"b","UserData":{"Key":"c","n":1}}]}'), set(),
                        names=("Id", "Key"))
    assert named["json"] == {"Id": REDACTED, "Items": [{"Id": REDACTED, "UserData": {"Key": REDACTED, "n": 1}}]}
    vocabulary = {"capabilities": [{"name": "a.b", "probes": [
        {"id": "guarded", "credential": "none"}, {"id": "read", "credential": "operator"}]}]}
    assert operator_probes(vocabulary) == {("a.b", "read")}
    run = Run("thing", "example/thing@sha256:0", 80, "/config")
    recipe = Recipe(setup=anonymous, notes={}, env={"KEY": "{key}", "WG": "{wireguard}", "TZ": TZ})
    minted = environment(run, recipe)
    assert minted == {"KEY": run.key, "WG": run.wireguard, "TZ": TZ} and run.key in run.secrets, minted
    # A minted value reaches Docker through the environment, never the command line.
    command, env = created(run, recipe)
    assert not any(secret in argument for secret in run.secrets for argument in command), command
    assert "KEY" in command and env["KEY"] == run.key and env["WG"] == run.wireguard

    # Echoed back in another case, URL-encoded, base64-encoded or JSON-escaped,
    # at any depth or as a key, a credential is still scrubbed.
    secret = "S3cret/Key+1"
    made = {secret}
    echoed = {
        "upper": secret.upper(),
        "url": f"/x?k={urllib.parse.quote(secret, safe='')}",
        "plus": urllib.parse.quote_plus(secret),
        "b64": base64.b64encode(secret.encode()).decode(),
        "deep": [{secret: "value"}],
    }
    clean = json.dumps(scrubbed(echoed, made))
    assert not still_carries(clean, made) and REDACTED in clean, clean
    assert still_carries('{"a": "...s3cret%2Fkey%2B1..."}', made)
    assert not still_carries('{"a": "nothing here"}', made)
    assert forms("ab") == set(), "a value too short to look for is not looked for"

    # A failure is reported with what the run made taken out.
    try:
        raise RuntimeError(f"refused {run.key}")
    except RuntimeError as broke:
        assert run.key not in str(scrubbed(str(broke), run.secrets))
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
