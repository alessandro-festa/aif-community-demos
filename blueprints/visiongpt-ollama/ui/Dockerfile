# VisionGPT (SUSE) demo UI — FastAPI + static SUSE frontend.
# Local run is the primary path (via the Blueprint Marketplace); this Dockerfile
# lets you optionally publish + run the UI in-cluster.
FROM python:3.12-slim

# ffmpeg + libs for OpenCV video decoding.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/
COPY samples/ ./samples/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
ENV OPENAI_BASE_URL=http://ollama:11434/v1 \
    VLM_MODEL=qwen2.5vl:3b \
    OPENAI_API_KEY=EMPTY \
    SENSITIVITY=normal

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
