# ---- stage 1: build CSS dengan Tailwind CLI standalone (tanpa Node) ----
FROM debian:bookworm-slim AS css
WORKDIR /app
ARG TW_VERSION=3.4.14
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in amd64) tw=linux-x64 ;; arm64) tw=linux-arm64 ;; *) tw=linux-x64 ;; esac \
 && curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TW_VERSION}/tailwindcss-${tw}" \
 && chmod +x /usr/local/bin/tailwindcss \
 && rm -rf /var/lib/apt/lists/*
COPY tailwind.config.js .
COPY apps ./apps
RUN tailwindcss -i apps/dashboard/static/src/input.css -o /out/tailwind.css --minify

# ---- stage 2: runtime ----
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_SETTINGS_MODULE=config.settings.prod
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml
COPY . .
COPY --from=css /out/tailwind.css apps/dashboard/static/css/tailwind.css
RUN SECRET_KEY=build ALLOWED_HOSTS=build python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
