#!/usr/bin/env bash
# Starts a local dev session: builds Containerfile.dev and runs it with the
# repo bind-mounted in at /app, so edits on the host take effect live via
# Flask's reloader with no rebuild needed. Browse to http://localhost:8080.
#
# database.db and instance/artifacts/ land inside the bind-mounted repo
# (the app's own defaults, unchanged from running it directly on the
# host), so dev data persists across restarts without a separate volume;
# both are already gitignored.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

export NETHUB_PORT="${NETHUB_PORT:-8080}"
export SECRET_KEY="${SECRET_KEY:-dev-only-insecure-secret-key-do-not-deploy}"
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-dev-admin}"

# config.py defaults SESSION_COOKIE_SECURE on, which is right for a deployment
# but wrong here: this serves plain HTTP. Browsers do treat http://localhost as
# a trustworthy origin and will accept a Secure cookie there, so localhost
# works either way -- but reach this container over a LAN address or hostname
# (http://devbox:8080) and the browser silently refuses to store the cookie,
# so login appears to succeed and bounces straight back to the form with
# nothing in the logs. Dev only; never set this on a real deployment.
export SESSION_COOKIE_INSECURE="${SESSION_COOKIE_INSECURE:-1}"

IMAGE="localhost/nethub-dev:latest"
CONTAINER="nethub-dev"

echo "==> Building dev container image"
podman build -f Containerfile.dev -t "$IMAGE" .

# Bind-mount source must exist first; podman would otherwise create it
# root-owned. This replaced a mount of ansible/inventory/rendered/, a path
# that had already stopped existing before ansible/ was deleted -- and then
# instance/registries, which went with REGISTRIES_ROOT at build step 7.
mkdir -p ./instance/artifacts

echo "==> Starting dev container (Flask debug/reload on :$NETHUB_PORT)"
podman run --rm --name "$CONTAINER" \
    -p "${NETHUB_PORT}:${NETHUB_PORT}" \
    -v "$PWD:/app:Z" \
    -v "./instance/artifacts:/app/instance/artifacts:Z" \
    -e NETHUB_PORT \
    -e SECRET_KEY \
    -e ADMIN_USERNAME \
    -e ADMIN_PASSWORD \
    -e DATABASE_PATH \
    -e ARTIFACT_STORE \
    -e SESSION_COOKIE_INSECURE \
    "$IMAGE" --port "$NETHUB_PORT"
