FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system webterm \
    && adduser --system --ingroup webterm webterm

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY websocket_proxy.py web_http_proxy.py session_manager.py ssh_client.py telnet_client.py ./
COPY config.docker.yaml /app/config.yaml

USER webterm

EXPOSE 8765 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; [socket.create_connection(('127.0.0.1', p), 3).close() for p in (8765, 8766)]"

CMD ["python", "websocket_proxy.py", "-c", "/app/config.yaml"]
