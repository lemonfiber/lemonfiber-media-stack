# lemonfiber/media-stack tasks.
default:
    @just --list

# Everything CI runs.
ci: config validate

# Validate the compose file.
config:
    VPN_PROVIDER=protonvpn WIREGUARD_PRIVATE_KEY=x docker compose config --quiet

# Validate the manifest against the contract.
validate:
    python3 scripts/validate_manifest.py

# Start a form, e.g. `just up tv`.
up form:
    docker compose --profile {{form}} up -d
