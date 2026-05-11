# syntax=docker/dockerfile:1.6
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=5050

WORKDIR /app

# System deps for PDF/text processing (pdfplumber, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libjpeg-dev \
        zlib1g-dev \
        libfreetype6-dev \
        liblcms2-dev \
        libopenjp2-7 \
        libtiff5-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Pre-create writable dirs (Railway filesystem is ephemeral, but the code expects them)
RUN mkdir -p data/uploads data/jobs

EXPOSE 5050

# run.py already binds 0.0.0.0 and respects $PORT
CMD ["python", "run.py"]
