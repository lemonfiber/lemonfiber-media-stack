# Maintaining the stack

How to add, change and remove a service in `lemonfiber-media-stack`, and what CI
checks on every change. For running the stack, see the [README](../README.md).

## Adding a service

The judgement comes before the edits. A service enters only if it is open source
under an OSI-approved licence, publishes native `linux/arm64` and `linux/amd64`
images, is actively maintained, does something nothing here already does, and
works without a paid tier. An image lemonfiber builds from its own code is the
one exception to the first: it carries lemonfiber's own licence,
`Hippocratic-3.0`, and nothing else (`F2-R5`).

**Maintenance is established from the candidate's own history, never from what it
says about itself.** Read the history:

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
the relevant forms. Then `just ci`. See the specification's
[`30-repos/lemonfiber-media-stack.md`](https://github.com/lemonfiber/spec/blob/main/30-repos/lemonfiber-media-stack.md).

Among the `[[service]]` fields are the two that say what the service does on the
network when it runs — where it reaches, and what it asks for when it gets there:

```toml
reaches  = "the subtitle providers you enable"
asks_for = "Searches for subtitles matching what is in your library, signing in where a provider requires an account."
```

Both or neither, and `reaches = ""` where a service talks to nothing at all —
five of them do, and each still says what it does instead. This is what lets a
service be added with no lemonfiber change and no lemonfiber release: the prose
an operator reads about a new service arrives with the service, in the manifest,
rather than being compiled into the binary a release at a time. A manifest that
answers for some services and not others is refused, because a service nobody
wrote this down for cannot be told from one that reaches nothing.

Beside those is `provides` — what the service can do, so that the stack's wiring
can ask for a capability rather than name a service:

```toml
provides = ["media.serve", "identity.source"]
```

The names are core names in lemonfiber's published vocabulary, and that file is
generated from **this field**: a capability no service here declares fails the
generation, which is how *a capability nothing implements must not be published*
is enforced by the artefact refusing to be built rather than by review. What is
checked here is the shape, because which names exist is lemonfiber's to say.

What a service provides, it demonstrates. Each capability gets a
`[[service.claim]]` that binds every probe the vocabulary declares for it to a
request on this service and to a recording of the answer, kept under
`recordings/<service id>/`:

```toml
[[service.claim]]
capability = "indexer.search"

[[service.claim.probe]]
id = "guarded"
request = { method = "GET", path = "/api/v1/indexer" }
expect = { status = 401 }
fixture = "recordings/prowlarr/indexer-search-guarded.json"
```

A recording is one answer, taken from a fresh container of the image the
manifest pins, and its `recorded_from` names that `image@digest`. A claim for a
capability the service does not provide, a capability claimed twice, a
recording kept anywhere but its own service's directory or missing, and a
recording taken from any other image are all refused, so a pin that moves is
re-recorded in the same change.

`just record <vocabulary> <service>...` takes the recordings. For each service
it starts the pinned image fresh, its configuration on a volume of its own
holding only the templates this repository ships, does its first run with
credentials made for that run alone, asks every probe the claims bind, and
writes each answer with every credential the run made, and the container's name
and addresses, replaced by `<redacted>`. A credential is scrubbed in either case
and URL-, base64- or JSON-encoded, and a recording that still carries one is not
written. No credential is put on a command line or printed. The container, its
volume and any image it pulled are removed afterwards. `<vocabulary>` is the
`capability-vocabulary.json` lemonfiber publishes, which says which probes are
asked with the operator's credential.

Recyclarr, Unpackerr, Homepage, Caddy, the request gate and the decline service
declare nothing. The first two write into other services' configuration and
watch the filesystem; the next two are configured by lemonfiber writing a file
rather than by anything asking them a question; the request gate carries Seerr's
asks rather than answering any of its own, and the decline service answers an
invitee's browser rather than another service. A capability is something one
service asks another for while both are running, and nothing asks any of them.

## What reaches what

`provides` says what each service can do. `[[wiring]]` says what reaches what,
and whether the link is an ask for a capability or a name:

```toml
[[wiring]]
by = "seerr"
asks = "identity.source"

[[wiring]]
by = "bazarr"
asks = "library.curate"
each = true

[[wiring]]
by = "qbittorrent"
to = "gluetun"
why = "It has no network namespace of its own — it is inside this one container's."
```

`by` is the service the link runs *from*, and it is the name a report uses when
nothing fills what was asked for. An ask names a capability rather than a
service, so anything that stands in for the far end is reached by everything
that asked with nothing else changed. `each = true` means the link reaches every
service that fills it rather than the one that does — `library.curate` is
declared here four times over and `identity.source` once, and an ask that could
not tell those apart would be wrong about both. `filled_by` is the stack choosing
between its own claimants, with the reason beside it, and it is a default the
operator substitutes rather than a rule.

A `to` is by name, and it carries `why`. The stack has seven, and every one of
them is genuinely about one service: qBittorrent lives inside Gluetun's network
namespace, which is a container rather than an errand and so has nothing for an
ask to resolve to; Unpackerr and Recyclarr are configured per application, in
each service's own terms, and know Sonarr rather than whatever curates a
library; the decline service makes three of Jellyfin's own calls with a key
lemonfiber minted for it alone. Writing the reason down is the point — an
exception nobody explained reads as an oversight.

**An ordering edge is a by-name wiring.** A `depends_on` names a service, so CI
refuses one that no `[[wiring]]` shows as by name.

Homepage's panels and Caddy's routes are deliberately absent: lemonfiber writes
both files from the tier and the description each service already declares,
which is generation rather than wiring. The request gate has none either: it is
how lemonfiber carries Seerr's asks to Sonarr, Radarr and Jellyfin, not a
capability of its own.

## Networks

Every service is on the project's default network, except where its entry names
others. Five named networks, declared in `compose.yml`, confine the request gate
and the decline service, the images lemonfiber builds, each of which holds a
credential that administers another service:

- **The request gate** publishes no port and is on two internal networks only:
  `requests-gate`, shared with Seerr alone, and `gate-upstream`, shared with
  Sonarr, Radarr and Jellyfin. Seerr is not on the default network: it is on
  `requests-gate` and on `requests`, the bridge that carries its published port,
  its egress, and Homepage and Caddy, which reach it. The gate is Seerr's only
  path to Sonarr, Radarr and Jellyfin
  ([ADR-0032](https://github.com/lemonfiber/spec/blob/main/00-overview/decisions/0032-the-request-service-reaches-the-arrs-through-a-gate.md)).
- **The decline service** is on `decline-upstream`, an internal network shared
  with Jellyfin alone, and on `decline`, which carries its published port and
  nothing else, and translates no address off the host: the port answers, and
  the service reaches nothing beyond it
  ([ADR-0029](https://github.com/lemonfiber/spec/blob/main/00-overview/decisions/0029-a-household-service-declines-an-invitation-with-one-key.md)).

Both run with a read-only root, as the operator rather than root, with every
kernel capability dropped, `no-new-privileges` and a memory limit, and mount
their own configuration directory and nothing else. `scripts/validate_manifest.py`
holds each to that, and to exactly the networks and neighbours its ADR names
(ADR-0033 §4).

## Bumping a pin

A pin is the digest of an image's multi-architecture index, with its tag beside
it for reading: `tag` and `digest` in `stack.toml`, `image:tag@digest` in the
compose fragment. The digest is what runs; the tag is what a reader is shown.

Each pin follows the newest release of its own major. The `pins` workflow runs
weekly, and `scripts/pins.py` finds for each service the newest tag in the
current tag's spelling and major, resolves the digest of the index it names, and
writes both files. A service whose claims bind recordings is re-recorded from
the new image in the same change. It opens one pull request per service and arms
it to merge once every required check is green. A major is never crossed: that is the
operator's decision. `just pins` shows what it would move, and `just pins-apply
<service>` moves one by hand.

lemonfiber's own images, under `ghcr.io/lemonfiber`, are the exception: `pins`
leaves them alone, and they move with the release train. Each publish from a tag
`lemonfiber-<service>` cuts dispatches the `image-bump` workflow with the tag and
the index digest it published; `scripts/image_bump.py` checks that the service is
already in `stack.toml` with that image, and that the registry answers the tag
with that digest on both platforms, and writes the pin. Its pull request meets
the same checks and is armed to merge the same way.

Moving a `tag` is a review of the service, not an edit to a string, and three
checks hold it to that. Each reads the diff against the base branch, so none
says anything about a service the change did not touch.

- **`last_release` moves with the tag** — refreshed to what upstream has
  published now. A pin that moves while the date stays put leaves a record
  describing the *previous* review, and no single revision of the file can show
  it. Where a bump genuinely needs no new date, say so on the commit that makes
  it: `Pin-reviewed: <service-id>`.
- **the upstream licence is read from the forge**, and has to still be
  OSI-approved, or `Hippocratic-3.0` for lemonfiber's own images. A licence that could not be read at all fails the same way — a
  pin bump is where a licence is established, not where it is assumed. A
  licence file the forge cannot identify passes only where it is the same file
  at the new release as at the release pinned before. Recorded identifiers that
  merely differ from upstream's are reported, not failed.
- **the digest is the index the tag names.** A tag that moved with its digest
  left behind fails, and so does a moved pin whose tag the registry resolves to
  another digest. For a pin the change leaves alone, a tag re-published since is
  reported, not failed.

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
| Every `depends_on` shown as a by-name `[[wiring]]` | An exception is visible as one (`F4-R12`, `F9-R4`) |
| A wiring asks or names, never both | The far end is a capability or a service (`F9-R4`) |
| A by-name wiring and a chosen filler say why | Neither reads as an oversight or a rule (`F4-R8`, `F4-R12`) |
| Killswitch routing | Nothing shares Gluetun's profile without its namespace, so no client here is one lemonfiber must report as leaking (`C2-R12`) |
| Pinned by index digest, tag beside it | Nothing changes because time passed, and nothing runs by tag (`E1-R1`) |
| Kernel capabilities match the manifest | Only Gluetun is granted `NET_ADMIN` (`C6`) |
| OSI licence per service | Verified against a vendored SPDX list; lemonfiber's own images carry `Hippocratic-3.0` and nothing else (`F2-R5`) |
| What each service reaches | `reaches` and `asks_for` together, for every service or for none (`F2-R10`, `F1-R5`) |
| Capability names are core names | `area.verb`, never a plugin's namespace; which names exist is lemonfiber's to say (`F4-R4`, `ARCH-R110`) |
| A claim's evidence is its own service's, from its pin | A claim only for a capability the service provides, and one at most; every recording under `recordings/<id>/` and taken from the pinned `image@digest` (`ARCH-R136`) |
| Upstream licence, where a pin moves | Read from the forge; still OSI-approved, or `Hippocratic-3.0` for lemonfiber's own (`F2-R12`, `F2-R5`) |
| A bumped pin carries a reviewed date | `last_release` moves with the tag, or the commit re-affirms it (`F2-R14`) |
| A removal says why | `[[removed]]` names the reason and any replacement (`F2-R13`) |
| Every form resolves | `docker compose config` per form (`REPO-R17`), dragging in nothing outside its profiles (`B1-R14`, `REPO-R19`) |
| Every profile starts | Brought up with plain `docker compose`, every service answering its declared probe on its published port, then torn down. The torrent profile is excluded by name: it needs a VPN subscription (`F1-R1`) |
| Every claim holds against its recordings | Judged by lemonfiber's own judge, checked out at a pinned commit: a recording that refutes a probe fails, and one that cannot be judged is reported unproven (`ARCH-R137`, `F9-R2`) |
| arm64 + amd64 per pin | Read from the pinned index, which must be one (`F2-R6`, `E1-R1`) |
| A moved pin is its tag's index | The registry resolves the tag to the pinned digest (`E1-R1`) |

The parity checks read `docker compose config`'s resolved model rather than the
YAML, so they check what Docker will run, not what the file appears to say.
