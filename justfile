# lemonfiber/lemonfiber-media-stack tasks.
default:
    @just --list

# Turn on the repository's own git hooks. Once per clone.
hooks:
    git config core.hooksPath .githooks
    @echo "hooks on: .githooks/pre-push"

# Everything CI runs bar the image check, which needs the network — and the hooks
# turned on if they are not already, this being the command run before a push.
ci: hooks lint validate forms docs test

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

# Every pinned image publishes linux/amd64 and linux/arm64. Networked — but the
# self-test is not, and runs first for that reason: the reading it proves is the
# one least likely to have been exercised by anybody before a registry answers.
images:
    python3 scripts/check_images.py --self-test
    python3 scripts/check_images.py

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
