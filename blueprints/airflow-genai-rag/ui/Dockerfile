# Astra — Airflow GenAI RAG demo UI (FastAPI + static SUSE-styled frontend).
# Local run is the primary path (see README); this Dockerfile lets you optionally
# publish the UI and deploy it in-cluster, like suse-vss-ui.
#   docker build -t ghcr.io/<you>/airflow-genai-rag-ui:dev .
FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
# In-cluster defaults (override for local use).
ENV OLLAMA_BASE_URL=http://ollama:11434 \
    MILVUS_URI=http://milvus:19530 \
    EMBED_MODEL=nomic-embed-text \
    GEN_MODEL=astra-custom \
    KB_COLLECTION=kb

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
