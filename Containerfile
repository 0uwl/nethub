# Production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --no-log-init --uid 1000 --create-home nethub

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=nethub:nethub nethub nethub

USER nethub

EXPOSE 8080

# /app/data and /app/artifacts are mount points (see quadlet/nethub.container)
# -- do not bake content into the image; bind-mount them at runtime.
#
# The sibling unit overrides Entrypoint= to run `python -m nethub.sibling`
# from this same image (quadlet/nethub-sibling.container).
ENTRYPOINT ["gunicorn", "-c", "nethub/gunicorn.conf.py", "nethub:create_app()"]
