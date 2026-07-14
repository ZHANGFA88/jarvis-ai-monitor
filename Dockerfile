FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DASHBOARD_PORT=8765 \
    DASHBOARD_HOST=0.0.0.0

WORKDIR /app

COPY public ./public
COPY scripts ./scripts
COPY config.example.json ./config.example.json

RUN chmod +x scripts/*.sh scripts/*.py 2>/dev/null || true

EXPOSE 8765

CMD ["python3", "scripts/serve.py"]
