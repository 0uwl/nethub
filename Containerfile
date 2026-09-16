# Production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --no-log-init --uid 1000 --create-home nethub

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=nethub:nethub nethub nethub
COPY --chmod=0755 entrypoint.sh /usr/local/bin/nethub-entrypoint

USER nethub

EXPOSE 8080

# /app/data and /app/artifacts are mount points (see quadlet/nethub.container)
# -- do not bake content into the image; bind-mount them at runtime.
#
# The entrypoint is a shim, not decoration: it renames systemd's socket-
# activation variables so gunicorn's arbiter cannot mistake the credential
# socket for an HTTP listener and drop its own bind. Read entrypoint.sh before
# changing this line. The sibling unit overrides Entrypoint= and does not need
# it -- it connects to that socket rather than being handed one.
ENTRYPOINT ["/usr/local/bin/nethub-entrypoint", "gunicorn", "-c", "nethub/gunicorn.conf.py", "nethub:create_app()"]
