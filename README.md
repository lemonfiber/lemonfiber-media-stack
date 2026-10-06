<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/logo-on-ink.svg">
    <img alt="lemonfiber" src=".github/logo.svg" height="72">
  </picture>
</p>

<h1 align="center">lemonfiber-media-stack</h1>

<p align="center">
  The Docker Compose media stack that lemonfiber runs: indexers, download
  clients, the *arr apps, Jellyfin and Seerr. It also runs on its own, with
  plain <code>docker compose</code>.
</p>

<p align="center">
  <a href="https://github.com/lemonfiber/lemonfiber-media-stack/actions/workflows/validate.yml"><img alt="validate" src="https://github.com/lemonfiber/lemonfiber-media-stack/actions/workflows/validate.yml/badge.svg"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/lemonfiber/lemonfiber-media-stack"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/lemonfiber/lemonfiber-media-stack/badge"></a>
</p>

---

This repository defines a self-hosted media stack of 22 services: twenty
open-source projects, such as Prowlarr, SABnzbd, qBittorrent, Sonarr, Radarr,
Jellyfin and Seerr, plus two small services lemonfiber builds for it. Every image
is pinned to an exact version.

The [`lemonfiber`](https://github.com/lemonfiber/lemonfiber) tool sets this
stack up, wires the services together and checks them. You do not need it: the
stack is a normal Compose project, so you can run it yourself and stop using
lemonfiber at any time.

> **Status:** CI starts every profile on every change and checks that each
> service answers. The exception is the torrent profile (Gluetun and
> qBittorrent): it needs a real VPN subscription, so CI does not start it, and
> nobody has yet started it by hand.

## Run it without lemonfiber

You need Docker with Compose v2.20 or newer (`compose.yml` uses `include:`).

```sh
git clone https://github.com/lemonfiber/lemonfiber-media-stack.git
cd lemonfiber-media-stack
cp .env.example .env      # set DATA_ROOT; add VPN credentials only for torrents
docker compose --profile media up -d
```

That starts the library services, with no third-party accounts needed:
Jellyfin on port 8096, plus Seerr, Navidrome, Audiobookshelf and
Calibre-Web-Automated. `.env.example` documents every variable.

### Forms

A *form* is a named set of profiles: the part of the stack you want for one
job. [`just`](https://github.com/casey/just) reads them from `stack.toml`:

```console
$ just forms-list
search   Find things. Nothing else runs.
dl       You have a link — fetch it.
hunt     Search and grab, manually.
tv       Search, download and automate television
movies   Search, download and automate film
music    Search, download and automate music
books    Search, download and automate books and audiobooks
auto     Everything automated, nothing served
library  Serve what exists. Requires no third-party accounts.
full     The lot.
proxy    Friendly hostnames. Layers onto any other form.
$ just up tv
```

`just up tv` runs the same command you could type yourself:

```sh
docker compose --profile search --profile usenet --profile torrent \
               --profile tv --profile subs up -d
```

`just down tv` stops it again.

## The one rule

**Every service that touches your files gets exactly one `${DATA_ROOT}:/data`
mount.** Downloads and media live under it on one filesystem, so an import is a
hardlink: instant, and no extra disk. Split them across two mounts and every
import silently becomes a copy. CI rejects a change that does this.

## What is in this repository

| Path | What it is |
|------|------------|
| `stack.toml` | The manifest lemonfiber reads: services, profiles, forms, and services that were removed |
| `compose.yml` | Includes the fragments below and declares the named networks; no services of its own |
| `compose/` | One fragment per profile: `tv.yml`, `media.yml`, `torrent.yml`, and the rest |
| `compose/_common.yml` | Shared service defaults, reached through `extends:` |
| `.env.example` | Every variable, documented |
| `stacks/` | An overlay for NAS and copy mode |
| `config/` | Starting configuration for Recyclarr, Homepage, Caddy and SABnzbd |
| `recordings/` | Recorded answers from each service, which prove what it can do |
| `scripts/` | The checks CI runs, each runnable locally through `just` |

## Documentation

- [The services](https://docs.lemonfiber.app/running/the-services/): what each one does
- [Running without lemonfiber](https://docs.lemonfiber.app/advanced/without-lemonfiber/): the variables, and how to take over from lemonfiber
- [Hardlinks and one mount point](https://docs.lemonfiber.app/fixing/hardlinks-and-one-mount-point/)

## Contributing

[docs/maintaining.md](docs/maintaining.md) covers adding, updating and removing a
service, and every check CI runs. `just ci` runs those checks locally and turns on
the git hooks.

Every change cites a requirement in the
[specification](https://github.com/lemonfiber/spec); read the
[contributing guide](https://github.com/lemonfiber/spec/blob/main/50-governance/contributing.md)
and [AGENTS.md](AGENTS.md) first. Report a vulnerability privately, as
[SECURITY.md](https://github.com/lemonfiber/.github/blob/main/SECURITY.md)
describes.

## Licence

[Hippocratic License 3.0](LICENSE). The services lemonfiber does not build keep
their own open-source licences (GPL, MIT, Apache and others); this repository
holds their configuration, not their code. The request gate and the decline
service, which lemonfiber builds, are under the Hippocratic License too.

lemonfiber is made by [NightWorksIO](https://nightworks.io).

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
