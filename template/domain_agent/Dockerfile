FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Pre-bake bytecode (mesma decisão do mediador: corta 1-3s de import time
# no cold start). Note a ausência proposital de PYTHONDONTWRITEBYTECODE.
RUN python -m compileall -q /usr/local/lib/python3.12/site-packages /app

EXPOSE 8080

CMD ["python", "-m", "app.main"]
