# Production image
#
# The base is pinned by digest (the multi-arch index, so amd64 and arm64 both
# resolve): a rebuild of the same commit gets the same Debian and Python, and a
# base change arrives as a reviewable diff -- Dependabot's docker ecosystem
# proposes the new digest weekly. The cost is that Debian security fixes land
# only through that bump; the weekly scan of the published image (ci.yml,
# scan-published) is what notices when one is overdue.
# uv runs only in this stage, to turn uv.lock into a hashed requirements file
# that plain pip installs below; it never reaches the final image. The uv
# image is pinned by digest like the base, and Dependabot bumps both.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS lock
WORKDIR /src
COPY pyproject.toml uv.lock ./
# --frozen exports the lock as committed and never re-resolves; --no-dev
# leaves out pytest and the linters.
RUN ["/uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--output-file", "requirements.txt"]

FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --no-log-init --uid 1000 --create-home nethub

WORKDIR /app
# Every package, transitive ones included, is pinned and must match its hash
# from uv.lock, so a replaced file on the index fails the build instead of
# shipping.
COPY --from=lock /src/requirements.txt .
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
