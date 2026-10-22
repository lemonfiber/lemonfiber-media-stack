"""The project lemonfiber writes from this stack, which is what runs.

Nothing here writes a Compose file. The lemonfiber binary does: `lemonfiber reset
--confirm`, with a configuration directory of its own holding the operator's values,
writes the project the way it writes it on an operator's machine. Over a copy of this
stack it writes this stack's own project; with no stack directory it writes the stack
the binary carries, which is this stack with the bundle's assembled parts laid over it
when the binary was built from this tree. This reads back what it wrote, as Compose
resolves it. `LEMONFIBER` names the binary where it is not on the path.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The binary that writes the project.
LEMONFIBER = os.environ.get("LEMONFIBER", "lemonfiber")
# What of this stack lemonfiber reads to write the project.
CARRIED = ("stack.toml", "services", "config", ".env.example")
# The project lemonfiber writes, beneath the stack directory.
FILE = "compose.generated.yml"
# Where lemonfiber keeps its configuration beneath the directory it is given.
CONFIGURATION = "lemonfiber"
# The operator's environment file there.
OPERATOR_FILE = ".env"
# The setting lemonfiber names the project by.
PROJECT_KEY = "LEMONFIBER_PROJECT"
# The key a service's secrets files are named under in the project.
ENV_FILE = "env_file:"
# What separates the parts of a network's name in the project.
SEPARATOR = "__"
# The network a service's ask of a capability is joined on, by its parts.
ASK = "ask"


def operator(data_root: pathlib.Path, project: str) -> dict[str, str]:
    """The operator's values `.env.example` asks for, as whoever runs this: the data root,
    the uid pair, and the project's name."""
    return {
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
        "DATA_ROOT": str(data_root),
        PROJECT_KEY: project,
    }


def written(scratch: pathlib.Path, values: dict[str, str], *, carried: bool = True) -> pathlib.Path:
    """The directory of the project lemonfiber writes beneath `scratch`, with the operator's
    environment file holding `values`: this stack's own project where `carried`, and the
    project of the stack the binary carries otherwise."""
    config = scratch / "configuration"
    (config / CONFIGURATION).mkdir(parents=True, exist_ok=True)
    (config / CONFIGURATION / OPERATOR_FILE).write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    data = scratch / "lemonfiber"
    command = [LEMONFIBER, "reset", "--confirm", "--config-dir", str(config), "--data-dir", str(data)]
    if carried:
        stack = scratch / "stack"
        for name in tracked():
            (stack / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, stack / name)
        command += ["--stack-dir", str(stack)]
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as missing:
        raise RuntimeError(f"{LEMONFIBER} is not on PATH; set LEMONFIBER to the binary that writes the project") \
            from missing
    found = sorted(path.parent for path in (scratch / "stack" if carried else data).rglob(FILE))
    if done.returncode != 0 or len(found) != 1:
        raise RuntimeError(f"lemonfiber wrote {len(found)} projects: {(done.stderr or done.stdout).strip()[-400:]}")
    filed(found[0] / FILE, config)
    return found[0]


def filed(project: pathlib.Path, config: pathlib.Path) -> None:
    """Each secrets file the project names beneath `config` that a start, not `reset`,
    writes: made empty and private where it is not there, holding no secret."""
    named, listing = [], False
    for line in project.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if listing and entry.startswith("- "):
            named.append(pathlib.Path(entry.removeprefix("- ")))
        else:
            listing = entry == ENV_FILE
    for path in named:
        if path.is_relative_to(config) and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600)


def tracked() -> list[str]:
    """Every file of this stack lemonfiber reads that the repository tracks, as a clone
    holds it: nothing a service wrote into its configuration while it ran."""
    done = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", *CARRIED],
                          capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"the stack's files could not be listed: {done.stderr.strip()[-400:]}")
    return [name for name in done.stdout.split("\0") if name]


def compose(project: pathlib.Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Plain `docker compose` in the project's directory, reading the project and the
    `.env` beside it as an operator's does, and nothing lemonfiber passes."""
    return subprocess.run(["docker", "compose", *args], cwd=project, env=env if env is not None else os.environ,
                          capture_output=True, text=True, check=False)


def model(project: pathlib.Path) -> dict:
    """The project, every profile active, as Compose resolves it, the secret files it
    names left unread."""
    done = compose(project, "config", "--no-env-resolution", "--format", "json",
                   env={**os.environ, "COMPOSE_PROFILES": "*"})
    if done.returncode != 0:
        raise RuntimeError(f"`docker compose config` failed over the project: {done.stderr.strip()[-400:]}")
    return json.loads(done.stdout)


def asked(networks: dict, asker: str, capability: str) -> str:
    """The network of `asker`'s ask of `capability`, by the name the project declares it."""
    prefix = SEPARATOR.join([ASK, asker, capability, ""])
    found = [name for name in networks if name.startswith(prefix)]
    if len(found) != 1:
        raise RuntimeError(f"the project holds {len(found)} networks for {asker}'s ask of {capability}")
    return found[0]


def fixed_address(service: dict, network: str) -> str | None:
    """The address a resolved service holds on one network, where it holds a fixed one."""
    attached = (service.get("networks") or {}).get(network) or {}
    return attached.get("ipv4_address")
