# Optional container image for the FinOps chat UI. The Blueprint Marketplace runs
# this frontend locally in a Python venv (see marketplace.yaml localFrontend), so
# this Dockerfile is only for running the UI as a container yourself.
FROM dp.apps.rancher.io/containers/python:3.12
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
