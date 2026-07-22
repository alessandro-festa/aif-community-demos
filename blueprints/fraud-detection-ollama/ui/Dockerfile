# Fraud / AML investigator UI — FastAPI + static SUSE frontend.
# Local run is the primary path (via the Blueprint Marketplace); this Dockerfile lets you
# optionally publish + run the UI in-cluster.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
ENV POSTGRES_URI=postgresql://fraud:fraud@fraud-db:5432/fraud \
    MILVUS_URI=http://milvus:19530 \
    OPENAI_BASE_URL=http://ollama:11434/v1 \
    LLM_MODEL=qwen2.5:1.5b \
    OPENAI_API_KEY=EMPTY

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
