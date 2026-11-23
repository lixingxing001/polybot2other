FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED=false \
    POLYBOT2OTHER_ENV_FILE=/dev/null

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/

# Match the SDK version used by the current macOS runtime. PYTHONPATH keeps
# the dashboard's bundled static files available from the copied source tree.
RUN python -m pip install --no-cache-dir --disable-pip-version-check . 'py_clob_client_v2==1.0.1' \
    && groupadd --gid 10001 edgepulse \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin edgepulse \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

USER 10001:10001

HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=3 \
    CMD python -c "import json, urllib.request; c=urllib.request.build_opener(urllib.request.ProxyHandler({})); r=c.open('http://127.0.0.1:8791/api/status', timeout=10); assert r.status == 200; json.load(r)"

CMD ["python", "-m", "polybot2other.web", "--host", "127.0.0.1", "--port", "8791"]
