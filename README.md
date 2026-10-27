<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/logo-on-ink.svg">
    <img alt="lemonfiber" src=".github/logo.svg" height="72">
  </picture>
</p>

<h1 align="center">lemonfiber-media-stack</h1>

> [!IMPORTANT]
> This repository is archived. Each service it held is a first-party plugin with a repository of its own, pinned in [lemonfiber/lemonfiber-plugins](https://github.com/lemonfiber/lemonfiber-plugins), which lemonfiber reads its stack from.

<p align="center">
  The media stack that lemonfiber runs: indexers, download clients, the *arr
  apps, Jellyfin and Seerr. The Compose project lemonfiber writes from it runs
  on its own, with plain <code>docker compose</code>.
</p>

<p align="center">
  <a href="https://github.com/lemonfiber/lemonfiber-media-stack/actions/workflows/validate.yml"><img alt="validate" src="https://github.com/lemonfiber/lemonfiber-media-stack/actions/workflows/validate.yml/badge.svg"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/lemonfiber/lemonfiber-media-stack"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/lemonfiber/lemonfiber-media-stack/badge"></a>
</p>

---

This repository defines a self-hosted media stack of 23 services: twenty
open-source projects, such as Prowlarr, SABnzbd, qBittorrent, Sonarr, Radarr,
Jellyfin and Seerr, with Caddy run twice, as the proxy and as Jellyfin's door,
plus two small services lemonfiber builds for it. Every image is pinned to an
exact version.

The [`lemonfiber`](https://github.com/lemonfiber/lemonfiber) tool writes this
stack's Compose project, wires the services together and checks them. You do not
need it to keep running: the project it writes is a normal Compose project, so
you can run it yourself and stop using lemonfiber at any time.

> **Status:** CI starts every profile on every change and checks that each
> service answers. The exception is the torrent profile (Gluetun and
> qBittorrent): it needs a real VPN subscription, so CI does not start it, and
> nobody has yet started it by hand.

## Run it without lemonfiber

This repository holds the stack's manifest, `stack.toml` and a file per service
in `services/`, and no Compose file. lemonfiber writes the project from them:
`compose.generated.yml` in its stack directory, with every value written in, a
`.env` beside it naming the profiles of the last start and the files Compose
reads, and each service's secrets in a file of its own that no container
mounts. From then on the stack is operable with plain `docker compose` in that
directory, with no lemonfiber binary running. You need Docker with Compose v2.

```sh
docker compose --profile media up -d
```

That starts the library services, with no third-party accounts needed:
Jellyfin on port 8096, plus Seerr, Navidrome, Audiobookshelf and
Calibre-Web-Automated.

Jellyfin is reached through its door, which publishes 8096 and 8920 and asks
Jellyfin, before any item's stream or picture goes on, whether the session
presented may see that item. A picture Jellyfin finds by a name rather than an
id, and its splash screen, it refuses. Its TLS port reads a certificate and key from
the project's `config/door/`, which lemonfiber writes before it starts the door.
Where lemonfiber never started it, make your own pair there before `up`:

```sh
openssl ecparam -name prime256v1 -genkey -noout -out config/door/key.pem
openssl req -new -x509 -key config/door/key.pem -out config/door/certificate.pem \
        -days 825 -subj /CN=door
```

Jellyfin trusts the door's fixed address, 10.80.96.18, to say which client a
request came from. Set it under Dashboard → Networking → Known proxies, and
restart Jellyfin, so its local-network rules see the household device rather
than the door.

### Forms

A *form* is a named set of profiles: the part of the stack you want for one
job. `lemonfiber up <form>` starts one, and [`just`](https://github.com/casey/just)
reads them from `stack.toml`:

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
$ just form tv
--profile search --profile usenet --profile torrent --profile tv --profile subs
```

`lemonfiber up tv` starts those profiles, and so does the same command typed in
the project's directory:

```sh
docker compose --profile search --profile usenet --profile torrent \
               --profile tv --profile subs up -d
```

## The one rule

**Every service that touches your files gets exactly one mount of the data
root, at `/data`.** Downloads and media live under it on one filesystem, so an
import is a hardlink: instant, and no extra disk. Split them across two mounts
and every import silently becomes a copy. A service declares only whether it
takes data, and lemonfiber writes the one mount.

## What is in this repository

| Path | What it is |
|------|------------|
| `stack.toml` | The manifest's root: profiles, forms, wiring, services that were removed, and `include`, which names each service's file |
| `services/` | One file per service, `<id>.toml`, holding its `[[service]]` |
| `.env.example` | Every variable the operator sets, documented |
| `config/` | Starting configuration for Recyclarr, Homepage, Caddy, the door and SABnzbd |
| `recordings/` | Recorded answers from each service, which prove what it can do |
| `scripts/` | The checks CI runs, each runnable locally through `just`; the ones that start the stack run the project lemonfiber writes |

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
