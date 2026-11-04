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
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH METIS_ENV=production
RUN groupadd --system --gid 10001 metis && useradd --system --uid 10001 --gid metis --no-create-home --shell /usr/sbin/nologin metis
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root backend/ /app/backend/
COPY --chown=root:root ml/ /app/ml/
# The exports module reads docs/legal/DISCLAIMER.md at runtime (fails closed without it).
COPY --chown=root:root docs/legal/ /app/docs/legal/
COPY --chown=root:root deploy/uvicorn_log_config.json /app/uvicorn_log_config.json
USER 10001:10001
EXPOSE 8000
# No secrets in the image: everything arrives through METIS_* environment variables at runtime.
# Access log is disabled (it prints query strings); remaining uvicorn logs go through the redaction filter.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-config", "/app/uvicorn_log_config.json"]
