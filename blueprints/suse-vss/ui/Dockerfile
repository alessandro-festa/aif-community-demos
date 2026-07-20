# SUSE VSC custom UI — FastAPI + static frontend, with CPU CLIP search.
# Build:  docker build -t ghcr.io/alessandro-festa/suse-vss-ui:dev .
FROM python:3.12-slim

# ffmpeg + libs for OpenCV (decode, RTSP); rest kept slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# CPU-only torch from the dedicated wheel index — avoids the multi-GB CUDA build.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the CLIP model into the image so the first request doesn't download ~600 MB.
# Cache in a world-readable dir since the app runs as a non-root user.
ENV SENTENCE_TRANSFORMERS_HOME=/opt/models \
    HF_HOME=/opt/models/hf
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('clip-ViT-B-32')" \
    && chmod -R a+rX /opt/models

COPY app/ ./app/
COPY static/ ./static/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
ENV OLLAMA_BASE_URL=http://ollama:11434/v1 \
    VLM_MODEL=moondream:1.8b \
    CLIP_MODEL=clip-ViT-B-32 \
    MILVUS_URI=http://milvus:19530 \
    FRAME_COUNT=0

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
