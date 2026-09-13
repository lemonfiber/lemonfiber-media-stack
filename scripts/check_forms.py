#!/usr/bin/env python3
"""Resolve every form declared in stack.toml.

A form is a named set of profiles, and the promise the stack makes is that any
of them starts on its own — `docker compose --profile search up` must not need
`tv` to be running. This asserts each one is a structurally valid project, and
that it actually contains services.

The overlays are checked too: an overlay naming a service the base project does
not define is valid YAML and a broken stack.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OVERLAYS = ("stacks/compose.storage.nas.yml",)

# Forms resolve with a data root and nothing else. This is the point of the
# check, not an oversight: Compose interpolates the whole project before it
# filters by profile, so a `${VAR:?}` guard on any one service silently becomes a
# precondition for every form. Supplying placeholders here would hide exactly the
# coupling this is meant to catch — someone running `library` being asked for VPN
# credentials.
FORM_ENV = {"DATA_ROOT": "/__lemonfiber_data_root__"}

# An overlay is applied deliberately, by name, so it may demand its own settings.
OVERLAY_ENV = {
    **FORM_ENV,
    "NAS_HOST": "validation-placeholder",
    "NAS_EXPORT": "/validation-placeholder",
}


def resolve(profiles: list[str], files: list[str], settings: dict) -> tuple[bool, str, dict]:
    with tempfile.TemporaryDirectory() as tmp:
        # An empty --env-file, because Compose reads ./.env automatically and a
        # developer's local one would supply the very variables this is checking
        # nobody is forced to supply.
        empty = pathlib.Path(tmp) / "empty.env"
        empty.write_text("", encoding="utf-8")

        command = ["docker", "compose", "--env-file", str(empty)]
        for path in files:
            command += ["-f", path]
        for profile in profiles:
            command += ["--profile", profile]
        command += ["config", "--format", "json"]

        result = subprocess.run(
            command,
            cwd=ROOT,
            # A deliberately bare environment rather than the caller's: what
            # resolves must not depend on whose machine is running the check.
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "DOCKER_HOST": os.environ.get("DOCKER_HOST", ""),
                **settings,
            },
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        return False, result.stderr.strip(), {}
    return True, "", json.loads(result.stdout)


def judge(
    form: str,
    profiles: list[str],
    ok: bool,
    message: str,
    model: dict,
    profile_of: dict[str, str],
) -> str | None:
    """What is wrong with one resolved form, where anything is.

    Apart from the call that resolves it, so the three verdicts can be driven
    without Compose. Each is a different failure: a project that does not build,
    one that builds and starts nothing, and one that builds and starts more than
    the form asked for.
    """
    if not ok:
        return f"form {form}: does not resolve:\n{message}"

    started = set(model.get("services", {}))

    if not started:
        return f"form {form}: activates {profiles} but starts no services"

    # Nothing outside the form's profiles may be dragged in — that would mean a
    # dependency crossed a boundary and the subset is not really a subset.
    strays = {s for s in started if profile_of.get(s) not in profiles}

    if strays:
        return f"form {form}: pulls in {sorted(strays)}, outside its profiles {profiles}"

    return None


def self_test() -> int:
    """Each verdict `judge` gives, driven against a project it did not resolve.

    Every one of them needs Compose and a manifest to reach, which is why none of
    them had ever been driven — and the last is the one worth the most: a stray
    is how a form stops being a subset, and it is the failure that looks like a
    working stack right up until somebody starts one form on its own.
    """
    profile_of = {"sonarr": "tv", "radarr": "film", "gluetun": "vpn"}
    started = {"services": {"sonarr": {}}}

    cases = (
        ("a project that does not build", ["tv"], False, "no such profile", {}, "does not resolve"),
        ("a form that starts nothing", ["tv"], True, "", {"services": {}}, "starts no services"),
        ("a form pulling in another profile", ["tv"], True, "",
         {"services": {"sonarr": {}, "gluetun": {}}}, "pulls in ['gluetun']"),
        ("a service no profile claims", ["tv"], True, "",
         {"services": {"sonarr": {}, "orphan": {}}}, "pulls in ['orphan']"),
    )

    problems = []

    for said, profiles, ok, message, model, because in cases:
        verdict = judge("x", profiles, ok, message, model, profile_of)
        if verdict is None:
            problems.append(f"{said}: was judged sound")
        elif because not in verdict:
            problems.append(f"{said}: said {verdict!r}, which does not mention {because!r}")

    # The other side. A rule refusing everything is as useless as one refusing
    # nothing, and only this says which of the two this is.
    if judge("x", ["tv"], True, "", started, profile_of) is not None:
        problems.append("a form starting exactly its own profile was refused")

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA check that cannot tell a subset from a stack is not a check.")
        return 1
    print(f"self-test: all {len(cases)} broken forms were named, and the sound one was not")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="prove the verdicts, without Compose")

    if parser.parse_args().self_test:
        return self_test()

    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    profile_of = {s["id"]: s["profile"] for s in manifest["service"]}
    errors: list[str] = []

    for form in manifest["form"]:
        profiles = form["profiles"]
        ok, message, model = resolve(profiles, ["compose.yml"], FORM_ENV)
        wrong = judge(form["id"], profiles, ok, message, model, profile_of)

        if wrong is not None:
            errors.append(wrong)
            continue

        started = set(model.get("services", {}))
        print(f"  ok   form {form['id']:<8} {len(started):>2} services  {','.join(profiles)}")

    for overlay in OVERLAYS:
        ok, message, model = resolve(["*"], ["compose.yml", overlay], OVERLAY_ENV)
        if not ok:
            errors.append(f"overlay {overlay}: does not resolve:\n{message}")
        else:
            print(f"  ok   overlay {overlay}")

    if errors:
        print()
        print("\n".join(f"::error::{error}" for error in errors))
        return 1
    print(f"\nall {len(manifest['form'])} forms and {len(OVERLAYS)} overlay(s) resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
