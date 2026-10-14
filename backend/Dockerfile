# Derleme bağlamı depo kökü olmalı (web/ klasörü de kopyalanır).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_DIR=/app/web \
    DB_PATH=/data/zeynep.db

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt

COPY backend/app ./app
COPY web ./web

RUN useradd -m app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
