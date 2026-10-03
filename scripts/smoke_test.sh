#!/usr/bin/env bash
# Container smoke test. Runs the image the way the two Quadlet
# units do and checks that a deployment comes up:
#
#   - the image's own keygen makes the sibling's key pair;
#   - the sibling, started first on an empty database, waits for the web unit
#     to migrate rather than migrating itself or crashing;
#   - the web unit migrates, bootstraps an admin and serves GET /login with a
#     200 and its Content-Security-Policy;
#   - the sibling then logs its start line, and both are still running.
#
# Same shape as quadlet/nethub.container and quadlet/nethub-sibling.container:
# read-only root, tmpfs /tmp (and /run for the web unit), UID 1000, the data and
# artifact directories bind-mounted into both with :z. Under podman, UserNS is
# keep-id:uid=1000,gid=1000, as in the units.
#
# What it does NOT cover: SELinux. The :z relabel only matters on an enforcing
# host, and neither CI's Ubuntu runners (AppArmor) nor a nested podman can
# provide one, so a :Z-versus-:z relabel conflict would pass here. Nor does it
# run
# the units under systemd: it is the same flags by hand, not Quadlet itself.
#
# Usage: scripts/smoke_test.sh [image]        (default localhost/nethub:latest)
#   ENGINE=podman|docker   container engine (default podman, which CI uses).
#                          docker has no keep-id, so it runs as --user 1000:1000
#                          and chowns the bind-mounted directories first.
#   SMOKE_PORT             host port for the web unit (default 18080)
#   SMOKE_TIMEOUT          seconds to wait for each check (default 90)
set -euo pipefail

IMAGE=${1:-localhost/nethub:latest}
ENGINE=${ENGINE:-podman}
PORT=${SMOKE_PORT:-18080}
TIMEOUT=${SMOKE_TIMEOUT:-90}

name=nethub-smoke-$$
web=$name-web
sibling=$name-sibling
work=$(mktemp -d)
mkdir -p "$work/data" "$work/artifacts" "$work/keys"

cleanup() {
    local status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- web container log"; "$ENGINE" logs "$web" 2>&1 || true
        echo "--- sibling container log"; "$ENGINE" logs "$sibling" 2>&1 || true
    fi
    "$ENGINE" rm -f "$web" "$sibling" >/dev/null 2>&1 || true
    if [ "$ENGINE" = docker ]; then
        # Owned by UID 1000 now; a rootless user may not be able to remove them.
        "$ENGINE" run --rm --user 0 --entrypoint rm -v "$work:/w" "$IMAGE" \
            -rf /w/data /w/artifacts /w/keys >/dev/null 2>&1 || true
    fi
    rm -rf "$work" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT

case "$ENGINE" in
    podman) user=("--userns=keep-id:uid=1000,gid=1000") ;;
    docker)
        user=(--user 1000:1000)
        "$ENGINE" run --rm --user 0 --entrypoint chown -v "$work:/w" "$IMAGE" \
            -R 1000:1000 /w
        ;;
    *) echo "ENGINE must be podman or docker, not $ENGINE" >&2; exit 2 ;;
esac

# Everything both units share: the hardening and the two shared directories.
unit=(--read-only --tmpfs /tmp "${user[@]}"
      -v "$work/data:/app/data:z" -v "$work/artifacts:/app/artifacts:z"
      -e DATABASE_PATH=/app/data/database.db -e ARTIFACT_STORE=/app/artifacts)

wait_for() {
    local what=$1; shift
    local i
    for ((i = 0; i < TIMEOUT; i++)); do
        if "$@"; then
            echo "ok: $what"
            return 0
        fi
        sleep 1
    done
    echo "FAILED: $what (gave up after ${TIMEOUT}s)" >&2
    return 1
}

logged() { "$ENGINE" logs "$1" 2>&1 | grep -q "$2"; }
running() { [ "$("$ENGINE" inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }
serves_login() {
    curl -fsS -o /dev/null -D "$work/headers" "http://127.0.0.1:$PORT/login" 2>/dev/null
}

echo "== key pair, from the image's own keygen"
keygen_output=$("$ENGINE" run --rm "${unit[@]}" -v "$work/keys:/keys:z" \
    --entrypoint python "$IMAGE" -m nethub.sealed_credentials keygen \
    --out /keys/credential.key) || { echo "FAILED: keygen" >&2; exit 1; }
public_key=$(sed -n 's/^NETHUB_CREDENTIAL_PUBLIC_KEY=//p' <<<"$keygen_output")
if [ -z "$public_key" ]; then
    echo "FAILED: keygen printed no public key" >&2
    exit 1
fi
echo "ok: key pair generated"

echo "== sibling first, on an empty database"
"$ENGINE" run -d --name "$sibling" "${unit[@]}" \
    -v "$work/keys:/keys:ro,z" \
    -e NETHUB_SEARCH_DIR=/app/artifacts \
    -e NETHUB_CREDENTIAL_KEY_FILE=/keys/credential.key \
    -e NETHUB_CREDENTIAL_PUBLIC_KEY="$public_key" \
    --entrypoint python "$IMAGE" -m nethub.sibling >/dev/null
wait_for "the sibling waits for the web unit to migrate" \
    logged "$sibling" 'waiting for the web unit to migrate'

echo "== web unit"
secret=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
"$ENGINE" run -d --name "$web" "${unit[@]}" --tmpfs /run \
    -p "127.0.0.1:$PORT:8080" \
    -e SECRET_KEY="$secret" \
    -e NETHUB_CREDENTIAL_PUBLIC_KEY="$public_key" \
    -e ADMIN_USERNAME=smoke \
    "$IMAGE" >/dev/null
wait_for "GET /login returns 200" serves_login
if ! grep -qi "^content-security-policy: .*script-src 'none'" "$work/headers"; then
    echo "FAILED: /login has no Content-Security-Policy refusing scripts" >&2
    cat "$work/headers" >&2
    exit 1
fi
echo "ok: /login carries the Content-Security-Policy"
wait_for "the web unit bootstrapped the first admin" \
    logged "$web" "created initial admin user 'smoke'"

echo "== sibling, once the schema is current"
wait_for "the sibling logs its start line" logged "$sibling" 'runner .* started'
for container in "$web" "$sibling"; do
    if ! running "$container"; then
        echo "FAILED: $container is no longer running" >&2
        exit 1
    fi
done
echo "ok: both containers still running"
echo "smoke test passed"
