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
        unzip \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Xray-core — required for VLESS VPN connections to Telegram (vless:// links).
# Telethon speaks only SOCKS5/HTTP/MTProxy, so a local Xray process bridges
# VLESS → local SOCKS5. Pin a known-good release; override with XRAY_VERSION.
ARG XRAY_VERSION=v25.1.30
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) xray_arch="64" ;; \
        arm64) xray_arch="arm64-v8a" ;; \
        *) echo "Unsupported arch: $arch" && exit 1 ;; \
    esac; \
    asset="Xray-linux-${xray_arch}.zip"; \
    rel_path="XTLS/Xray-core/releases/download/${XRAY_VERSION}/${asset}"; \
    # GitHub release-assets CDN is frequently unreachable from some regions \
    # (SSL_ERROR_SYSCALL). Try the direct URL first, then mirror proxies. \
    urls="https://github.com/${rel_path} \
        https://ghproxy.net/https://github.com/${rel_path} \
        https://gh-proxy.com/https://github.com/${rel_path} \
        https://mirror.ghproxy.com/https://github.com/${rel_path}"; \
    ok=0; \
    for url in $urls; do \
        echo "Trying: $url"; \
        if curl -fL --retry 5 --retry-delay 3 --retry-all-errors \
                --connect-timeout 20 -o /tmp/xray.zip "$url"; then \
            ok=1; break; \
        fi; \
    done; \
    [ "$ok" -eq 1 ] || { echo "Failed to download $asset from all sources" && exit 1; }; \
    unzip /tmp/xray.zip xray -d /usr/local/bin; \
    chmod +x /usr/local/bin/xray; \
    rm -f /tmp/xray.zip; \
    /usr/local/bin/xray version
ENV XRAY_BIN=/usr/local/bin/xray

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
