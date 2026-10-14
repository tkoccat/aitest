# Derleme bağlamı depo kökü olmalı.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# libgomp1: onnxruntime (Silero VAD) için gerekli
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agent/requirements.txt .
RUN pip install -r requirements.txt

COPY agent/agent.py .

RUN useradd -m app
USER app

CMD ["python", "agent.py", "start"]
