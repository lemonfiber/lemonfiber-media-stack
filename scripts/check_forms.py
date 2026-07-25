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


def main() -> int:
    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    profile_of = {s["id"]: s["profile"] for s in manifest["service"]}
    errors: list[str] = []

    for form in manifest["form"]:
        profiles = form["profiles"]
        ok, message, model = resolve(profiles, ["compose.yml"], FORM_ENV)
        if not ok:
            errors.append(f"form {form['id']}: does not resolve:\n{message}")
            continue

        started = set(model.get("services", {}))
        if not started:
            errors.append(f"form {form['id']}: activates {profiles} but starts no services")
            continue

        # Nothing outside the form's profiles may be dragged in — that would mean
        # a dependency crossed a boundary and the subset is not really a subset.
        strays = {s for s in started if profile_of.get(s) not in profiles}
        if strays:
            errors.append(
                f"form {form['id']}: pulls in {sorted(strays)}, outside its profiles {profiles}"
            )
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
