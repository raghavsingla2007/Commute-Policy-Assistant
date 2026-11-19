FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model into the image (~/.cache/chroma/onnx_models) so tests and eval run offline.
RUN python -c "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction as E; E()(['warm-up'])"

COPY . .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
