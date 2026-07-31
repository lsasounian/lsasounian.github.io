FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Pre-bake bytecode — corta 1-3s de import time no cold start.
# Sem PYTHONDONTWRITEBYTECODE: queremos o .pyc gravado na imagem.
RUN python -m compileall -q /usr/local/lib/python3.12/site-packages /app

EXPOSE 8080
CMD ["python", "main.py"]
