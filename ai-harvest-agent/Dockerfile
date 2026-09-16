FROM python:3.11-slim-bookworm

RUN rm -rf /var/lib/apt/lists/* \
    && apt-get clean \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    xvfb \
    x11vnc \
    supervisor \
    websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# --no-cache-dir: don't bake pip's download cache (~114 MB) into the image layer.
RUN pip install --no-cache-dir -r requirements.txt

# chromium here installs full Chromium + chromium-headless-shell. Both are needed:
# PlaywrightService launches headless=True with no channel, which in PW 1.49
# resolves to the headless-shell binary; the headful/persistent path uses full
# Chromium. --no-shell was tried and broke the headless path — do not re-add it.
RUN playwright install chromium

COPY . .
COPY supervisord.conf /etc/supervisor/conf.d/harvest.conf

ENV DISPLAY=:99

EXPOSE 8000 6080

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/harvest.conf"]