<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/logo-on-ink.svg">
    <img alt="lemonfiber" src=".github/logo.svg" height="72">
  </picture>
</p>

<h1 align="center">Lemonfiber &mdash; media-stack</h1>

<p align="center">
  The Docker Compose stack Lemonfiber orchestrates: indexers, download clients,
  the *arr automation apps, Jellyfin and Seerr &mdash; 19 services, all
  open-source, all pinned.
</p>

---

> **Status: scaffold.** The manifest (`stack.toml`) is complete; `compose.yml` is
> a validating subset. See the [spec](https://github.com/lemonfiber/spec) and
> [roadmap](https://github.com/lemonfiber/spec/blob/main/00-overview/roadmap.md)
> (this repo is milestone **M1**).

## Runs without lemonfiber

This is the load-bearing property: it's a **standalone Compose project**. Clone
it, set `.env`, and run it with plain Docker — no `lemonfiber` binary anywhere:

```
cp .env.example .env      # set DATA_ROOT, and VPN creds if using torrents
docker compose --profile search --profile usenet --profile tv up -d
```

That's what makes adopting Lemonfiber a reversible decision.

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
| `compose.yml` | The stack itself |
| `.env.example` | Every variable, documented |
| `stacks/` | Overlays: NAS/copy mode, Caddy proxy |
| `config/` | Seeded service config templates |

## Adding a service

Three data edits, no code: a service block in `compose.yml`, a `[[service]]` in
`stack.toml`, and its profile added to the relevant forms. CI holds it to every
rule. See spec
[`30-repos/media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/media-stack.md).

## Contributing

The spec is **canonical** — every change cites a spec identifier. Read
[AGENTS.md](AGENTS.md) and the
[contributing guide](https://github.com/lemonfiber/spec/blob/main/50-governance/contributing.md).

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
  &nbsp;&middot;&nbsp;<a href="https://discord.gg/daQmY23ym"><img alt="Discord" src=".github/discord.svg" height="20"></a>
</p>
