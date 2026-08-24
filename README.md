<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/logo-on-ink.svg">
    <img alt="lemonfiber" src=".github/logo.svg" height="72">
  </picture>
</p>

<h1 align="center">lemonfiber-media-stack</h1>

<p align="center">
  The Docker Compose stack Lemonfiber orchestrates: indexers, download clients,
  the *arr automation apps, Jellyfin and Seerr &mdash; 19 services, all
  open-source, all pinned.
</p>

<p align="center">
  <a href="https://github.com/lemonfiber/lemonfiber-media-stack/actions/workflows/validate.yml"><img alt="validate" src="https://github.com/lemonfiber/lemonfiber-media-stack/actions/workflows/validate.yml/badge.svg"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/lemonfiber/lemonfiber-media-stack"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/lemonfiber/lemonfiber-media-stack/badge"></a>
</p>

---

> **Status: complete, not yet run on hardware.** All 19 services are defined and
> every rule below is enforced in CI. The remaining **M1** exit criteria are the
> two things CI cannot check — a hardlink import verified end to end, and the VPN
> killswitch verified by hand. See the
> [spec](https://github.com/lemonfiber/spec) and
> [roadmap](https://github.com/lemonfiber/spec/blob/main/00-overview/roadmap.md).

## Runs without lemonfiber

This is the load-bearing property: it's a **standalone Compose project**. Clone
it, set `.env`, and run it with plain Docker — no `lemonfiber` binary anywhere:

```
cp .env.example .env      # set DATA_ROOT, and VPN creds if using torrents
docker compose --profile search --profile usenet --profile torrent \
               --profile tv --profile subs up -d
```

That five-profile set is the **`tv` form**. Forms are named profile sets, and
`just up tv` expands one for you straight from `stack.toml`:

```
just forms-list           # search, dl, hunt, tv, movies, music, books, …
just up tv
```

That's what makes adopting Lemonfiber a reversible decision.

Requires Docker Compose **v2.20 or newer** — `compose.yml` uses `include:`.

## The one rule

**Every service gets exactly one `${DATA_ROOT}:/data` mount.** Downloads and
media live as subdirectories under it, on one filesystem, so imports hardlink
instead of copy. Splitting them into separate mounts silently breaks hardlinks —
CI rejects it. See spec
[ADR-0006](https://github.com/lemonfiber/spec/blob/main/00-overview/decisions/0006-single-data-mount.md).

## Files

| File | What |
|------|------|
| `stack.toml` | The manifest Lemonfiber consumes — services, profiles, forms |
| `compose.yml` | Stitches the fragments together; no services of its own |
| `compose/` | One fragment per profile — `tv.yml`, `media.yml`, `torrent.yml`, … |
| `compose/_common.yml` | Shared service defaults, reached via `extends:` |
| `.env.example` | Every variable, documented |
| `stacks/` | Overlay: NAS/copy mode |
| `config/` | Seeded templates for Recyclarr, Homepage and Caddy |
| `scripts/` | The checks CI runs, all runnable locally via `just` |

## Adding a service

Three data edits, no code: a service block in the right `compose/<profile>.yml`,
a `[[service]]` in `stack.toml`, and its profile added to the relevant forms. Then
`just ci`. See spec
[`30-repos/lemonfiber-media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/lemonfiber-media-stack.md).

## What CI enforces

Not style preferences — each has a spec requirement behind it, and each is
proven to fail when broken by `scripts/test_validate_manifest.py`.

| Check | Enforces |
|-------|----------|
| Manifest ↔ compose parity | Every service in one is in the other (`REPO-R18`) |
| One `${DATA_ROOT}:/data` mount per service | Hardlinks (`ADR-0006`, `C5-R5`) |
| Bindings match the manifest tier | Admin on loopback, household on LAN (`C6-R1/R2`) |
| No `depends_on` across a profile | Any subset boots (`B1-R14`) |
| Killswitch routing | Nothing shares Gluetun's profile without its namespace, so no client here is one lemonfiber must report as leaking (`C2-R12`) |
| Pinned, non-floating tags | Nothing changes because time passed (`E1-R1`) |
| Capabilities match the manifest | Only Gluetun holds `NET_ADMIN` (`C6`) |
| OSI licence per service | Verified against a vendored SPDX list (`F2-R5`) |
| Every form resolves | `docker compose config` per form (`REPO-R17`), dragging in nothing outside its profiles (`B1-R14`, `REPO-R19`) |
| arm64 + amd64 per pin | Read from each registry's manifest list (`F2-R6`) |

The parity checks read `docker compose config`'s resolved model rather than the
YAML, so they check what Docker will run, not what the file appears to say.

## Contributing

The spec is **canonical** — every change cites a spec identifier. Read
[AGENTS.md](AGENTS.md) and the
[contributing guide](https://github.com/lemonfiber/spec/blob/main/50-governance/contributing.md).

`just ci` runs the checks above and turns on this repository's pre-push hook,
which refuses a push that would leave a branch carrying no commit `origin/main`
does not — what pushing the trunk over a feature branch looks like. It is `git
config core.hooksPath .githooks`, per clone, and `just hooks` does only that. A
clone where neither has run has no hook: git cannot read `.githooks/` on its own.

## Licence

[Hippocratic License 3.0](LICENSE). The bundled *services* are each independently
open-source (GPL/MIT/Apache); this repo distributes configuration, not their code.

---

<p align="center">
  <a href="https://nightworks.io">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset=".github/nightworks-white.png">
      <img alt="NightWorks.io" src=".github/nightworks-dark.png" height="20">
    </picture>
  </a>
  &nbsp;&middot;&nbsp;<a href="https://discord.nightworks.io"><img alt="Discord" src=".github/discord.svg" height="20"></a>
</p>
