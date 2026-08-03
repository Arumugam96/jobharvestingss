FROM python:3.11-slim-bookworm
# Pinned to bookworm (Debian 12) — the floating `python:3.11-slim` tag now
# resolves to trixie (Debian 13), whose renamed font packages
# (ttf-unifont/ttf-ubuntu-font-family) break `playwright install --with-deps`.

# Install system deps for Playwright + a virtual display stack so the
# non-headless Chromium (used for the manual LinkedIn/Naukri login flow)
# has somewhere to render when there's no physical monitor (e.g. on EC2).
RUN apt-get update && apt-get install -y \
    curl wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libasound2 libpango-1.0-0 libpangocairo-1.0-0 \
    xvfb x11vnc supervisor websockify \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium --with-deps

COPY . .
COPY supervisord.conf /etc/supervisor/conf.d/harvest.conf

ENV DISPLAY=:99

EXPOSE 8000 6080

CMD ["supervisord", "-c", "/etc/supervisor/conf.d/harvest.conf"]
