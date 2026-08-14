FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hls_resolver.py segment_cache.py stream_limit.py app.py ./

ENV PORT=10000
ENV HLS_WAIT_MS=3500
ENV HLS_GOTO_TIMEOUT_MS=8000
ENV TOKEN_TTL_SECONDS=1800
ENV CACHE_TTL_SECONDS=1800
ENV SEGMENT_CACHE_MB=64
ENV SEGMENT_CACHE_TTL_SECONDS=25
ENV MAX_STREAMS_PER_SID=2
ENV STREAM_IDLE_SECONDS=30
ENV REQUIRE_SID=0

EXPOSE 10000

CMD uvicorn app:APP --host 0.0.0.0 --port ${PORT}
