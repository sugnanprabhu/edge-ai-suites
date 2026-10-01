#!/bin/bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Self-contained bring-up script for the uav-vision-scene stack.
# This folder (including docker-compose.yml, .env, src/secrets/* and the
# scenescape-drone-scene.tar.bz2 scene fixture) can be copied to any machine
# and brought up with just: ./up.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

ENV_FILE=".env"
if [ ! -f "${ENV_FILE}" ]; then
  echo "Error: ${ENV_FILE} not found in ${SCRIPT_DIR}." >&2
  exit 1
fi

# Regenerate secrets/certs only if missing (keeps the copy self-contained across
# fresh clones/machines while not clobbering an existing, working set).
if [ ! -f "src/secrets/browser.auth" ]; then
  echo "Generating secrets/certs..."
  bash src/secrets/generate_secrets.sh
fi

# Keep SUPASS in .env synced with src/secrets/supass (source of truth).
SUPASS_VAL="$(cat src/secrets/supass)"
if grep -q "^SUPASS=" "${ENV_FILE}"; then
  sed -i "s#^SUPASS=.*#SUPASS=${SUPASS_VAL}#" "${ENV_FILE}"
else
  echo "SUPASS=${SUPASS_VAL}" >> "${ENV_FILE}"
fi

# Set HOST_IP (override by passing it as $1, e.g. ./up.sh 10.10.10.10).
HOST_IP_ARG="${1:-}"
if [ -n "${HOST_IP_ARG}" ]; then
  HOST_IP="${HOST_IP_ARG}"
else
  HOST_IP="$(hostname -I | cut -f1 -d' ')"
fi
if grep -q "^HOST_IP=" "${ENV_FILE}"; then
  sed -i "s#^HOST_IP=.*#HOST_IP=${HOST_IP}#" "${ENV_FILE}"
else
  echo "HOST_IP=${HOST_IP}" >> "${ENV_FILE}"
fi

# NOTE: UID/GID are intentionally left blank. The broker service runs
# "user: ${UID}:${GID}"; its entrypoint does `cp -r /run/secrets /mosquitto/secrets`,
# which must create a new directory directly under /mosquitto (owned by root:root
# in the eclipse-mosquitto image). Setting UID/GID to a non-root host user causes
# "Permission denied" and the broker (and anything depending on it: scene, analytics)
# to crash-loop. Leaving them blank makes compose fall back to the image's default
# (root) user, which is required for this entrypoint to succeed.
sed -i "s#^UID=.*#UID=#" "${ENV_FILE}"
sed -i "s#^GID=.*#GID=#" "${ENV_FILE}"

mkdir -p src/nginx/ssl
if [ ! -f src/nginx/ssl/server.key ] || [ ! -f src/nginx/ssl/server.crt ]; then
  echo "Generating self-signed nginx certificate..."
  openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout src/nginx/ssl/server.key -out src/nginx/ssl/server.crt \
    -subj "/C=US/ST=CA/L=San Francisco/O=Intel/OU=Edge AI/CN=localhost"
fi

echo "Configuring stack for HOST_IP=${HOST_IP} (broker runs as default/root; see UID/GID note above)"
docker compose up -d

echo ""
echo "Stack starting. Check status with: docker compose ps"
echo "Once healthy, browse to https://${HOST_IP}/ (login: admin / \$SUPASS in .env)"
