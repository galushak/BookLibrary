FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBRARY_DATA_DIR=/data \
    LIBRARY_PORT=8080

WORKDIR /app
COPY server.py ./server.py
COPY static ./static

RUN mkdir -p /data

EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import json,urllib.request; json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3))"

CMD ["python", "server.py"]
