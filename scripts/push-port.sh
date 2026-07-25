#!/bin/sh
# Pushes the VPN's forwarded port into qBittorrent, and releases it again when
# the tunnel drops. Runs inside Gluetun, which shares its network namespace with
# qBittorrent, so the client answers on loopback.
#
# Called with the port to set: the granted port on acquire, 0 on release.
# Releasing matters as much as setting — qBittorrent left holding a port the
# provider has taken back does not re-acquire correctly on the next reconnect.
set -eu

port="${1:?a port is required}"
api="http://127.0.0.1:8081/api/v2"
jar=/tmp/qbt-session.cookies

# A first boot has no WebUI password yet, and an alarming error here would train
# operators to ignore the one message telling them port forwarding is inert.
if [ -z "${QBT_PASSWORD:-}" ]; then
    echo "qBittorrent WebUI password not set: forwarded port ${port} was NOT pushed."
    echo "Set QBITTORRENT_PASSWORD in .env to the password you set in qBittorrent."
    exit 0
fi

# The API answers 403 without a session. --keep-session-cookies is required
# because qBittorrent's SID is a session cookie and is otherwise never written
# to the jar.
if ! wget -qO- --timeout=10 --keep-session-cookies --save-cookies "${jar}" \
    --post-data "username=${QBT_USERNAME:-admin}&password=${QBT_PASSWORD}" \
    "${api}/auth/login" | grep -q 'Ok.'; then
    rm -f "${jar}"
    echo "qBittorrent rejected the WebUI login: forwarded port ${port} was NOT pushed." >&2
    exit 1
fi

wget -qO- --timeout=10 --load-cookies "${jar}" \
    --post-data "json={\"listen_port\":${port}}" \
    "${api}/app/setPreferences"

rm -f "${jar}"
echo "qBittorrent listen port set to ${port}"
