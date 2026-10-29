# Образ бота мониторинга эфиров.
# Токен и прочие секреты в образ НЕ попадают — передаются при запуске: --env-file .env
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    LOG_DIR=/app/logs

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --system --uid 10001 --home /app monitor \
    && mkdir -p /app/data /app/logs \
    && chown -R monitor:monitor /app/data /app/logs
USER monitor

VOLUME ["/app/data", "/app/logs"]
CMD ["python", "main.py"]
