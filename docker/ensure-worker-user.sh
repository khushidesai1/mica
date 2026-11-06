#!/bin/sh
# OpenSSH needs a passwd entry even when the container uses a numeric UID.
set -eu

if ! getent group 65532 >/dev/null; then
    groupadd --gid 65532 mica
fi
if ! getent passwd 65532 >/dev/null; then
    useradd --uid 65532 --gid 65532 --home-dir /srv/pistachio \
        --no-create-home --no-log-init --shell /usr/sbin/nologin --password '!' mica
fi

# Preserve the existing runtime identity and home instead of remapping files.
entry=$(getent passwd 65532)
test "$(printf '%s' "$entry" | cut -d: -f4)" = 65532
test "$(printf '%s' "$entry" | cut -d: -f6)" = /srv/pistachio
test "$(printf '%s' "$entry" | cut -d: -f7)" = /usr/sbin/nologin
