# Production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --no-log-init --uid 1000 --create-home nethub

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R nethub:nethub /app

USER nethub

EXPOSE 8080

# /app/data and /app/registry are mount points (see quadlet/nethub.container)
# -- do not bake content into the image; bind-mount them at runtime.
ENTRYPOINT ["gunicorn", "-c", "nethub/gunicorn.conf.py", "nethub:create_app()"]
