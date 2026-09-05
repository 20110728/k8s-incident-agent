FROM python:3.12-slim-bookworm

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd \
        --gid "${APP_GID}" \
        incident-agent \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --home-dir /home/incident-agent \
        --shell /usr/sbin/nologin \
        incident-agent

COPY pyproject.toml ./
COPY backend ./backend
COPY scripts ./scripts
COPY knowledge ./knowledge

RUN python -m pip install --no-cache-dir . \
    && chown -R incident-agent:incident-agent \
        /app \
        /home/incident-agent

USER incident-agent

EXPOSE 8000

HEALTHCHECK \
    --interval=5s \
    --timeout=3s \
    --start-period=5s \
    --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).read()"]

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]