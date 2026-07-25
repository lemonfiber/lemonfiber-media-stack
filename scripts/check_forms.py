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
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OVERLAYS = ("stacks/compose.storage.nas.yml",)

PLACEHOLDER_ENV = {
    "DATA_ROOT": "/__lemonfiber_data_root__",
    "VPN_PROVIDER": "validation-placeholder",
    "WIREGUARD_PRIVATE_KEY": "validation-placeholder",
    "NAS_HOST": "validation-placeholder",
    "NAS_EXPORT": "/validation-placeholder",
}


def resolve(profiles: list[str], files: list[str]) -> tuple[bool, str, dict]:
    command = ["docker", "compose"]
    for path in files:
        command += ["-f", path]
    for profile in profiles:
        command += ["--profile", profile]
    command += ["config", "--format", "json"]

    result = subprocess.run(
        command,
        cwd=ROOT,
        # Placeholders win over the caller's environment: structural validity
        # must not depend on whose machine is running the check.
        env={**os.environ, **PLACEHOLDER_ENV},
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
        ok, message, model = resolve(profiles, ["compose.yml"])
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
        ok, message, model = resolve(["*"], ["compose.yml", overlay])
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
