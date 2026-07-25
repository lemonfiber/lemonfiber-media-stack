#!/usr/bin/env python3
"""Expand a form id into the `--profile` flags it stands for.

A form is a set of profiles, not one profile: `tv` means search, usenet, torrent,
tv and subs together. `docker compose --profile tv up` would start Sonarr
with no indexers and nowhere to download from — which is why this exists rather
than the form id being passed through.

    $ python3 scripts/form_profiles.py tv
    --profile search --profile usenet --profile torrent --profile tv --profile subs
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("form", nargs="?", help="form id, e.g. tv")
    parser.add_argument("--list", action="store_true", help="list declared forms")
    args = parser.parse_args()

    manifest = tomllib.loads((ROOT / "stack.toml").read_text(encoding="utf-8"))
    forms = manifest["form"]

    if args.list:
        for form in forms:
            print(f"{form['id']:<8} {form['description']}")
        return 0

    if not args.form:
        parser.error("a form id is required unless --list is given")

    match = next((f for f in forms if f["id"] == args.form), None)
    if match is None:
        known = ", ".join(f["id"] for f in forms)
        print(f"unknown form {args.form!r}; known forms: {known}", file=sys.stderr)
        return 2

    print(" ".join(f"--profile {profile}" for profile in match["profiles"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
