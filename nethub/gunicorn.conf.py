import os

# Fixed at 1, not a WORKERS knob -- unlike a Postgres-backed app, NetHub's
# SQLite store (design-document.md §5) and the single-worker hard rule in
# CLAUDE.md both assume exactly one process. Don't add a WORKERS env var
# without revisiting that.
workers = 1

# The other half of the one-worker rule, and required rather than optional
# (CLAUDE.md, "Flask runs exactly one worker -- and more than one thread").
# gunicorn's defaults are worker_class='sync' with threads=1, so leaving these
# unset made production single-threaded: a 1.5 GB upload plus its SHA-512 pass
# would hold the whole server for the entire window, which is the failure
# design-document.md §3.2 legislates against (2 s p99 on phone-home), arriving
# through ingest instead of through a dispatch call. The credential socket is
# served on its own thread for the same reason and also needs this.
#
# Not a tuning knob to raise for throughput: one process is fixed above because
# the in-memory CredentialStore and SQLite both assume it, and these threads
# exist so that one process is not also one request at a time.
worker_class = 'gthread'
threads = 4

bind = f"0.0.0.0:{os.environ.get('NETHUB_PORT', '8080')}"

# With gthread this bounds a worker that has stopped responding to the arbiter,
# not a single slow request, which is what makes a multi-minute upload safe to
# leave at two minutes. Under the previous sync worker the same value was
# applied per request: a 1.2 GB upload slower than ~10 MB/s was killed
# mid-ingest and the worker restarted, which also discarded every held
# credential and failed pending approvals with failure_stage='credential'.
timeout = 120
accesslog = '-'
errorlog = '-'

# Unused (nothing sends gunicorn control commands here) and, since 25.1.0,
# on by default -- gunicorn otherwise tries to create $HOME/.gunicorn for
# the control socket, which fails under the Quadlet unit's ReadOnly=true.
control_socket_disable = True
