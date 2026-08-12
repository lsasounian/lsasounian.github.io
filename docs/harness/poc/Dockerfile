FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# comando default; sobrescrito por serviço no docker-compose.yml
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
