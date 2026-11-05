# syntax=docker/dockerfile:1
FROM python:3.12.8-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /w
COPY requirements.txt ml/requirements.txt ./
# ml/requirements.txt (scikit-learn, scipy, PyYAML; joblib comes with scikit-learn) is required by the model
# registry. Its test-only `pytest` pin is filtered out so no dev tooling ships in the image.
RUN grep -v '^pytest' ml/requirements.txt > ml-runtime.txt \
    && python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt -r ml-runtime.txt

FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH METIS_ENV=production STUDIO_CACHE_DIR=/var/lib/metis/cache
RUN groupadd --system --gid 10001 metis && useradd --system --uid 10001 --gid metis --no-create-home --shell /usr/sbin/nologin metis
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root backend/ /app/backend/
COPY --chown=root:root ml/ /app/ml/
COPY --chown=root:root leadstudio/ /app/leadstudio/
# The exports module reads docs/legal/DISCLAIMER.md at runtime (fails closed without it).
COPY --chown=root:root docs/legal/ /app/docs/legal/
COPY --chown=root:root deploy/uvicorn_log_config.json /app/uvicorn_log_config.json
# Writable cache for the read-only root filesystem (compose mounts a volume here); initial ownership is copied to the volume.
RUN mkdir -p /var/lib/metis/cache && chown 10001:10001 /var/lib/metis/cache
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
# No secrets in the image: everything arrives through METIS_* environment variables at runtime.
# Access log is disabled (it prints query strings); remaining uvicorn logs go through the redaction filter.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-config", "/app/uvicorn_log_config.json"]
