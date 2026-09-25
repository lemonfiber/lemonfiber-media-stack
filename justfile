# lemonfiber/lemonfiber-media-stack tasks.
default:
    @just --list

# Turn on the repository's own git hooks. Once per clone.
hooks:
    git config core.hooksPath .githooks
    @echo "hooks on: .githooks/pre-push"

# Everything CI runs bar the image check, which needs the network — and the hooks
# turned on if they are not already, this being the command run before a push.
ci: hooks lint validate changes forms docs test forge

# The gate scripts themselves, read by ruff. First, because a name that does not
# exist is a failure every check below would report as its own.
lint:
    uvx ruff@0.16.4 check scripts/

# stack.toml against the contract, and compose.yml held in parity with it.
validate:
    python3 scripts/validate_manifest.py

# Every form and overlay resolves to a valid project.
forms:
    python3 scripts/check_forms.py --self-test
    python3 scripts/check_forms.py

# The service count the docs state matches the one stack.toml defines.
docs:
    python3 scripts/check_docs.py
    python3 scripts/check_docs.py --self-test

# The lint's own tests — each rule proven to fail when the rule is broken.
test:
    python3 scripts/test_validate_manifest.py

# Start profiles for real and make each service answer the probe stack.toml
# declares for it, then take them down. Needs Docker and the network, and it is
# the only check here that runs the stack rather than reading it — `just ci`
# leaves it out for that reason. Services write into config/<service>/ while
# they are up, which is runtime state git ignores everywhere but the four
# templates this repo ships; expect sabnzbd.ini to come back modified.
#
#   just runs search
#   just runs search,usenet
runs profiles:
    python3 scripts/check_runs.py --self-test
    python3 scripts/check_runs.py --profile {{profiles}}

# The groups CI starts, read from the manifest.
runs-list:
    @python3 scripts/check_runs.py --plan

# What a *change* to the manifest has to carry, which no single revision of it
# shows: a pin that moved with its release date reviewed, a service that left
# with its reason recorded. Needs git, and answers about this branch rather than
# about this file — so it is run with the same base CI uses.
changes base="origin/main":
    python3 scripts/check_manifest_change.py --self-test
    python3 scripts/check_manifest_change.py --base {{base}}

# Where the networked checks may address a request, and what they may echo out
# of a reply, and how a registry is addressed. Offline, and the one whose
# failure would be a token going somewhere it should not.
forge:
    python3 scripts/forge.py --self-test
    python3 scripts/registry.py --self-test
    python3 scripts/pins.py --self-test

# Every pinned digest is an index publishing linux/amd64 and linux/arm64, and a
# pin this branch moves is the index its tag names. Networked — but the
# self-test is not, and runs first for that reason: the reading it proves is the
# one least likely to have been exercised by anybody before a registry answers.
images base="origin/main":
    python3 scripts/check_images.py --self-test
    python3 scripts/check_images.py --base {{base}}

# What `pins` would move: each service to the newest release of its major, by
# tag and digest. Networked. `just pins-apply sonarr` writes one.
pins:
    python3 scripts/pins.py --self-test
    python3 scripts/pins.py --plan

pins-apply +services:
    python3 scripts/pins.py --apply {{services}}

# Shipped config templates parse in the service that reads them. Networked.
configs:
    python3 scripts/check_configs.py
    python3 scripts/check_configs.py --self-test

# Recorded upstream release dates still agree with upstream. Networked.
#
# The self-test was written with this check and only CI ran it, so `just
# releases` from a shell answered with less than the same check answers in CI —
# which is the wrong way round, because a shell is where somebody reproduces
# what CI said.
releases:
    python3 scripts/check_releases.py --self-test
    python3 scripts/check_releases.py

# What each service's upstream licences itself as *now*, rather than what the
# manifest says it did when somebody last looked. Networked. Fails only for a
# service whose pin this branch moves; everything else is reported.
licences base="origin/main":
    python3 scripts/check_licences.py --self-test
    python3 scripts/check_licences.py --base {{base}}

# Judge a candidate service on its commit and release history rather than on
# what its README says about itself. Networked, run by hand, and not a gate: a
# candidate has no manifest entry to gate. e.g.
#
#   just candidate https://github.com/owner/project --image ghcr.io/owner/project:v1.2.3
candidate url *flags:
    python3 scripts/check_candidate.py --self-test
    python3 scripts/check_candidate.py {{url}} {{flags}}

# Raw Compose validity, no manifest involved.
config:
    VPN_PROVIDER=protonvpn WIREGUARD_PRIVATE_KEY=x docker compose config --quiet

# List the forms this stack declares.
forms-list:
    @python3 scripts/form_profiles.py --list

# Start a form, e.g. `just up tv`.
#
# A form is a *set* of profiles, not one profile: `tv` means search, usenet,
# torrent, tv and subs together. The set is read from stack.toml, so this cannot
# drift from what lemonfiber would start.
up form:
    docker compose $(python3 scripts/form_profiles.py {{form}}) up -d

# Stop a form the same way.
down form:
    docker compose $(python3 scripts/form_profiles.py {{form}}) down
