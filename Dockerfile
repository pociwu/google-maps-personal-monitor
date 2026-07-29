FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MAPS_MONITOR_DATA_DIR=/app/state/data \
    MAPS_MONITOR_BACKUP_DIR=/app/state/backups \
    MAPS_MONITOR_DEBUG_DIR=/app/state/debug

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
RUN pip install --no-cache-dir '.[test]'
COPY config/targets.example.yaml ./config/targets.example.yaml

ENTRYPOINT ["maps-monitor", "--config", "/app/config/targets.yaml"]
CMD ["run-and-send"]
