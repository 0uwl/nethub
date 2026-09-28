# Production image
#
# The base is pinned by digest (the multi-arch index, so amd64 and arm64 both
# resolve): a rebuild of the same commit gets the same Debian and Python, and a
# base change arrives as a reviewable diff -- Dependabot's docker ecosystem
# proposes the new digest weekly. The cost is that Debian security fixes land
# only through that bump; the weekly scan of the published image (cicd.yml,
# scan-published) is what notices when one is overdue.
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --no-log-init --uid 1000 --create-home nethub

WORKDIR /app
# requirements.txt is a hashed lockfile (pip-compile, from requirements.in):
# every package, transitive ones included, is pinned and must match its hash,
# so a replaced file on the index fails the build instead of shipping. pytest
# and the lint tools are in requirements-dev.txt and never reach this image.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY --chown=nethub:nethub nethub nethub

USER nethub

EXPOSE 8080

# /app/data and /app/artifacts are mount points (see quadlet/nethub.container)
# -- do not bake content into the image; bind-mount them at runtime.
#
# The sibling unit overrides Entrypoint= to run `python -m nethub.sibling`
# from this same image (quadlet/nethub-sibling.container).
ENTRYPOINT ["gunicorn", "-c", "nethub/gunicorn.conf.py", "nethub:create_app()"]
