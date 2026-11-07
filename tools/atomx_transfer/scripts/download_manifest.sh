#!/usr/bin/env bash
# Worker-only download adaptation of khushidesai1/atomx-transfer at
# 5d864b6cd13f1873364aa70b9ac1939f4c656616. The trusted wrapper supplies an exact
# manifest pinned at confirmation. Uploads and completion checks belong to the engine.
set -euo pipefail
: "${ATOMX_PASSWORD_FD:?}" "${ATOMX_KNOWN_HOSTS:?}" "${ATOMX_SFTP_HOST:?}"
: "${ATOMX_SFTP_USER:?}" "${ATOMX_SFTP_PORT:?}"
# OpenSSH keeps the first option value; -b adds BatchMode=yes internally.
# Enable the private-pipe password helper before selecting the batch file.
exec sshpass -d "$ATOMX_PASSWORD_FD" sftp -F /dev/null -o BatchMode=no -b "$1" \
  -o StrictHostKeyChecking=yes \
  -o "UserKnownHostsFile=$ATOMX_KNOWN_HOSTS" -o GlobalKnownHostsFile=/dev/null \
  -o IdentityAgent=none -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive \
  -o ConnectTimeout=30 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -o "Port=$ATOMX_SFTP_PORT" -o "User=$ATOMX_SFTP_USER" "$ATOMX_SFTP_HOST"
