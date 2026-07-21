# Chest X-ray Copilot UI — FastAPI + static frontend, CPU BiomedCLIP embeddings.
# The marketplace normally runs this UI locally (venv); this image is optional,
# for an in-cluster deployment.
# Build:  docker build -t ghcr.io/alessandro-festa/xray-copilot-ui:dev .
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# CPU-only torch from the dedicated wheel index — avoids the multi-GB CUDA build.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake BiomedCLIP into the image so the first request doesn't download it.
ENV HF_HOME=/opt/models/hf
RUN python -c "from open_clip import create_model_from_pretrained; \
    create_model_from_pretrained('hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224')" \
    && chmod -R a+rX /opt/models

COPY app/ ./app/
COPY static/ ./static/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
ENV OPENAI_BASE_URL=http://vllm-router-service:80/v1 \
    OPENAI_API_KEY=EMPTY \
    DEFAULT_MODEL=chaoyinshe/llava-med-v1.5-mistral-7b-hf \
    MILVUS_URI=http://milvus:19530 \
    CLIP_MODEL=hf-hub:microsoft/BiomedCLIP-PubMedBERT_256-vit_base_patch16_224 \
    IMG_COLLECTION=xray_embeddings

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
