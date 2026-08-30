#!/usr/bin/env bash
# Starts a local dev session: builds Containerfile.dev and runs it with the
# repo bind-mounted in at /app, so edits on the host take effect live via
# Flask's reloader with no rebuild needed. Browse to http://localhost:8080.
#
# database.db and instance/registries/ land inside the bind-mounted repo
# (the app's own defaults, unchanged from running it directly on the
# host), so dev data persists across restarts without a separate volume;
# both are already gitignored.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

export NETHUB_PORT="${NETHUB_PORT:-8080}"
export SECRET_KEY="${SECRET_KEY:-dev-only-insecure-secret-key}"
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-dev-admin}"

IMAGE="localhost/nethub-dev:latest"
CONTAINER="nethub-dev"

echo "==> Building dev container image"
podman build -f Containerfile.dev -t "$IMAGE" .

echo "==> Starting dev container (Flask debug/reload on :$NETHUB_PORT)"
podman run --rm --name "$CONTAINER" \
    -p "${NETHUB_PORT}:${NETHUB_PORT}" \
    -v "$PWD:/app:Z" \
    -v "./ansible/inventory/rendered/group_vars:/app/instance/registries:Z" \
    -e NETHUB_PORT \
    -e SECRET_KEY \
    -e ADMIN_USERNAME \
    -e ADMIN_PASSWORD \
    -e DATABASE_PATH \
    -e REGISTRIES_ROOT \
    "$IMAGE" --port "$NETHUB_PORT"
