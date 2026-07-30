FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sse_handler.py worker.py ./

# sobrescrito pelo docker-compose.yml pro serviço "worker"
CMD ["uvicorn", "sse_handler:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
