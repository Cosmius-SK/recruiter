# TalentFlow API (FastAPI control plane)
FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Durable workflow checkpoints live on a volume so containers are disposable.
ENV TALENTFLOW_CHECKPOINT_DB=/data/talentflow_checkpoints.sqlite
VOLUME /data

EXPOSE 8000
CMD ["uvicorn", "talentflow.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
