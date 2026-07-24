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

## The rules you cannot break

- **One `${DATA_ROOT}:/data` mount per service.** Never split downloads and media
  into separate mounts — it breaks hardlinks (ADR-0006). CI rejects it.
- **One profile per service** (`B1-R1`); **no `depends_on` across profiles**
  except `qbittorrent → gluetun`, which share the `torrent` profile (`B1-R14`).
- **Pinned tags, never `latest`** (`E1-R1`).
- **`bind` matches the manifest tier** — admin services `loopback`, household
  services `lan` (`C6`). Only Gluetun holds `NET_ADMIN`.
- `stack.toml` and `compose.yml` must stay in parity — every service in one is in
  the other. `scripts/validate_manifest.py` and `docker compose config` check
  this in CI.

## Adding a service

`compose.yml` block (one profile) + `stack.toml` `[[service]]` + add its profile
to the relevant forms. No code. Then `python3 scripts/validate_manifest.py`.

## Before you open a PR

- `docker compose config --quiet` passes and `validate_manifest.py` is clean.
- Cite a spec identifier in a commit `Spec:` trailer and the PR body.
- No AI attribution in commits.
