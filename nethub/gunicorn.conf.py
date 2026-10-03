import os

# Fixed at 1 because of SQLite: the web process and the
# sibling already share one file, and more writers means more lock waits. It
# is a tuning choice rather than a correctness rule -- device credentials
# travel sealed in the job row, so nothing is held in one worker's memory that
# another worker would need. Don't add a WORKERS env var without
# measuring that SQLite copes.
workers = 1

# Required, not optional (docs/deployment.md [one-worker-four-threads]).
# gunicorn's defaults are worker_class='sync' with threads=1, so leaving these
# unset makes production single-threaded: a 1.5 GB upload plus its SHA-512
# pass would hold the whole server for the entire window.
#
# These threads exist so that one process is not also one request at a time.
worker_class = 'gthread'
threads = 4

bind = f"0.0.0.0:{os.environ.get('NETHUB_PORT', '8080')}"

# With gthread this bounds a worker that has stopped responding to the arbiter,
# not a single slow request, which is what makes a multi-minute upload safe to
# leave at two minutes. Under the sync worker the same value applies per
# request, and would kill a 1.2 GB upload slower than ~10 MB/s mid-ingest.
timeout = 120
accesslog = '-'
errorlog = '-'

# Unused (nothing sends gunicorn control commands here) and, since 25.1.0,
# on by default -- gunicorn otherwise tries to create $HOME/.gunicorn for
# the control socket, which fails under the Quadlet unit's ReadOnly=true.
control_socket_disable = True
