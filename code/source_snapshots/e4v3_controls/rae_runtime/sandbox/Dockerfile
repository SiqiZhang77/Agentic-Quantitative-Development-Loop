# ---- builder: install pinned, hash-verified deps only ----
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9 AS builder
WORKDIR /build
COPY sandbox/requirements.txt requirements.txt
RUN pip install --no-cache-dir --no-deps --require-hashes \
    --prefix=/install -r requirements.txt

# ---- runtime: hardened image ----
FROM python:3.12-slim@sha256:d764629ce0ddd8c71fd371e9901efb324a95789d2315a47db7e4d27e78f1b0e9 AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=builder /install /usr/local
COPY sandbox/ .
COPY proxy/ ./proxy/
COPY mcp/ ./mcp/
COPY after_backtest/ ./after_backtest/
RUN useradd --create-home --uid 1000 sandbox \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace \
    && chmod 0750 /workspace
VOLUME ["/workspace"]
COPY --chmod=0555 sandbox/entrypoint.sh /entrypoint.sh
USER sandbox
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "run.py"]
