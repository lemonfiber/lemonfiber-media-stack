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

> **Status: defined in full, started in CI, and three of them do not start.**
> All 19 services are defined and every rule below is enforced in CI, which now
> also starts the stack: each profile is brought up with plain `docker compose`
> and every service is made to answer the probe `stack.toml` declares for it.
> The first run of that check found **Jellyfin, Seerr and Bindery cannot start
> from a clean clone on Linux** — Docker creates a missing bind-mount source as
> `root:root`, and a container running as a fixed non-root user cannot then
> write its own `/config`. It does not happen on Docker Desktop, where bind
> mounts ignore ownership, which is why it was never seen. The three are named
> in `KNOWN_BROKEN` in `scripts/check_runs.py`, and that register fails when one
> of them starts working, so it cannot outlive the defect. Still verified only
> by hand: a hardlink import end to end, the VPN killswitch, and the torrent
> profile itself, which needs a real VPN subscription to come up at all. See the
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

That's what makes adopting Lemonfiber a reversible decision — and it is checked
rather than asserted. `scripts/check_runs.py` starts each profile on a runner
with no `lemonfiber` binary on its path, waits for every service to answer the
probe the manifest declares for it, and tears the project down again:

```
just runs search          # one profile
just runs search,usenet   # or several
just runs-list            # what CI fans out over
```

Three of them are recorded in that script as not starting from a clean clone,
and the check reports rather than fails for them — see the status note above. It
fails for everything else, and it fails for one of the three the moment it
starts working, so the register cannot quietly become permanent.

A connection to a published port is deliberately not what it accepts as an
answer. Docker puts a proxy in front of every published port and that proxy
accepts before it knows whether anything inside the container is listening, so a
stack whose services were all replaced by `sleep infinity` passes a check that
only connects. This one reads, and being hung up on is a failure.

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
| `stack.toml` | The manifest Lemonfiber consumes — services, profiles, forms, removals |
| `compose.yml` | Stitches the fragments together; no services of its own |
| `compose/` | One fragment per profile — `tv.yml`, `media.yml`, `torrent.yml`, … |
| `compose/_common.yml` | Shared service defaults, reached via `extends:` |
| `.env.example` | Every variable, documented |
| `stacks/` | Overlay: NAS/copy mode |
| `config/` | Seeded templates for Recyclarr, Homepage and Caddy |
| `scripts/` | The checks CI runs, all runnable locally via `just` |

## Adding a service

The judgement comes before the edits. A service enters only if it is open source
under an OSI-approved licence, publishes native `linux/arm64` and `linux/amd64`
images, is actively maintained, does something nothing here already does, and
works without a paid tier.

**Maintenance is established from the candidate's own history, never from what it
says about itself.** Every project's README says it is actively maintained. The
one this catalogue investigated and rejected presented itself as the successor to
a service in the stack, and had two days of commits, no release and no published
image behind it. Read the history instead:

```
just candidate https://github.com/owner/project --image ghcr.io/owner/project:v1.2.3
```

It reports commits of its own (a fork is measured against what it forked, not
credited with it), releases and tags, last activity, and whether the image
exists — then says `admit`, `watch` or `reject`. Put its answer in the pull
request that proposes the service: `watch` is admissible with that judgement
recorded against it, `reject` belongs in the spec's notable exclusions rather
than in `stack.toml`.

Then three data edits, no code: a service block in the right
`compose/<profile>.yml`, a `[[service]]` in `stack.toml`, and its profile added to
the relevant forms. Then `just ci`. See spec
[`30-repos/lemonfiber-media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/lemonfiber-media-stack.md).

Among the `[[service]]` fields are the two that say what the service does on the
network when it runs — where it reaches, and what it asks for when it gets there:

```toml
reaches  = "the subtitle providers you enable"
asks_for = "Searches for subtitles matching what is in your library, signing in where a provider requires an account."
```

Both or neither, and `reaches = ""` where a service talks to nothing at all —
three of them do, and each still says what it does instead. This is what lets a
service be added with no lemonfiber change and no lemonfiber release: the prose
an operator reads about a new service arrives with the service, in the manifest,
rather than being compiled into the binary a release at a time. A manifest that
answers for some services and not others is refused, because a service nobody
wrote this down for cannot be told from one that reaches nothing.

## Bumping a pin

Moving a `tag` is a review of the service, not an edit to a string, and two
checks hold it to that. Both read the diff against the base branch, so neither
says anything about a service the change did not touch.

- **`last_release` moves with the tag** — refreshed to what upstream has
  published now. A pin that moves while the date stays put leaves a record
  describing the *previous* review, and no single revision of the file can show
  it. Where a bump genuinely needs no new date, say so on the commit that makes
  it: `Pin-reviewed: <service-id>`.
- **the upstream licence is read from the forge**, and has to still be
  OSI-approved. A licence that could not be read at all fails the same way — a
  pin bump is where a licence is established, not where it is assumed. Recorded
  identifiers that merely differ from upstream's are reported, not failed.

## Removing a service

Delete its block from `compose/<profile>.yml` and its `[[service]]` from
`stack.toml`; if it was the only service in its profile, remove the profile and
every form reference to it too. Then record why it went:

```toml
[[removed]]
id = "readarr"
removed_in = "0.1.0"     # the stack version whose catalogue no longer carries it
reason = "Discontinued upstream in 2025. Its repository is archived, so the pin could only ever age."
replaced_by = "bindery"  # omit where nothing took the job over
```

A service that leaves takes its description with it, and a manifest that simply
stops mentioning it cannot say whether it was replaced, renamed or abandoned. CI
refuses a service that disappears without one of these entries.

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
| Kernel capabilities match the manifest | Only Gluetun is granted `NET_ADMIN` (`C6`) |
| OSI licence per service | Verified against a vendored SPDX list (`F2-R5`) |
| What each service reaches | `reaches` and `asks_for` together, for every service or for none (`F2-R10`, `F1-R5`) |
| Upstream licence, where a pin moves | Read from the forge; still OSI-approved (`F2-R12`) |
| A bumped pin carries a reviewed date | `last_release` moves with the tag, or the commit re-affirms it (`F2-R14`) |
| A removal says why | `[[removed]]` names the reason and any replacement (`F2-R13`) |
| Every form resolves | `docker compose config` per form (`REPO-R17`), dragging in nothing outside its profiles (`B1-R14`, `REPO-R19`) |
| Every profile starts | Brought up with plain `docker compose`, every service answering its declared probe on its published port, then torn down — bar three recorded as broken, which fail the check if they start working (`F1-R1`) |
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
