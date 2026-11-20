FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

# Optional, OFF by default: CLASSIC_TLS=1 makes the two download steps below (pip, model) offer only classic
# TLS key groups (X25519, P-256). Some networks drop OpenSSL 3.5's larger post-quantum TLS hello, so pip
# times out reaching PyPI (README, Troubleshooting). The config file exists only inside this one RUN step,
# so the final image has no TLS override.
ARG CLASSIC_TLS=0

COPY requirements.txt .
# Install dependencies, then bake the embedding model into the image (~/.cache/chroma/onnx_models) so tests
# and eval run offline.
RUN if [ "$CLASSIC_TLS" = "1" ]; then \
        printf "openssl_conf = d\n[d]\nssl_conf = s\n[s]\nsystem_default = g\n[g]\nGroups = X25519:P-256\n" \
            > /tmp/classic-tls.cnf; \
        export OPENSSL_CONF=/tmp/classic-tls.cnf; \
        echo "CLASSIC_TLS=1: download steps use TLS groups X25519:P-256"; \
    fi \
    && pip install -r requirements.txt \
    && python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm-up'])" \
    && rm -f /tmp/classic-tls.cnf \
    && rm -f /root/.cache/chroma/onnx_models/*/onnx.tar.gz
# ^ Chroma checks only the extracted model files, never the downloaded archive, so deleting it saves ~83 MB
#   without changing behaviour (tests run with --network none afterwards).

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
