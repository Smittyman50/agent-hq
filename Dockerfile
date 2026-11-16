# Agent HQ: builds the Angular UI, then serves it and the API from one Python container.
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npx ng build

FROM python:3.13-slim
# git: the knowledge library's Git sources (your Obsidian vault, runbook repos)
# tesseract-ocr: OCR for scanned PDFs (English included). For more languages, build with
#   --build-arg OCR_LANGS="deu fra" and set AHQ_OCR_LANGUAGES=eng+deu+fra
ARG OCR_LANGS=""
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates tesseract-ocr \
        $(for l in $OCR_LANGS; do echo tesseract-ocr-$l; done) \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir .
COPY --from=ui /ui/dist/frontend/browser ./static
ENV AHQ_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
