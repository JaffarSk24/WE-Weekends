# WE Weekends gateway. Build from the repository root (geo.json lives in web/):
#   docker build -f server/Dockerfile -t we-weekends:local .
FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY server/requirements.txt ./
RUN pip install --require-hashes -r requirements.txt \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin weekends \
 && mkdir -p /data && chown weekends:weekends /data
COPY server/weekends ./weekends
COPY web/assets/geo.json ./weekends/geo.json
USER weekends
EXPOSE 8790
CMD ["uvicorn", "weekends.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8790", "--no-server-header", "--no-access-log", "--limit-concurrency", "64", "--timeout-keep-alive", "5"]
