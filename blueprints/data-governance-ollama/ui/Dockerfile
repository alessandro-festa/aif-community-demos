# Data Governance Copilot UI — FastAPI + static SUSE frontend.
# Local run is the primary path (via the Blueprint Marketplace); this Dockerfile lets you
# optionally publish + run the UI in-cluster.
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY static/ ./static/

RUN useradd -u 10001 -m appuser
USER 10001

EXPOSE 8000
ENV OM_BASE_URL=http://openmetadata:8585 \
    OM_USER=admin@open-metadata.org \
    OM_PASSWORD=admin \
    OPENAI_BASE_URL=http://ollama:11434/v1 \
    LLM_MODEL=qwen2.5:1.5b \
    OPENAI_API_KEY=EMPTY

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
