import os

# Fixed at 1, not a WORKERS knob -- unlike a Postgres-backed app, NetHub's
# SQLite store (design-document.md §5) and the single-worker hard rule in
# CLAUDE.md both assume exactly one process. Don't add a WORKERS env var
# without revisiting that.
workers = 1

bind = f"0.0.0.0:{os.environ.get('NETHUB_PORT', '8080')}"
timeout = 120
accesslog = '-'
errorlog = '-'

# Unused (nothing sends gunicorn control commands here) and, since 25.1.0,
# on by default -- gunicorn otherwise tries to create $HOME/.gunicorn for
# the control socket, which fails under the Quadlet unit's ReadOnly=true.
control_socket_disable = True
