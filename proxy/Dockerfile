FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hls_resolver.py app.py ./

ENV PORT=10000
ENV HLS_WAIT_MS=2000
ENV HLS_GOTO_TIMEOUT_MS=8000
ENV CACHE_TTL_SECONDS=45

EXPOSE 10000

CMD uvicorn app:APP --host 0.0.0.0 --port ${PORT}
