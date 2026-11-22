FROM python:3.11-slim AS build

WORKDIR /build
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860

RUN useradd --create-home --uid 1000 appuser
RUN mkdir -p /home/appuser/app && chown appuser:appuser /home/appuser/app
WORKDIR /home/appuser/app
COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY --chown=appuser:appuser bot.py .

USER appuser
CMD ["sh", "-c", "exec env PORT=\"${PORT:-7860}\" python bot.py"]
