# AGENTS.md — lemonfiber-media-stack

> **Start at the roadmap and board on [lemonfiber.app](https://lemonfiber.app),
> rendered from the report of where every unreleased version stands. Then the
> rules** every repository shares:
> [working in the repositories](https://github.com/lemonfiber/spec/blob/main/50-governance/working-in-the-repositories.md)
> and [the rules for agents](https://github.com/lemonfiber/spec/blob/main/50-governance/ai-contributors.md).
> This file holds only what is true of this repository.

## What this repo is

The media stack's manifest — 23 services — that lemonfiber writes the Compose
project from: `stack.toml` and a file per service in `services/`, named by its
`include`. There is no Compose file here. Spec:
[`30-repos/lemonfiber-media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/lemonfiber-media-stack.md)
and the
[manifest contract](https://github.com/lemonfiber/spec/blob/main/20-architecture/contracts/stack-manifest.md).

## Layout

lemonfiber writes the project, `compose.generated.yml`, from the manifest: every
entry from its service file (ADR-0043), the networks from the asks and wirings,
and the operator's values written in. Nothing here is a Compose file, and the
checks that start the stack and the door run the project a lemonfiber binary
writes, which `scripts/generated.py` asks it for. `LEMONFIBER` names the binary
where it is not on the path.

## The rules you cannot break

- **One mount of the data root per service.** A service declares only whether it
  takes data, and lemonfiber writes the one `/data` mount. Never split downloads
  and media into separate mounts — it breaks hardlinks (ADR-0006).
- **One profile per service** (`B1-R1`); **no `depends_on` across profiles**
  (`B1-R14`). The one edge is qBittorrent's, inside the egress guard's network.
- **Pinned by digest, never by tag alone** (`E1-R1`). `digest` is the
  multi-architecture index's, beside a non-floating `tag`, and the project pulls
  `image:tag@digest`. A digest that is not an index publishing
  `linux/amd64` and `linux/arm64` fails CI (`F2-R6`).
- **`bind` matches the manifest tier** — admin services `loopback`, household
  services `lan` (`C6`). Only Gluetun holds `NET_ADMIN`, by its `egress-guard`
  shape, and a download client sharing its profile runs inside its network with
  `network = { via = "network.egress-guard" }` — one outside the tunnel's
  namespace leaks (`C2-R12`).
- **lemonfiber's own images run confined** (`C6-R20`, `C6-R22`). Each declares
  `posture = "confined"` and mounts its own configuration directory alone, and
  reaches only what its asks and wirings name: the request gate is Seerr's only
  path to Sonarr, Radarr and Jellyfin.

## Adding a service

First, `just candidate <url> --image <ref>` judges from its commit and release
history whether the candidate is maintained (`F2-R15`) and says `admit`, `watch`
or `reject`, which goes in the pull request; the criteria are in [docs/maintaining.md](docs/maintaining.md#adding-a-service).

Then its `[[service]]` in `services/<id>.toml`, named in `stack.toml`'s
`include`, and its profile in the relevant forms. lemonfiber writes its Compose
entry. No code (`REPO-R23`).

The `[[service]]` declares its container in the service model (ADR-0043), which
lemonfiber writes its Compose entry from. It also says what the service can do —
`provides`, in lemonfiber's published capability vocabulary — so wiring can ask
for a capability rather than name a service. A service nothing asks anything (Recyclarr, Unpackerr, Homepage,
Caddy, the door, the request gate, the decline service) declares nothing.

It says what the service reaches on the network and what it asks for there —
`reaches` and `asks_for`, both or neither, `reaches = ""` for one that talks to
nothing (`F2-R10`).

A service something reaches, or that reaches something, also needs a
`[[wiring]]`: `by` where the link runs from, then `asks` (a capability, `each =
true` to reach every filler) or `to` with a `why` (by name, the exception).

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
just ci        # the manifest + what the diff says + the validator's own tests
just runs <profiles>   # start them for real and make them answer (needs Docker and a lemonfiber binary)
just images    # each digest an arm64/amd64 index, moved pins their tag's (needs the network)
just pins      # what the weekly `pins` workflow would move (needs the network)
just licences  # what each upstream licences itself as now (needs the network)
just candidate <url>   # judge a candidate on its history (needs the network)
just record <vocabulary> <services>   # re-record what services answer their claimed probes (needs Docker)
```

`scripts/check_runs.py` has the lemonfiber binary write the project for a scratch
copy of the stack, brings each profile up with plain `docker compose` in its
directory, each service under its declared posture (`REPO-R93`), makes it answer
its probe, and takes it down again; `torrent`, which needs a VPN subscription, is
left out. Its `KNOWN_BROKEN` register reports an entry rather than failing, until
it works.

`scripts/validate_manifest.py` holds the manifest and its service files to the
contract; each rule it enforces has a negative test beside it.

## Before you open a PR

- `just ci` is clean, and `just images` if you touched a pin.
