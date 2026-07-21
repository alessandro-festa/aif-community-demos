# Insurance Support Copilot UI — FastAPI + static frontend, CPU CLIP + Presidio.
# The marketplace normally runs this UI locally (venv); this image is optional,
# for an in-cluster deployment.
# Build:  docker build -t ghcr.io/alessandro-festa/insurance-support-ui:dev .
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# CPU-only torch from the dedicated wheel index — avoids the multi-GB CUDA build.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_sm

# Bake CLIP so the first request doesn't download it.
ENV HF_HOME=/opt/models/hf SENTENCE_TRANSFORMERS_HOME=/opt/models
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('clip-ViT-B-32')" \
    && chmod -R a+rX /opt/models

COPY app/ ./app/
COPY static/ ./static/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
ENV CHAT_BASE_URL=http://vllm-router-service:80/v1 \
    EMBED_BASE_URL=http://ollama:11434/v1 \
    EMBED_MODEL=nomic-embed-text \
    MILVUS_URI=http://milvus:19530 \
    POSTGRES_URI=postgresql://insurance:insurance@support-db:5432/insurance \
    CASES_COLLECTION=support_cases \
    CLIP_MODEL=clip-ViT-B-32

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
