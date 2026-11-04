# syntax=docker/dockerfile:1
FROM python:3.12.8-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /w
COPY requirements.txt .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH METIS_ENV=production
RUN groupadd --system --gid 10001 metis && useradd --system --uid 10001 --gid metis --no-create-home --shell /usr/sbin/nologin metis
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root backend/ /app/backend/
USER 10001:10001
EXPOSE 8000
# No secrets in the image: everything arrives through METIS_* environment variables at runtime.
# backend.main:app is provided by the Domain API agent.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
