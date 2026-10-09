# AGENTS.md — lemonfiber-media-stack

> **Start at the roadmap and board on [lemonfiber.app](https://lemonfiber.app),
> rendered from the report of where every unreleased version stands. Then the
> rules** every repository shares:
> [working in the repositories](https://github.com/lemonfiber/spec/blob/main/50-governance/working-in-the-repositories.md)
> and [the rules for agents](https://github.com/lemonfiber/spec/blob/main/50-governance/ai-contributors.md).
> This file holds only what is true of this repository.

## What this repo is

The Docker Compose stack — 23 services — plus the manifest lemonfiber consumes:
`stack.toml` and a file per service in `services/`, named by its `include`. Spec:
[`30-repos/lemonfiber-media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/lemonfiber-media-stack.md)
and the
[manifest contract](https://github.com/lemonfiber/spec/blob/main/20-architecture/contracts/stack-manifest.md).

## Layout

`compose.yml` defines no services. It `include:`s one fragment per profile from
`compose/`, each with `project_directory: .` so relative paths resolve from the
repo root; without it the fragment's `extends:` path breaks and Compose refuses.

Shared defaults live in `compose/_common.yml`, which is **not** in the include
list, as three templates reached through `extends:`:

- `defaults` — `restart` and `TZ`. Everything gets these.
- `rootless` — adds `PUID`/`PGID`, for images that document them; an image that
  ignores them gets `defaults` and a `user:` pair instead.
- `confined` — for lemonfiber's own images: a read-only root, the operator's
  uid, every capability dropped, `no-new-privileges` and a memory limit.

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
- The manifest and the Compose model must stay in parity — every service in one
  is in the other, with the same image, tag, digest and profile.
- **lemonfiber's own images run confined** (`C6-R20`, `C6-R22`). Each extends
  `confined`, mounts `./config/<id>:/config` alone, and is on exactly the
  networks its ADR names, as `CONFINED` in `scripts/compose_parity.py` states.
  Seerr is not on the default network: the request gate is its only path to
  Sonarr, Radarr and Jellyfin.

## Adding a service

First, `just candidate <url> --image <ref>` judges from its commit and release
history whether the candidate is maintained (`F2-R15`) and says `admit`, `watch`
or `reject`, which goes in the pull request; the criteria are in [README.md](README.md#adding-a-service).

Then a service block in the matching `compose/<profile>.yml` (a new profile adds
its own file and `include:` entry, with `project_directory: .`), its `[[service]]`
in `services/<id>.toml`, named in `stack.toml`'s `include`, and its profile in the
relevant forms. No code (`REPO-R23`).

The `[[service]]` also says what the service can do — `provides`, in lemonfiber's
published capability vocabulary — so wiring can ask for a capability rather than
name a service. A service nothing asks anything (Recyclarr, Unpackerr, Homepage,
Caddy, the door, the request gate, the decline service) declares nothing.

It says what the service reaches on the network and what it asks for there —
`reaches` and `asks_for`, both or neither, `reaches = ""` for one that talks to
nothing (`F2-R10`).

A service something reaches, or that reaches something, also needs a
`[[wiring]]`: `by` where the link runs from, then `asks` (a capability, `each =
true` to reach every filler) or `to` with a `why` (by name, the exception). CI
refuses a `depends_on` no `[[wiring]]` shows.

## Bumping a pin, and removing a service

`scripts/check_manifest_change.py` judges both against the pull request's base:

- A moved `tag` (or `image`) carries a refreshed `last_release` or a
  `Pin-reviewed: <service-id>` trailer (`F2-R14`), and the new tag's index
  `digest` (`E1-R1`); `just pins-apply <service>` writes all three. Its upstream
  licence is held to the OSI list (`F2-R12`), or to `Hippocratic-3.0` for
  lemonfiber's own images (`F2-R5`), by `just licences`.
- A service that leaves the manifest has to gain a `[[removed]]` entry naming
  the reason and any replacement (`F2-R13`).

A moved `digest` stales every recording the service's claims name, and
`validate_manifest.py` refuses each one (`ARCH-R136`). The `pins` workflow
re-records them in the change that moves the pin; by hand, `just record
<vocabulary> <service>`, with the vocabulary the lemonfiber commit in
`.github/lemonfiber-judge` publishes.

## Checks

```
just ci        # parity + every form + what the diff says + the validator's own tests
just runs <profiles>   # start them for real and make them answer (needs Docker)
just images    # each digest an arm64/amd64 index, moved pins their tag's (needs the network)
just pins      # what the weekly `pins` workflow would move (needs the network)
just licences  # what each upstream licences itself as now (needs the network)
just candidate <url>   # judge a candidate on its history (needs the network)
just record <vocabulary> <services>   # re-record what services answer their claimed probes (needs Docker)
```

`scripts/check_runs.py` brings each profile up with plain `docker compose`, makes
every service answer the probe its service file declares, and takes it down again;
`torrent`, which needs a real VPN subscription, is left out of what CI fans over.
Its `KNOWN_BROKEN` register reports an entry rather than failing, until it works.

`scripts/validate_manifest.py` reads `docker compose config`'s **resolved**
model; each rule it enforces has a negative test beside it.

## Before you open a PR

- `just ci` is clean, and `just images` if you touched a pin.
