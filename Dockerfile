# TalentFlow API (FastAPI control plane)
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Product website, served by the API at / (single-service deployments).
COPY website ./website

# Default the checkpoint DB to /tmp, which is writable on Cloud Run (the rest of
# the filesystem is read-only there). The docker-compose/VM setup overrides this
# with /data and mounts a persistent volume for durable checkpoints.
ENV TALENTFLOW_CHECKPOINT_DB=/tmp/talentflow_checkpoints.sqlite

EXPOSE 8000
# Honour Cloud Run's PORT; default to 8000 for the docker-compose/nginx setup.
CMD uvicorn talentflow.api.app:app --host 0.0.0.0 --port ${PORT:-8000}
