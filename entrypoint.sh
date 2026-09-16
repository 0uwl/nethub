#!/bin/sh
# Image entrypoint. Exists for exactly one reason: to keep gunicorn's hands off
# the credential socket (design doc §9.1).
#
# quadlet/nethub-credential.socket hands this container a listening AF_UNIX
# socket the way systemd hands any unit one -- as an inherited descriptor, with
# $LISTEN_FDS/$LISTEN_PID naming it. That is the right mechanism (nothing in
# either container may bind() the path itself), but gunicorn's arbiter reads
# those same two variables and, when $LISTEN_PID matches its pid, CLEARS the
# configured `bind` and serves HTTP on whatever it inherited. Handing the
# socket over unaltered therefore breaks NetHub twice over: gunicorn speaks
# HTTP on the credential socket, and nothing ever listens on $NETHUB_PORT.
# It also unsets both variables on its way past, so the forked worker -- which
# is where create_app() and the CredentialStore actually live -- would find
# nothing to adopt even if the arbiter had left the descriptor alone.
#
# So: re-export the count under a name gunicorn does not know, and unset
# systemd's own before exec'ing it. The descriptor is deliberately left exactly
# where it is; systemd clears FD_CLOEXEC on it, so it survives this exec and
# then the arbiter's fork, which is what lands it in the worker.
# nethub/credential_socket.py's systemd_socket() reads it from there, and
# confirms it really is a listening AF_UNIX socket before accepting secrets on
# it -- the pid check systemd's own protocol uses cannot survive a fork.
#
# Anything not socket-activated (dev.sh, a plain `podman run`, the sibling unit,
# every test) passes straight through this with no variables set.
set -eu

if [ -n "${LISTEN_FDS:-}" ] && [ "${LISTEN_PID:-}" = "$$" ]; then
    NETHUB_LISTEN_FDS="$LISTEN_FDS"
    export NETHUB_LISTEN_FDS
fi
unset LISTEN_PID LISTEN_FDS LISTEN_FDNAMES

exec "$@"
