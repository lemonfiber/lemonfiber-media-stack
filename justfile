# lemonfiber/lemonfiber-media-stack tasks.
default:
    @just --list

# Everything CI runs bar the image check, which needs the network.
ci: validate forms docs test

# stack.toml against the contract, and compose.yml held in parity with it.
validate:
    python3 scripts/validate_manifest.py

# Every form and overlay resolves to a valid project.
forms:
    python3 scripts/check_forms.py

# The service count the docs state matches the one stack.toml defines.
docs:
    python3 scripts/check_docs.py
    python3 scripts/check_docs.py --self-test

# The lint's own tests — each rule proven to fail when the rule is broken.
test:
    python3 scripts/test_validate_manifest.py

# Every pinned image publishes linux/amd64 and linux/arm64. Networked.
images:
    python3 scripts/check_images.py

# Shipped config templates parse in the service that reads them. Networked.
configs:
    python3 scripts/check_configs.py
    python3 scripts/check_configs.py --self-test

# Recorded upstream release dates still agree with upstream. Networked.
releases:
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
