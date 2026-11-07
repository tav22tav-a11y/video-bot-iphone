# Cloud runtime for the Telegram bot: Python, FFmpeg, and a JS runtime (Node.js).
FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 python3-venv ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt

COPY bot.py ./
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
