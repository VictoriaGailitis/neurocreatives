# Dockerfile for Neurocreatives

# Pin to Debian 12 (bookworm). Debian 13 (trixie) introduced Sequoia-based
# APT signature verification (sqv) that has been intermittently rejecting
# official repo signatures, breaking `apt-get update` inside builds.
FROM python:3.13-slim-bookworm

# System dependencies (Pillow needs libjpeg/zlib at runtime; psycopg needs libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first to leverage cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

RUN mkdir -p downloads

EXPOSE 8000

# Healthcheck — hits the stats endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/stats > /dev/null || exit 1

CMD ["python", "run.py"]
