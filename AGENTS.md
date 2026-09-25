# AGENTS.md — lemonfiber-media-stack

Guidance for any AI agent working in this repo.

> **Common rules for every lemonfiber repo are canonical in the spec:**
> [50-governance/ai-contributors.md](https://github.com/lemonfiber/spec/blob/main/50-governance/ai-contributors.md).
> Read them. This file is the `lemonfiber-media-stack`-specific header only.

## What this repo is

The Docker Compose stack — 20 services — plus `stack.toml`, the manifest
lemonfiber consumes. Spec:
[`30-repos/lemonfiber-media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/lemonfiber-media-stack.md)
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
- **Pinned by digest, never by tag alone** (`E1-R1`). `digest` is the
  multi-architecture index's, beside a non-floating `tag`, and the compose
  fragment pulls `image:tag@digest`. A digest that is not an index publishing
  `linux/amd64` and `linux/arm64` fails CI (`F2-R6`).
- **`bind` matches the manifest tier** — admin services `loopback`, household
  services `lan` (`C6`). Only Gluetun holds `NET_ADMIN`, and anything sharing its
  profile must use `network_mode: service:gluetun` — a download client outside the
  tunnel's namespace is one lemonfiber reports as leaking (`C2-R12`).
- `stack.toml` and the Compose model must stay in parity — every service in one
  is in the other, with the same image, tag, digest and profile.

## Adding a service

A service block in the matching `compose/<profile>.yml` + a `[[service]]` in
`stack.toml` + add its profile to the relevant forms. No code (`REPO-R23`).
Then `just ci`.

The `[[service]]` also says what the service can do — `provides`, in lemonfiber's
published capability vocabulary — so that wiring can ask for a capability rather
than name a service. That file is generated from this field, so a capability
nothing here declares cannot be published. Recyclarr, Unpackerr, Homepage and
Caddy declare nothing, because nothing asks them anything: the first two act on
the filesystem and on other services' configuration, and the other two are
configured by lemonfiber writing a file.

The `[[service]]` says what the service reaches on the network and what it asks
for there — `reaches` and `asks_for`, both or neither, `reaches = ""` for one
that talks to nothing (`F2-R10`). That prose used to be a table compiled into
lemonfiber, which meant a new service needed a lemonfiber release before anything
could describe it; carrying it here is what makes `F1-R5` true.

A service something reaches, or that reaches something, also needs a
`[[wiring]]`: `by` is where the link runs from, and then either `asks` (a
capability, `each = true` where it reaches every filler rather than the one) or
`to` with a `why` (by name, which is the exception and has to say it is one).
A `depends_on` is a by-name wiring and CI refuses one that no `[[wiring]]`
shows as such.

If the service is the first of a new profile, add a `compose/<profile>.yml` and
an `include:` entry — with `project_directory: .`.

Before any of that, establish that the candidate is maintained — from its commit
and release history, never from its own description of itself (`F2-R15`). `just
candidate <url> --image <ref>` reads the history and says `admit`, `watch` or
`reject`; its answer belongs in the pull request. The full admission criteria are
in [README.md](README.md#adding-a-service).

## Bumping a pin, and removing a service

Both are changes only a diff can judge, and `scripts/check_manifest_change.py`
judges them against the pull request's base:

- A moved `tag` (or `image`) has to carry a refreshed `last_release`, or a
  `Pin-reviewed: <service-id>` trailer on the commit that moves it (`F2-R14`),
  and a new `digest` — the index the new tag names, which `just images` asks
  the registry for (`E1-R1`). `just pins-apply <service>` writes all three.
  A moved pin also has its upstream licence read from the forge and held to the
  OSI list (`F2-R12`) — that half is networked and lives in `just licences`.
- A service that leaves the manifest has to gain a `[[removed]]` entry naming
  the reason and any replacement (`F2-R13`).

## Checks

```
just ci        # parity + every form + what the diff says + the validator's own tests
just runs <profiles>   # start them for real and make them answer (needs Docker)
just images    # each digest an arm64/amd64 index, moved pins their tag's (needs the network)
just pins      # what the weekly `pins` workflow would move (needs the network)
just licences  # what each upstream licences itself as now (needs the network)
just candidate <url>   # judge a candidate on its history (needs the network)
```

`scripts/check_runs.py` is the one that starts something. Everything else here
reads the project; that brings each profile up with plain `docker compose` on a
path holding no lemonfiber binary, makes every service answer the probe
`stack.toml` declares for it, and takes it down again. A profile no machine
without an account can start — `torrent`, which needs a real VPN subscription —
is named in that script with the reason, and left out of what CI fans over.

`KNOWN_BROKEN` in the same script is the other register, and it points the other
way. It is empty now; it held Jellyfin, Seerr and Bindery, which could not start
from a clean clone on Linux because Docker creates a missing bind-mount source
as `root:root` and a container running as a fixed non-root user cannot write its
own `/config`. An entry there reports rather than fails — and **fails as soon as
that service works**, which is what proved those three fixed. Nothing goes in
there to make a run pass.

The cure is worth knowing before adding a service: a config directory shipped in
the repo belongs to whoever cloned it, and every service that drops privileges
is started as `${PUID}:${PGID}`, so an image with a non-root user baked in gets
a `user:` pair rather than the uid its author chose.

`scripts/validate_manifest.py` reads `docker compose config`'s **resolved**
model, not the YAML, so it checks what Docker will run rather than what the file
appears to say. Every rule it enforces has a negative test in
`scripts/test_validate_manifest.py` — if you add a rule, add the test that proves
it fails when broken.

## Before you open a PR

- `just ci` is clean, and `just images` if you touched a pin.
- Cite a spec identifier in a commit `Spec:` trailer and the PR body.
- No AI attribution in commits.
