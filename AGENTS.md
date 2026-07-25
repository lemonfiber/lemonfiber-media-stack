# AGENTS.md — media-stack

Guidance for any AI agent working in this repo.

> **Common rules for every lemonfiber repo are canonical in the spec:**
> [50-governance/ai-contributors.md](https://github.com/lemonfiber/spec/blob/main/50-governance/ai-contributors.md).
> Read them. This file is the `media-stack`-specific header only.

## What this repo is

The Docker Compose stack — 19 services — plus `stack.toml`, the manifest
lemonfiber consumes. Spec:
[`30-repos/media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/media-stack.md)
and the
[manifest contract](https://github.com/lemonfiber/spec/blob/main/20-architecture/contracts/stack-manifest.md).

## Layout

`compose.yml` defines no services. It `include:`s one fragment per profile from
`compose/`, and each entry carries `project_directory: .` so that relative paths
in a fragment resolve from the repo root rather than from `compose/`. Omitting it
breaks the fragment's `extends:` path and Compose refuses to build a model —
loudly, which is the intent.

Shared defaults live in `compose/_common.yml` as two template services reached
through `extends:`. That file is deliberately **not** in the include list, so the
templates never become containers:

- `defaults` — `restart` and `TZ`. Everything gets these.
- `rootless` — adds `PUID`/`PGID`, for images that document them. An image that
  ignores them gets `defaults` and a `user:` pair instead; setting PUID on an
  image that ignores it is a silent no-op that reads like a security control.

## The rules you cannot break

- **One `${DATA_ROOT}:/data` mount per service.** Never split downloads and media
  into separate mounts — it breaks hardlinks (ADR-0006). CI rejects it.
- **One profile per service** (`B1-R1`); **no `depends_on` across profiles**
  except `qbittorrent → gluetun`, which share the `torrent` profile (`B1-R14`).
- **Pinned tags, never `latest`** (`E1-R1`). A pin that does not resolve on both
  `linux/amd64` and `linux/arm64` fails CI (`F2-R6`).
- **`bind` matches the manifest tier** — admin services `loopback`, household
  services `lan` (`C6`). Only Gluetun holds `NET_ADMIN`, and anything sharing its
  profile must use `network_mode: service:gluetun` (`C2-R12`).
- `stack.toml` and the Compose model must stay in parity — every service in one
  is in the other, with the same image, tag and profile.

## Adding a service

A service block in the matching `compose/<profile>.yml` + a `[[service]]` in
`stack.toml` + add its profile to the relevant forms. No code (`REPO-R23`).
Then `just ci`.

If the service is the first of a new profile, add a `compose/<profile>.yml` and
an `include:` entry — with `project_directory: .`.

## Checks

```
just ci        # parity + every form + the validator's own tests
just images    # arm64/amd64 for every pin (needs the network)
```

`scripts/validate_manifest.py` reads `docker compose config`'s **resolved**
model, not the YAML, so it checks what Docker will run rather than what the file
appears to say. Every rule it enforces has a negative test in
`scripts/test_validate_manifest.py` — if you add a rule, add the test that proves
it fails when broken.

## Before you open a PR

- `just ci` is clean, and `just images` if you touched a pin.
- Cite a spec identifier in a commit `Spec:` trailer and the PR body.
- No AI attribution in commits.
