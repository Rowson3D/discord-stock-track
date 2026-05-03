FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    STOCK_BOT_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN useradd --create-home --shell /usr/sbin/nologin stockbot \
    && mkdir -p /data \
    && chown -R stockbot:stockbot /app /data /ms-playwright

USER stockbot

CMD ["python", "bot.py"]